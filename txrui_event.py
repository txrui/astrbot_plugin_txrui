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
from astrbot.api.platform import MessageType

logger = logging.getLogger(__name__)


#: 媒体段类型 -> 客户端可识别的段类型
_MEDIA_SEGMENT_TYPE = {
    "image": "image",
    "file": "file",
    "video": "video",
    "record": "file",
}


class TxruiSendError(RuntimeError):
    """发送失败或结果未知；unknown=True 时不得自动重试，以免重复发送。"""

    def __init__(self, error: str, *, unknown: bool = False) -> None:
        self.error = error
        self.unknown = unknown
        status = "发送结果未知，请先核实微信端，避免重复发送" if unknown else "发送失败"
        super().__init__(f"txrui {status}: {error}")


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
        enable_media: bool = True,
        media_fallback_to_text: bool = True,
        record_as_file: bool = True,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.server = server
        self.chat_id = chat_id
        self.account_id = account_id
        self.chat_type = {
            MessageType.GROUP_MESSAGE: "group",
            MessageType.FRIEND_MESSAGE: "private",
        }.get(message_obj.type, "")
        self.capabilities: dict = dict(capabilities or {})
        # ---- 可调运行时参数（来自平台配置）----
        self.enable_media = bool(enable_media)
        self.media_fallback_to_text = bool(media_fallback_to_text)
        self.record_as_file = bool(record_as_file)

    # ---------- 媒体存储 ----------

    def _store_media(
        self, path: str, filename: str, mime: str, image: bool = False
    ) -> dict:
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
        max_media_bytes = int(getattr(self.server, "max_media_bytes", 0) or 0)
        if max_media_bytes > 0 and len(data) > max_media_bytes:
            return {"ok": False, "media_id": "", "error": "too_large"}
        if image:
            # 缓存文件可能没有后缀或带错误后缀；客户端保存图片时需要真实格式。
            signatures = (
                (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
                (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
                (b"GIF87a", "image/gif", ".gif"),
                (b"GIF89a", "image/gif", ".gif"),
                (b"BM", "image/bmp", ".bmp"),
            )
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                detected = ("image/webp", ".webp")
            else:
                detected = next(
                    ((kind, suffix) for magic, kind, suffix in signatures
                     if data.startswith(magic)),
                    None,
                )
            if detected:
                mime, suffix = detected
                if mimetypes.guess_type(filename)[0] != mime:
                    filename = (os.path.splitext(filename)[0] or "image") + suffix
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

        # Image 没有 _resolve_file_source；直接使用原图，避免不必要的图片解析。
        # 本地 GIF/WebP 必须保留原始字节和动画，不转成 PNG 或单帧。
        if isinstance(comp, Image):
            for source in (comp.url, comp.file, getattr(comp, "path", "")):
                local = self._local_path_of(source)
                if local:
                    return local, os.path.basename(local)

        # Record / Video 有 _resolve_file_source()
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
        await self._send_outbound_message(message)
        await super().send(message)

    async def _send_outbound_message(self, message: MessageChain) -> None:
        """事件回复与主动发送共用媒体处理和确认逻辑，不在此上报发送指标。"""
        if self.server is None:
            raise TxruiSendError("no_server")

        content: list[dict] = []
        errors: list[str] = []
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

            # 语音降级策略由 record_as_file 决定：关闭则跳过语音段
            if segment_type == "record" and not self.record_as_file:
                errors.append("record_disabled")
                continue

            # 媒体总开关
            if not self.enable_media:
                errors.append(f"{segment_type}:media_disabled")
                if self.media_fallback_to_text:
                    content.append(
                        {"type": "text", "text": f"[{segment_type} 已禁用]"}
                    )
                continue

            path, filename = await self._resolve_media_path(comp)
            if not path:
                errors.append(f"{segment_type}:unreadable_media")
                # 取不到本体 —— 绝不引用不存在的 media_id
                if self.media_fallback_to_text:
                    content.append(
                        {
                            "type": "text",
                            "text": f"[{segment_type} 发送失败：本地文件不可读]",
                        }
                    )
                continue

            mime = mimetypes.guess_type(filename or path)[0] or ""
            stored = self._store_media(
                path, filename, mime, segment_type == "image"
            )
            if not stored.get("ok") or not stored.get("media_id"):
                errors.append(f"{segment_type}:{stored.get('error') or 'storage_failed'}")
                logger.warning(
                    "txrui 媒体存储失败 %s: %s", path, stored.get("error", "")
                )
                if self.media_fallback_to_text:
                    content.append(
                        {
                            "type": "text",
                            "text": f"[{segment_type} 发送失败：存储失败]",
                        }
                    )
                continue

            media_id = str(stored["media_id"])
            outbound_type = _MEDIA_SEGMENT_TYPE.get(segment_type, "file")
            if segment_type == "image" and self.capabilities:
                image_supported = bool(self.capabilities.get("image", True))
                if not image_supported and bool(self.capabilities.get("file", True)):
                    outbound_type = "file"
            seg: dict = {
                "type": outbound_type,
                "media_id": media_id,
                "mime": str(stored.get("mime") or mime),
                "url": f"/api/v1/media/blobs/{media_id}",
                "text": str(stored.get("filename") or filename or media_id),
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
                chat_type=self.chat_type,
            )
            if not result.get("ok") or result.get("unknown"):
                error = str(result.get("error") or "send_not_confirmed")
                unknown = bool(result.get("unknown"))
                logger.warning(
                    "txrui outbound 未被确认 chat=%s account=%s unknown=%s: %s",
                    self.chat_id,
                    self.account_id,
                    unknown,
                    error,
                )
                raise TxruiSendError(error, unknown=unknown)
            logger.info(
                "txrui 客户端确认发送 chat=%s types=%s platform_message_id=%s",
                self.chat_id,
                ",".join(seg["type"] for seg in content),
                result.get("platform_message_id", ""),
            )

        # 文本降级仅用于提示用户，不能当作原始媒体发送成功。
        if errors:
            raise TxruiSendError("; ".join(errors))
        if not content and message.chain:
            raise TxruiSendError("no_supported_content")


__all__ = ["TxruiEvent", "TxruiSendError"]
