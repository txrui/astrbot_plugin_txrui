"""txrui 平台适配器：把 txrui connector 协议接入 AstrBot 流水线。

职责边界：
- `TxruiWSServer`（txrui_client.py）只管协议层：握手、帧分发、媒体存取。
- `TxruiAdapter`（本文件）管 AstrBot 侧：payload -> AstrBotMessage -> 事件入队，
  以及主动发送（send_by_session）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from .txrui_client import TxruiConnection, TxruiWSServer
from .txrui_config import TXRUI_CONFIG_METADATA
from .txrui_event import TxruiEvent

# `MessageSession` 在部分 AstrBot 版本里没有从 `astrbot.api.platform` 导出，
# 但 `astrbot.core.platform.message_session` 里一直都有。这里做降级导入，
# 避免因为宿主版本差异导致整个插件装不上。
try:
    from astrbot.api.platform import MessageSession
except ImportError:  # pragma: no cover - 取决于宿主版本
    from astrbot.core.platform.message_session import MessageSession

logger = logging.getLogger(__name__)


def _register_txrui_adapter(cls):
    """注册平台适配器，兼容不同版本的 `register_platform_adapter` 签名。

    旧版本可能没有 `config_metadata` / `support_streaming_message` 等参数，
    直接传会抛 TypeError 导致整个插件装不上。这里按签名过滤参数。
    """
    import inspect

    kwargs = {
        "default_config_tmpl": {
            "id": "txrui_default",
            "enable": False,
            "host": "0.0.0.0",
            "port": 8788,
            "token": "",
            "connector_id": "txrui_001",
            "account_id": "wx_001",
            "media_dir": "data/txrui_media",
        },
        "adapter_display_name": "微信桌面端(txrui)",
        "support_streaming_message": False,
        "config_metadata": TXRUI_CONFIG_METADATA,
        "logo_path": "logo.png",
    }
    try:
        params = inspect.signature(register_platform_adapter).parameters
        accepts_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if not accepts_kwargs:
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):  # pragma: no cover - 签名不可读时全量传
        pass

    return register_platform_adapter("txrui", "txrui_desktop 微信桌面端适配器", **kwargs)(cls)


#: 非文本媒体段的占位文案（对齐 txrui_desktop `payload.py::NON_TEXT_PLACEHOLDER`）
_PLACEHOLDER_TEXT = {
    "image": "[图片]",
    "file": "[文件]",
    "emoji": "[表情]",
    "video": "[视频]",
    "audio": "[语音]",
}


@_register_txrui_adapter
class TxruiAdapter(Platform):
    """txrui_desktop 微信桌面端适配器。"""

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        # Platform.__init__ 的真实签名是 (config, event_queue)
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings

        self.host = str(platform_config.get("host", "0.0.0.0"))
        self.port = int(platform_config.get("port", 8788))
        self.token = str(platform_config.get("token", "") or "")
        self.connector_id = str(platform_config.get("connector_id", "txrui_001") or "")
        self.account_id = str(platform_config.get("account_id", "wx_001") or "")
        media_dir = str(platform_config.get("media_dir", "data/txrui_media") or "")
        self.media_dir = os.path.abspath(media_dir)

        if not self.token and self.host in ("0.0.0.0", "::"):
            logger.warning(
                "txrui 适配器监听在 %s 但未配置 token，"
                "任何人都可以注入微信消息，请立即配置 token 或改监听 127.0.0.1",
                self.host,
            )

        self.server = TxruiWSServer(
            host=self.host,
            port=self.port,
            token=self.token,
            connector_id=self.connector_id,
            account_id=self.account_id,
            media_dir=self.media_dir,
            on_event=self._on_event,
        )
        self._stopped = False

    # ---------- 平台元信息 ----------

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="txrui",
            description="txrui_desktop 微信桌面端适配器",
            id=str(self.config.get("id") or "txrui_default"),
            default_config_tmpl=self.config,
            adapter_display_name="微信桌面端(txrui)",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    # ---------- 生命周期 ----------

    async def run(self) -> None:
        """启动 WS 服务端并常驻。断线由 txrui 侧重连，服务端不主动断。"""
        await self.server.start()
        self.status = Platform.RUNNING
        logger.info(
            "txrui 适配器已启动，监听 %s:%s（connector_id=%s）",
            self.host,
            self.port,
            self.connector_id,
        )
        try:
            while not self._stopped:
                await asyncio.sleep(1)
        finally:
            self.status = Platform.STOPPED

    async def terminate(self) -> None:
        """关闭 WS 服务端。"""
        self._stopped = True
        await self.server.stop()
        logger.info("txrui 适配器已停止")

    # ---------- 入站 ----------

    async def _on_event(self, payload: dict, conn: TxruiConnection) -> None:
        """WS 收到 event 帧 -> AstrBotMessage -> 事件入队。"""
        try:
            message = self.convert_message(payload)
        except Exception as e:  # noqa: BLE001
            logger.exception("txrui event 转换失败: %s", e)
            return
        if message is None:
            return

        event = TxruiEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            server=self.server,
            chat_id=str(payload.get("chat_id") or ""),
            account_id=str(payload.get("account_id") or self.account_id),
            capabilities=conn.capabilities,
        )
        self.commit_event(event)
        logger.info(
            "txrui 收到消息 chat=%s sender=%s type=%s",
            payload.get("chat_id"),
            payload.get("sender", {}).get("platform_user_id", ""),
            message.type.value,
        )

    def convert_message(self, payload: dict) -> AstrBotMessage | None:
        """txrui event payload -> AstrBotMessage。"""
        chat_id = str(payload.get("chat_id") or "")
        if not chat_id:
            logger.warning("txrui event 缺少 chat_id，忽略")
            return None
        chat_type = str(payload.get("chat_type") or "private").lower()
        is_group = chat_type == "group"

        sender_raw = payload.get("sender") or {}
        if not isinstance(sender_raw, dict):
            sender_raw = {}
        user_id = str(sender_raw.get("platform_user_id") or "")
        nickname = str(sender_raw.get("display_name") or "") or None

        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
        )
        message.self_id = self.account_id
        message.session_id = chat_id
        message.message_id = str(
            payload.get("client_seq") or payload.get("event_id") or ""
        )
        message.group = Group(group_id=chat_id) if is_group else None
        message.sender = MessageMember(user_id=user_id, nickname=nickname)
        message.raw_message = payload
        timestamp = payload.get("timestamp")
        if isinstance(timestamp, int):
            message.timestamp = timestamp

        chain: list[Any] = []
        text_parts: list[str] = []
        for seg in payload.get("content") or []:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "text").lower()
            extra = seg.get("extra") or {}
            if not isinstance(extra, dict):
                extra = {}
            text = str(seg.get("text") or "")

            if seg_type == "text":
                if not text:
                    continue
                chain.append(Plain(text))
                text_parts.append(text)
                continue

            if seg_type in ("image", "file", "video", "audio"):
                component = self._build_media_component(seg_type, seg, extra)
                if component is not None:
                    chain.append(component)
                    text_parts.append(_PLACEHOLDER_TEXT.get(seg_type, "[媒体]"))
                continue

            if seg_type in ("quote", "system"):
                # txrui 的 quote/system 段本质是文本
                if text:
                    chain.append(Plain(text))
                    text_parts.append(text)
                continue

            # 未知段类型：按文本处理，不丢内容
            if text:
                chain.append(Plain(text))
                text_parts.append(text)

        message.message = chain
        message.message_str = "\n".join(text_parts)
        return message

    def _build_media_component(self, seg_type: str, seg: dict, extra: dict) -> Any:
        """构造媒体组件。

        有 `media_id` 且本地确实存在 -> 指向真实文件的组件。
        只有 `extra.placeholder=True` -> 降级为占位 Plain，**绝不指向不存在的 media_id**。
        """
        media_id = str(seg.get("media_id") or "")
        placeholder = bool(extra.get("placeholder"))
        text = str(seg.get("text") or "") or _PLACEHOLDER_TEXT.get(seg_type, "[媒体]")

        if not media_id or placeholder:
            return Plain(_PLACEHOLDER_TEXT.get(seg_type, "[媒体]"))

        entry = self.server.media_store.get(media_id)
        if entry is None or not os.path.isfile(entry.path):
            logger.warning(
                "txrui 媒体本体不存在，降级为占位: media_id=%s type=%s", media_id, seg_type
            )
            return Plain(_PLACEHOLDER_TEXT.get(seg_type, "[媒体]"))

        if seg_type == "image":
            return Image.fromFileSystem(entry.path)
        if seg_type == "video":
            return Video.fromFileSystem(entry.path)
        if seg_type == "audio":
            return Record.fromFileSystem(entry.path)
        name = str(extra.get("name") or text or entry.filename or media_id)
        return File(name=name, file=entry.path)

    # ---------- 出站 ----------

    async def send_by_session(
        self,
        session: MessageSession,
        message_chain: MessageChain,
    ) -> None:
        """按会话发送消息（覆写基类：基类默认只上报 Metric）。"""
        chat_id = getattr(session, "session_id", "") or ""
        content = await self._build_outbound_content(message_chain)
        if content:
            result = await self.server.send_outbound(
                chat_id,
                content,
                account_id=self.account_id,
            )
            if not result.get("ok"):
                logger.warning(
                    "txrui send_by_session 未被确认 chat=%s: %s",
                    chat_id,
                    result.get("error", ""),
                )
        await super().send_by_session(session, message_chain)

    async def _build_outbound_content(self, message_chain: MessageChain) -> list[dict]:
        """MessageChain -> outbound 的 content[] 段列表。

        媒体组件无法直接拿到 media_id（媒体本体要先落盘），所以这里只负责
        文本与媒体的存在性判断，真正的媒体存储由 `TxruiEvent.send` 完成。
        """
        content: list[dict] = []
        for comp in message_chain.chain or []:
            if isinstance(comp, Plain):
                if comp.text:
                    content.append({"type": "text", "text": comp.text})
                continue
            if isinstance(comp, (Image, Video, Record, File)):
                # 媒体本体需要先落盘拿 media_id，由事件侧处理。
                # 这里走「无事件上下文」的路径，媒体降级为文本说明，
                # 避免引用不存在的 media_id。
                label = {
                    Image: "图片",
                    Video: "视频",
                    Record: "语音",
                    File: "文件",
                }.get(type(comp), "媒体")
                content.append({"type": "text", "text": f"[{label}]"})
                continue
        return content


__all__ = ["TxruiAdapter"]
