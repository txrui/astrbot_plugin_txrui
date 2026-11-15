"""txrui 事件类：把 AstrBot 的回复转成 txrui connector 协议的 outbound 指令。

出站段格式（对齐 txrui_desktop `ws_client.py::_dispatch_send`）::

    {"type": "text",  "text": "..."}
    {"type": "image", "media_id": "...", "mime": "...", "url": "...", "text": "文件名"}
    {"type": "file",  ...}
    {"type": "video", ...}

媒体本体的流向（**注意：不是**服务端向客户端上传）：

    AstrBot 本地文件
      -> 服务端 media_store 落盘（sha256 内容寻址，得到 media_id）
      -> outbound 段里带 media_id
      -> 客户端收到 outbound 后走 `media_fetch` 取回本体
      -> 客户端粘贴进微信

客户端（`txrui/ws_client.py::_on_message`）只处理 `media_ack` / `media_fetch_ack` /
`ack` / `heartbeat_ack` / `outbound`，**不接受**服务端下发的 `media_upload`，
所以出站媒体必须由服务端自行落盘，而不是让客户端代存。
"""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File, Image, Plain, Record, Video

logger = logging.getLogger(__name__)


#: 媒体段类型 -> 客户端可识别的段类型
_MEDIA_SEGMENT_TYPE = {
    "image": "image",
    "file": "file",
    "video": "video",
    "record": "file",
}


class TxruiEvent(AstrMessageEvent):
    """一条来自 txrui 的微信消息事件。"""

    def __init__(
        self,
        message_str: str,
        message_obj: Any,
        platform_meta: Any,
        session_id: str,
        server: Any = None,
        chat_id: str = "",
        account_id: str = "",
        capabilities: dict | None = None,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.server = server
        self.chat_id = chat_id
        self.account_id = account_id
        self.capabilities: dict = dict(capabilities or {})

    # ---------- 媒体存储 ----------

    def _store_media(self, path: str, filename: str, mime: str) -> dict:
        """把本地媒体本体存入服务端 media_store，返回 {ok, media_id, ...}。

        纯同步（本地文件 IO），不走 WS 往返。
        """
        if self.server is None:
            return {"ok": False, "media_id": "", "error": "no_server"}
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            logger.warning("txrui 读取媒体失败 %s: %s", path, e)
            return {"ok": False, "media_id": "", "error": f"read_failed:{str(e)[:80]}"}
        if not data:
            return {"ok": False, "media_id": "", "error": "empty_media"}
        if not mime:
            mime = mimetypes.guess_type(filename or path)[0] or ""
        try:
            entry = self.server.media_store.put(data, filename=filename, mime=mime)
        except OSError as e:
            logger.warning("txrui 媒体落盘失败 %s: %s", path, e)
            return {"ok": False, "media_id": "", "error": f"storage_failed:{str(e)[:80]}"}
        return {
            "ok": True,
            "media_id": entry.media_id,
            "mime": entry.mime or mime,
            "size": entry.size,
            "filename": entry.filename,
        }

    async def _resolve_media_path(self, comp: Any) -> tuple[str, str]:
        """取媒体组件的本地路径与文件名。

        返回 `(path, filename)`；取不到时 path 为空串。

        优先用组件自带的 `_resolve_file_source()`（直接给原始文件，不转码）：
        txrui 侧发媒体走的是「文件粘贴」，不做格式转换，转码既慢又可能因
        缺少 ffmpeg 而失败。拿不到再退回 `convert_to_file_path()`。
        """
        if isinstance(comp, File):
            # File 的 file 是 property，异步上下文内同步访问会 warning 返回空串，
            # 必须走 await get_file()
            try:
                path = await comp.get_file()
            except Exception as e:  # noqa: BLE001
                logger.warning("txrui File.get_file 失败: %s", e)
                return "", comp.name or ""
            return path or "", comp.name or os.path.basename(path or "")

        # Image / Record / Video 都有 _resolve_file_source()
        resolver = getattr(comp, "_resolve_file_source", None)
        if resolver is not None:
            try:
                source = await resolver()
            except Exception as e:  # noqa: BLE001
                logger.debug("txrui _resolve_file_source 失败: %s", e)
                source = ""
            if source:
                local = self._local_path_of(source)
                if local:
                    return local, os.path.basename(local)

        try:
            path = await comp.convert_to_file_path()
        except Exception as e:  # noqa: BLE001
            logger.warning("txrui convert_to_file_path 失败: %s", e)
            return "", ""
        return path or "", os.path.basename(path or "")

    @staticmethod
    def _local_path_of(source: str) -> str:
        """把媒体引用（本地路径 / file:// URI）解析成本地绝对路径。

        处理不了的引用（http / base64 / data:）返回空串，让调用方回退到
        `convert_to_file_path()`。
        """
        if not source:
            return ""
        candidate = source
        if candidate.startswith("file://"):
            try:
                from astrbot.core.utils.media_utils import file_uri_to_path

                candidate = file_uri_to_path(candidate)
            except Exception:  # noqa: BLE001
                from urllib.parse import unquote, urlparse

                parsed = urlparse(candidate)
                candidate = unquote(parsed.path)
        if candidate.startswith(("http://", "https://", "base64://", "data:")):
            return ""
        try:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        except OSError:
            pass
        return ""

    # ---------- 出站 ----------

    async def send(self, message: MessageChain) -> None:
        """把回复消息发回微信。

        媒体段：服务端先把本体落盘拿到 `media_id`，再在 outbound 段里引用；
        客户端收到后走 `media_fetch` 取回本体并粘贴。
        语音：客户端 `capabilities.audio=False`（微信视觉端没有真语音通道），
        如实降级为文件段并在 `extra.file_fallback` 标记，不谎称发了真语音。
        """
        if self.server is None:
            logger.warning("txrui 事件没有服务端引用，消息未发送")
            await super().send(message)
            return

        content: list[dict] = []
        for comp in message.chain or []:
            if isinstance(comp, Plain):
                if not comp.text:
                    continue
                content.append({"type": "text", "text": comp.text})
                continue

            if isinstance(comp, Image):
                segment_type = "image"
            elif isinstance(comp, Video):
                segment_type = "video"
            elif isinstance(comp, Record):
                segment_type = "record"
            elif isinstance(comp, File):
                segment_type = "file"
            else:
                # At / Reply / Face 等 txrui 不支持的段：如实跳过
                logger.debug("txrui 不支持的组件类型，跳过: %s", type(comp).__name__)
                continue

            path, filename = await self._resolve_media_path(comp)
            if not path:
                # 取不到本体 —— 绝不引用不存在的 media_id
                content.append(
                    {
                        "type": "text",
                        "text": f"[{segment_type} 发送失败：本地文件不可读]",
                    }
                )
                continue

            mime = mimetypes.guess_type(filename or path)[0] or ""
            stored = self._store_media(path, filename, mime)
            if not stored.get("ok") or not stored.get("media_id"):
                logger.warning(
                    "txrui 媒体存储失败 %s: %s", path, stored.get("error", "")
                )
                content.append(
                    {
                        "type": "text",
                        "text": f"[{segment_type} 发送失败：存储失败]",
                    }
                )
                continue

            media_id = str(stored["media_id"])
            seg: dict = {
                "type": _MEDIA_SEGMENT_TYPE.get(segment_type, "file"),
                "media_id": media_id,
                "mime": str(stored.get("mime") or mime),
                "url": f"/api/v1/media/blobs/{media_id}",
                "text": filename or media_id,
            }
            if segment_type == "record":
                # 语音降级为文件：客户端 audio 不支持真语音，只能走文件粘贴
                seg["extra"] = {"file_fallback": True, "original_type": "audio"}
            content.append(seg)

        if content:
            result = await self.server.send_outbound(
                self.chat_id,
                content,
                account_id=self.account_id,
            )
            if not result.get("ok"):
                logger.warning(
                    "txrui outbound 未被确认 chat=%s: %s",
                    self.chat_id,
                    result.get("error", ""),
                )

        await super().send(message)


__all__ = ["TxruiEvent"]
