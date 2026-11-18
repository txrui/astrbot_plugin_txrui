"""txrui 平台适配器：把 txrui connector 协议接入 AstrBot 流水线。

职责边界：
- `TxruiWSServer`（txrui_client.py）只管协议层：握手、帧分发、媒体存取。
- `TxruiAdapter`（本文件）管 AstrBot 侧：payload -> AstrBotMessage -> 事件入队，
  以及主动发送（send_by_session）。

配置来源：**平台配置**（WebUI「平台」页 → 编辑 txrui 机器人实例，存于
`data/cmd_config.json` 的 `platform` 数组）。平台实例由 `PlatformManager`
用该配置直接创建，保存后 `platform_manager.reload()` 重建实例，配置立即生效。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
from .txrui_config import TXRUI_CONFIG_METADATA, build_default_config_tmpl
from .txrui_event import TxruiEvent, TxruiSendError

# `MessageSession` 在部分 AstrBot 版本里没有从 `astrbot.api.platform` 导出，
# 但 `astrbot.core.platform.message_session` 里一直都有。这里做降级导入，
# 避免因为宿主版本差异导致整个插件装不上。
try:
    from astrbot.api.platform import MessageSession
except ImportError:  # pragma: no cover - 取决于宿主版本
    from astrbot.core.platform.message_session import MessageSession

try:
    from astrbot.core.platform.platform import PlatformStatus
except ImportError:  # pragma: no cover - 极旧版本无独立状态枚举
    PlatformStatus = None

logger = logging.getLogger(__name__)


def _register_txrui_adapter(cls):
    """注册平台适配器，兼容不同版本的 `register_platform_adapter` 签名。

    旧版本可能没有 `support_streaming_message` 等参数，直接传会抛 TypeError
    导致整个插件装不上。这里按签名过滤参数。

    **配置入口**：txrui 的所有可调项都注册在 `config_metadata` 里，
    WebUI「平台」页编辑机器人实例时会渲染成表单，保存进
    `data/cmd_config.json` 的 `platform` 数组。平台实例由 `PlatformManager`
    用这份配置直接创建，保存后 `platform_manager.reload()` 会重建实例，
    新配置立即生效，无需任何插件侧的配置同步。
    """
    import inspect

    kwargs = {
        "default_config_tmpl": build_default_config_tmpl(),
        "adapter_display_name": "txrui（微信）",
        "support_streaming_message": False,
        "logo_path": "logo.png",
        "config_metadata": TXRUI_CONFIG_METADATA,
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
    "sticker": "[表情]",
    "video": "[视频]",
    "audio": "[语音]",
}


@_register_txrui_adapter
class TxruiAdapter(Platform):
    """txrui_desktop 微信桌面端适配器。

    配置来源：**平台配置**（WebUI「平台」页 → 编辑 txrui 机器人实例，
    存于 `data/cmd_config.json` 的 `platform` 数组）。

    平台实例由 `PlatformManager` 用平台配置直接创建，因此这里的配置就是
    WebUI「平台」页里填的那一份，无需任何额外的同步逻辑。用户保存后
    AstrBot 调用 `platform_manager.reload()` 重建实例，配置立即生效。
    """

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        # Platform.__init__ 的真实签名是 (config, event_queue)
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings

        self._stopped = False

        # 从平台配置读取全部参数，并建好 WS 服务端。
        self._apply_config(platform_config, rebuild_server=True)

    # ---------- 配置应用 ----------

    def _apply_config(self, cfg: dict, rebuild_server: bool = True) -> None:
        """把平台配置写入适配器实例。

        `rebuild_server=True` 时会重建 WS 服务端实例（用于配置变更后
        让 host/port/token/帧上限等生效）。
        """
        self.host = str(cfg.get("host", "0.0.0.0"))
        self.port = int(cfg.get("port", 8788))
        self.token = str(cfg.get("token", "") or "")
        self.connector_id = str(cfg.get("connector_id", "txrui_001") or "")
        self.account_id = str(cfg.get("account_id", "wx_001") or "")
        media_dir = str(cfg.get("media_dir", "data/txrui_media") or "")
        self.media_dir = os.path.abspath(media_dir)

        # ---- 可调运行时参数（WebUI「平台」页）----
        self.max_media_bytes = self._cfg_int(
            cfg, "max_media_mb", 20, minimum=1
        ) * 1024 * 1024
        self.max_frame_bytes = self._cfg_int(
            cfg, "max_frame_mb", 64, minimum=1
        ) * 1024 * 1024
        # 帧上限必须 ≥ 单文件上限，否则大文件会在帧层就被掐断
        if self.max_frame_bytes < self.max_media_bytes:
            logger.warning(
                "txrui 单帧上限(%sMB) 小于单文件上限(%sMB)，已自动对齐",
                self.max_frame_bytes // (1024 * 1024),
                self.max_media_bytes // (1024 * 1024),
            )
            self.max_frame_bytes = self.max_media_bytes
        self.send_ack_timeout = self._cfg_float(
            cfg, "send_ack_timeout", 30.0, minimum=1.0
        )
        self.handshake_timeout = self._cfg_float(
            cfg, "handshake_timeout", 15.0, minimum=1.0
        )
        self.heartbeat_interval = self._cfg_float(
            cfg, "heartbeat_interval", 0.0, minimum=0.0
        )
        self.enable_media = self._cfg_bool(cfg, "enable_media", True)
        self.media_fallback_to_text = self._cfg_bool(
            cfg, "media_fallback_to_text", True
        )
        self.record_as_file = self._cfg_bool(cfg, "record_as_file", True)
        self.strip_at_in_text = self._cfg_bool(cfg, "strip_at_in_text", False)
        self.max_text_length = self._cfg_int(cfg, "max_text_length", 0, minimum=0)
        self.blocked_sender_ids = self._cfg_list(cfg, "blocked_sender_ids")
        self.blocked_chat_ids = self._cfg_list(cfg, "blocked_chat_ids")
        self.debug_log = self._cfg_bool(cfg, "debug_log", False)
        self.log_payload = self._cfg_bool(cfg, "log_payload", False)

        if not self.token and self.host in ("0.0.0.0", "::"):
            logger.warning(
                "txrui 适配器监听在 %s 但未配置 token，"
                "任何人都可以注入微信消息，请立即配置 token 或改监听 127.0.0.1",
                self.host,
            )

        if rebuild_server:
            self.server = TxruiWSServer(
                host=self.host,
                port=self.port,
                token=self.token,
                connector_id=self.connector_id,
                account_id=self.account_id,
                media_dir=self.media_dir,
                on_event=self._on_event,
                max_media_bytes=self.max_media_bytes,
                max_frame_bytes=self.max_frame_bytes,
                send_ack_timeout=self.send_ack_timeout,
                handshake_timeout=self.handshake_timeout,
                heartbeat_interval=self.heartbeat_interval,
                enable_media=self.enable_media,
                debug_log=self.debug_log,
                log_payload=self.log_payload,
            )

    # ---------- 配置读取辅助 ----------

    @staticmethod
    def _cfg_int(
        cfg: dict, key: str, default: int, minimum: int | None = None
    ) -> int:
        """读整数配置；非法或越界时回落到默认值。"""
        raw = cfg.get(key, default)
        try:
            value = int(float(str(raw)))
        except (TypeError, ValueError):
            logger.warning("txrui 配置 %s 非法(%r)，使用默认值 %s", key, raw, default)
            return default
        if minimum is not None and value < minimum:
            logger.warning(
                "txrui 配置 %s=%s 小于下限 %s，使用下限", key, value, minimum
            )
            return minimum
        return value

    @staticmethod
    def _cfg_float(
        cfg: dict, key: str, default: float, minimum: float | None = None
    ) -> float:
        """读浮点配置；非法或越界时回落到默认值。"""
        raw = cfg.get(key, default)
        try:
            value = float(str(raw))
        except (TypeError, ValueError):
            logger.warning("txrui 配置 %s 非法(%r)，使用默认值 %s", key, raw, default)
            return default
        if minimum is not None and value < minimum:
            logger.warning(
                "txrui 配置 %s=%s 小于下限 %s，使用下限", key, value, minimum
            )
            return minimum
        return value

    @staticmethod
    def _cfg_bool(cfg: dict, key: str, default: bool) -> bool:
        """读布尔配置；接受 true/false、"1"/"0"、1/0。"""
        raw = cfg.get(key, default)
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        logger.warning("txrui 配置 %s 非法(%r)，使用默认值 %s", key, raw, default)
        return default

    @staticmethod
    def _cfg_list(cfg: dict, key: str) -> set[str]:
        """读列表配置；接受 list 或逗号分隔字符串。"""
        raw = cfg.get(key, [])
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            parts = [str(p).strip() for p in raw]
        else:
            return set()
        return {p for p in parts if p}

    # ---------- 平台元信息 ----------

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="txrui",
            description="txrui_desktop 微信桌面端适配器",
            id=str(self.config.get("id") or "txrui_default"),
            default_config_tmpl=self.config,
            adapter_display_name="txrui（微信）",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    def get_client(self) -> TxruiWSServer:
        """返回实际的协议服务对象，供插件调用其媒体与发送接口。"""
        return self.server

    def create_event(
        self,
        message: AstrBotMessage,
        *,
        connection: TxruiConnection | None = None,
    ) -> TxruiEvent:
        """为框架注入、入站和主动发送创建具有微信发送能力的事件。

        与 QQ 官方适配器一样覆盖 Platform.create_event，避免框架注入
        消息时创建只记录发送指标、不会实际下发的基础 AstrMessageEvent。
        路由使用标准消息的 session_id；账号优先使用原始载荷中的账号。
        """
        chat_id = str(message.session_id or "")
        if not chat_id:
            raise TxruiSendError("missing_chat_id")
        payload = message.raw_message if isinstance(message.raw_message, dict) else {}
        account_id = str(payload.get("account_id") or message.self_id or self.account_id)
        conn = connection if connection is not None else self.server.find_connection(
            chat_id, account_id=account_id
        )
        return TxruiEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=chat_id,
            server=self.server,
            chat_id=chat_id,
            account_id=account_id,
            capabilities=conn.capabilities if conn is not None else None,
            enable_media=self.enable_media,
            media_fallback_to_text=self.media_fallback_to_text,
            record_as_file=self.record_as_file,
        )

    # ---------- 生命周期 ----------

    async def run(self) -> None:
        """启动 WS 服务端并常驻。断线由 txrui 侧重连，服务端不主动断。"""
        try:
            await self.server.start()
        except OSError:
            logger.exception(
                "txrui WebSocket 服务端启动失败，监听地址 %s:%s；请根据错误信息排查端口占用或监听权限",
                self.host,
                self.port,
            )
            if PlatformStatus is not None:
                self.status = PlatformStatus.ERROR
            raise
        if PlatformStatus is not None:
            self.status = PlatformStatus.RUNNING
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
            if PlatformStatus is not None:
                self.status = PlatformStatus.STOPPED

    async def terminate(self) -> None:
        """关闭 WS 服务端。"""
        self._stopped = True
        await self.server.stop()
        if PlatformStatus is not None:
            self.status = PlatformStatus.STOPPED
        logger.info("txrui 适配器已停止")

    # ---------- 入站 ----------

    async def _on_event(self, payload: dict, conn: TxruiConnection) -> dict:
        """WS 收到 event 帧 -> AstrBotMessage -> 事件入队，并返回接收状态。"""
        # 1) 黑名单过滤（在转换之前，省掉后续开销）
        if self._is_blocked(payload):
            return {"ok": True, "status": "filtered", "error": ""}
        try:
            message = self.convert_message(payload)
        except Exception:  # noqa: BLE001
            logger.exception("txrui event 转换失败")
            return {"ok": False, "status": "failed", "error": "conversion_failed"}
        if message is None:
            return {"ok": False, "status": "rejected", "error": "invalid_message"}
        if not message.message:
            return {"ok": True, "status": "filtered", "error": "empty_message"}

        event = self.create_event(message, connection=conn)
        self.commit_event(event)
        # 日志里带上 payload 原始的 chat_type，便于与 txrui core 侧对账
        # （core 侧上报日志同样带 type=）。
        logger.info(
            "txrui 收到消息 chat=%s sender=%s type=%s",
            payload.get("chat_id"),
            message.sender.user_id,
            self._resolve_chat_type(payload) or "unknown",
        )
        if self.debug_log:
            logger.info("txrui 调试-原始载荷: %s", payload if self.log_payload else message.message_str)
        return {"ok": True, "status": "queued", "error": ""}

    def _is_blocked(self, payload: dict) -> bool:
        """黑名单判定：命中发送者 ID 或会话 ID 则丢弃该消息。"""
        chat_id = str(payload.get("chat_id") or "")
        sender = payload.get("sender") or {}
        if not isinstance(sender, dict):
            sender = {}
        user_id = str(sender.get("platform_user_id") or "")
        if user_id and user_id in self.blocked_sender_ids:
            logger.info("txrui 消息被屏蔽（发送者 %s 在黑名单）", user_id)
            return True
        if chat_id and chat_id in self.blocked_chat_ids:
            logger.info("txrui 消息被屏蔽（会话 %s 在黑名单）", chat_id)
            return True
        return False

    def _process_text(self, text: str) -> str:
        """入站文本预处理：剥离 @ 提及、限长截断。"""
        if not text:
            return text
        if self.strip_at_in_text:
            # 去掉 @某人 的前缀，只留正文
            stripped = re.sub(r"^\s*@\S+\s*", "", text)
            if stripped:
                text = stripped
        if self.max_text_length > 0 and len(text) > self.max_text_length:
            logger.warning(
                "txrui 入站文本超长(%s > %s)，已截断",
                len(text),
                self.max_text_length,
            )
            text = text[: self.max_text_length]
        return text

    @staticmethod
    def _resolve_chat_type(payload: dict) -> str:
        """解析 event payload 里的会话类型。

        口径与 txrui core 侧一致：``private`` / ``group``。
        优先取顶层 ``chat_type``，其次取 ``extra.chat_type``（部分采集端把
        类型塞在 extra 里）。

        **缺失时返回空串**，不谎报成 ``private``——上层据此归为
        ``OTHER_MESSAGE``，前端显示「未知」而不是误标成私信。
        """
        raw = payload.get("chat_type")
        if not raw:
            extra = payload.get("extra")
            if isinstance(extra, dict):
                raw = extra.get("chat_type")
        return str(raw or "").strip().lower()

    def convert_message(self, payload: dict) -> AstrBotMessage | None:
        """txrui event payload -> AstrBotMessage。"""
        chat_id = str(payload.get("chat_id") or "")
        if not chat_id:
            logger.warning("txrui event 缺少 chat_id，忽略")
            return None
        chat_type = self._resolve_chat_type(payload)
        is_group = chat_type == "group"
        is_private = chat_type == "private"

        sender_raw = payload.get("sender") or {}
        if not isinstance(sender_raw, dict):
            sender_raw = {}
        user_id = str(sender_raw.get("platform_user_id") or "")
        nickname = str(sender_raw.get("display_name") or "") or None

        message = AstrBotMessage()
        if is_group:
            message.type = MessageType.GROUP_MESSAGE
        elif is_private:
            message.type = MessageType.FRIEND_MESSAGE
        else:
            # 类型未知时**不谎报私信**，归为 OTHER_MESSAGE（前端可显示「未知」）。
            message.type = MessageType.OTHER_MESSAGE
        message.self_id = str(payload.get("account_id") or self.account_id)
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
            # 兼容桌面端的 text + extra.emoji 格式，优先交给真实媒体处理。
            if seg_type == "text" and extra.get("emoji"):
                seg_type = "emoji"

            if seg_type == "text":
                if not text:
                    continue
                text = self._process_text(text)
                if not text:
                    continue
                chain.append(Plain(text))
                text_parts.append(text)
                continue

            if seg_type in ("image", "emoji", "sticker", "file", "video", "audio"):
                component = self._build_media_component(seg_type, seg, extra)
                if component is not None:
                    chain.append(component)
                    video_preview = (extra.get("preview") and extra.get("original_type") == "video")
                    if video_preview or (seg_type == "video" and isinstance(component, Image)):
                        text_parts.append("[视频预览]")
                    else:
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
        媒体总开关关闭时 -> 按 `media_fallback_to_text` 决定跳过还是降级为占位文本。
        """
        media_id = str(seg.get("media_id") or "")
        placeholder = bool(extra.get("placeholder"))
        text = str(seg.get("text") or "") or _PLACEHOLDER_TEXT.get(seg_type, "[媒体]")

        if not self.enable_media:
            if not self.media_fallback_to_text:
                return None
            return Plain(_PLACEHOLDER_TEXT.get(seg_type, "[媒体]"))

        if not media_id or placeholder:
            return Plain(_PLACEHOLDER_TEXT.get(seg_type, "[媒体]"))

        entry = self.server.media_store.get(media_id)
        if entry is None or not os.path.isfile(entry.path):
            logger.warning(
                "txrui 媒体本体不存在，降级为占位: media_id=%s type=%s", media_id, seg_type
            )
            return Plain(_PLACEHOLDER_TEXT.get(seg_type, "[媒体]"))

        if seg_type in ("image", "emoji", "sticker"):
            return Image.fromFileSystem(entry.path)
        if seg_type == "video":
            # 兼容旧桌面端：PNG 截图曾以 video/mp4 上传，按真实文件头纠正。
            try:
                with open(entry.path, "rb") as source:
                    head = source.read(12)
            except OSError:
                return Plain("[视频]")
            image_data = (head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM"))
                          or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))
            if image_data:
                return Image.fromFileSystem(entry.path)
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
        """按会话主动发送，复用事件回复的媒体存储与客户端确认逻辑。"""
        chat_id = getattr(session, "session_id", "") or ""
        if not chat_id:
            raise TxruiSendError("missing_chat_id")
        chat_types = {
            MessageType.GROUP_MESSAGE: "group",
            MessageType.FRIEND_MESSAGE: "private",
        }
        chat_type = chat_types.get(session.message_type)
        if chat_type is None:
            raise TxruiSendError("unsupported_session_type")
        message_obj = self.convert_message({
            "chat_id": chat_id,
            "chat_type": chat_type,
            "account_id": self.account_id,
            "sender": {"platform_user_id": self.account_id, "display_name": ""},
            "content": [],
        })
        event = self.create_event(message_obj)
        await event._send_outbound_message(message_chain)
        await super().send_by_session(session, message_chain)


__all__ = ["TxruiAdapter"]
