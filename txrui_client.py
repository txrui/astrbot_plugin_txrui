"""txrui connector WS 服务端。

复用 txrui_desktop 既有的 connector 协议（纯 JSON + base64，无核心模块依赖）：

上行（客户端 -> 服务端）::

    hello         -> hello_ack
    heartbeat     -> heartbeat_ack
    event         -> ack（接收/入队状态，不表示机器人已回复）
    media_upload  -> media_ack
    media_fetch   -> media_fetch_ack
    send_ack      -> （唤醒对应的 outbound Future）

下行（服务端 -> 客户端）::

    outbound      （request_id + payload.content[]）
    send_ack      （由客户端回）

并发要点：
- `websockets` 不保证多协程并发 `send()` 安全，这里每连接一把写锁串行化写帧
  （对齐 txrui_desktop/txrui/ws_client.py 的 `_send_lock`）。
- 媒体本体落文件系统（按 sha256 内容寻址去重），进程内内存索引，
  不给 AstrBot 引入 DB 迁移负担。
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0"

#: 单个媒体最大字节（与 txrui_desktop 的 MAX_MEDIA_BYTES 对齐，默认 20MB）
MAX_MEDIA_BYTES = 20 * 1024 * 1024

#: 接收帧最大字节（与 txrui_desktop 的 max_size 对齐，默认 64MB）
MAX_FRAME_BYTES = 64 * 1024 * 1024

#: outbound 等待 send_ack 的超时（与客户端上传/取媒体超时一致）
SEND_ACK_TIMEOUT = 30.0

#: 出站段类型白名单（对齐 txrui_desktop `ws_client.py::_dispatch_send`）
OUTBOUND_SEGMENT_TYPES = ("text", "image", "file", "video", "sticker")


class TxruiProtocolError(Exception):
    """协议层错误（握手失败、帧非法等）。"""


@dataclass
class MediaEntry:
    """一个已落盘的媒体本体。"""

    media_id: str
    path: str
    mime: str = ""
    size: int = 0
    filename: str = ""
    created_at: float = field(default_factory=time.time)


class MediaStore:
    """媒体本体存储：文件系统落盘 + 内存索引。

    按 sha256 内容寻址（`<sha256[:2]>/<sha256>`）实现去重：同一份图片
    上传多次只占一份磁盘空间，media_id 恒为内容哈希。
    """

    def __init__(self, media_dir: str | os.PathLike[str]) -> None:
        self.root = Path(media_dir).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        # media_id -> MediaEntry
        self._index: dict[str, MediaEntry] = {}

    @staticmethod
    def compute_media_id(data: bytes) -> str:
        """按内容算 media_id（sha256 十六进制前 32 位）。"""
        return hashlib.sha256(data).hexdigest()[:32]

    def _path_for(self, media_id: str) -> Path:
        return self.root / media_id[:2] / media_id

    def put(self, data: bytes, filename: str = "", mime: str = "") -> MediaEntry:
        """落盘一份媒体本体，返回条目（同内容重复上传会复用已有条目）。"""
        if not data:
            raise ValueError("empty media")
        media_id = self.compute_media_id(data)
        existing = self._index.get(media_id)
        if existing is not None and os.path.exists(existing.path):
            return existing

        path = self._path_for(media_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子改名，避免半截文件被索引到
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

        entry = MediaEntry(
            media_id=media_id,
            path=str(path),
            mime=mime or "",
            size=len(data),
            filename=filename or media_id,
        )
        self._index[media_id] = entry
        return entry

    def get(self, media_id: str) -> MediaEntry | None:
        """按 media_id 取媒体条目。索引丢失时按内容寻址路径回捞。"""
        if not media_id:
            return None
        entry = self._index.get(media_id)
        if entry is not None:
            return entry
        path = self._path_for(media_id)
        if not path.is_file():
            return None
        entry = MediaEntry(
            media_id=media_id,
            path=str(path),
            mime="",
            size=path.stat().st_size,
            filename=media_id,
        )
        self._index[media_id] = entry
        return entry

    def read(self, media_id: str) -> tuple[bytes, MediaEntry] | None:
        """读取媒体本体字节。"""
        entry = self.get(media_id)
        if entry is None:
            return None
        try:
            with open(entry.path, "rb") as f:
                return f.read(), entry
        except OSError as e:
            logger.warning("读取媒体本体失败 media_id=%s: %s", media_id, e)
            return None


class TxruiConnection:
    """一条已认证的 WS 连接。"""

    def __init__(
        self,
        ws: Any,
        connector_id: str,
        account_ids: str = "",
        capabilities: dict | None = None,
        client_info: str = "",
    ) -> None:
        self.ws = ws
        self.connector_id = connector_id
        self.account_ids = account_ids
        self.capabilities: dict = dict(capabilities or {})
        self.client_info = client_info
        self.connected_at = time.time()
        # 写锁：websockets 不保证并发 send() 安全
        self._send_lock = asyncio.Lock()
        # request_id -> Future（等待 send_ack）
        self._pending: dict[str, asyncio.Future] = {}
        # request_id -> 上次结果（幂等重放）
        self._seen_outbound: dict[str, dict] = {}
        self._seen_outbound_max = 500

    async def send_json(self, obj: dict) -> None:
        """串行化写帧。"""
        async with self._send_lock:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))

    def supports(self, capability: str) -> bool:
        """客户端是否声明支持某能力（未声明时保守认为支持）。"""
        if not self.capabilities:
            return True
        return bool(self.capabilities.get(capability, True))

    def register_pending(self, request_id: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut
        return fut

    def resolve_pending(self, request_id: str, result: dict) -> None:
        fut = self._pending.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def fail_pending(self, error: str) -> None:
        """连接断开时把所有未决请求判失败。"""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result({"ok": False, "error": error})
        self._pending.clear()

    def remember_outbound(self, request_id: str, result: dict) -> None:
        if not request_id:
            return
        self._seen_outbound[request_id] = result
        if len(self._seen_outbound) > self._seen_outbound_max:
            # 简单 FIFO 淘汰
            for old in list(self._seen_outbound)[
                : len(self._seen_outbound) - self._seen_outbound_max
            ]:
                self._seen_outbound.pop(old, None)


class TxruiWSServer:
    """txrui connector WebSocket 服务端。

    只做协议层：握手、帧分发、媒体存取、出站下发。
    消息转换与事件入队由 `TxruiAdapter` 负责。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8788,
        token: str = "",
        connector_id: str = "txrui_001",
        account_id: str = "wx_001",
        media_dir: str = "data/txrui_media",
        on_event: Any = None,
        max_media_bytes: int = MAX_MEDIA_BYTES,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        send_ack_timeout: float = SEND_ACK_TIMEOUT,
        handshake_timeout: float = 15.0,
        heartbeat_interval: float = 0.0,
        enable_media: bool = True,
        debug_log: bool = False,
        log_payload: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self.connector_id = connector_id
        self.account_id = account_id
        self.media_store = MediaStore(media_dir)
        # on_event: async (payload: dict, conn: TxruiConnection) -> None
        self._on_event = on_event
        self._connections: dict[str, TxruiConnection] = {}
        self._server: Any = None
        self._running = False
        self._conn_seq = 0
        # ---- 可调运行时参数（来自平台配置）----
        self.max_media_bytes = int(max_media_bytes)
        self.max_frame_bytes = int(max_frame_bytes)
        self.send_ack_timeout = float(send_ack_timeout)
        self.handshake_timeout = float(handshake_timeout)
        self.heartbeat_interval = float(heartbeat_interval)
        self.enable_media = bool(enable_media)
        self.debug_log = bool(debug_log)
        self.log_payload = bool(log_payload)
        # 心跳应答节流：记录最近一次应答时间
        self._last_heartbeat_ack = 0.0

    # ---------- 连接管理 ----------

    @property
    def connections(self) -> list[TxruiConnection]:
        return list(self._connections.values())

    def find_connection(
        self, chat_id: str = "", account_id: str = ""
    ) -> TxruiConnection | None:
        """按账号精确路由；同账号重连取最新连接，绝不回退到其他账号。

        chat_id 保留为旧调用方兼容参数，不用于匹配账号声明。
        未声明账号的旧客户端仅在唯一连接且目标为默认账号时兼容。
        """
        target = str(account_id or self.account_id).strip()
        candidates = [
            conn for conn in self._connections.values()
            if target in {part.strip() for part in conn.account_ids.split(",")
                          if part.strip()}
        ]
        if not candidates and len(self._connections) == 1 and target == self.account_id:
            legacy = next(iter(self._connections.values()))
            if not legacy.account_ids.strip():
                candidates = [legacy]
        return max(candidates, key=lambda c: c.connected_at) if candidates else None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动 WS 服务端（不阻塞）。"""
        if self._running:
            return
        try:
            from websockets.asyncio.server import serve
        except ImportError:  # pragma: no cover - 老版本 websockets 兜底
            from websockets.server import serve  # type: ignore[no-redef]

        try:
            self._server = await serve(
                self._handler,
                self.host,
                self.port,
                max_size=self.max_frame_bytes,
            )
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                raise OSError(
                    f"txrui WebSocket 服务端无法绑定 {self.host}:{self.port}："
                    "端口仍被占用（可能属于当前 AstrBot 进程中的重复 txrui 实例）。"
                    "插件不会自动结束其他进程；请检查重复实例或其他服务是否占用该端口。"
                ) from e
            if e.errno in (errno.EACCES, errno.EPERM):
                raise OSError(
                    f"txrui WebSocket 服务端启动失败：当前运行用户无权监听 "
                    f"{self.host}:{self.port}，请检查端口权限和安全策略。"
                ) from e
            raise OSError(
                f"txrui WebSocket 服务端启动失败 ({self.host}:{self.port}): {e}"
            ) from e
        self._running = True
        logger.info("txrui WS 服务端已监听 %s:%s", self.host, self.port)

    async def stop(self) -> None:
        """停止接受新连接、关闭活动连接并释放监听端口。"""
        self._running = False
        server, self._server = self._server, None
        if server is not None:
            # 先关闭监听 socket，避免停止期间继续接受连接；wait_closed 确认
            # 底层 socket 已关闭后，平台重载才可安全重新绑定同一端口。
            server.close()
        for conn in list(self._connections.values()):
            conn.fail_pending("server_stopped")
            try:
                await conn.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._connections.clear()
        if server is not None:
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                logger.exception("txrui WebSocket 服务端等待关闭时发生错误")
        logger.info("txrui WS 服务端已关闭并释放监听端口 %s:%s", self.host, self.port)

    # ---------- 握手 ----------

    def _verify_hello(self, hello: dict) -> None:
        """校验 hello 帧的 connector_id 与 token。

        token 与 connector_id **都要**匹配才算通过。不通过抛
        `TxruiProtocolError`。
        """
        got_connector = str(hello.get("connector_id") or "")
        got_token = str(hello.get("token") or "")
        if self.connector_id and got_connector != self.connector_id:
            raise TxruiProtocolError(
                f"connector_id mismatch: got {got_connector!r}, want {self.connector_id!r}"
            )
        if self.token and got_token != self.token:
            raise TxruiProtocolError("token mismatch")

    async def _handle_hello_frame(self, ws: Any, hello: dict) -> TxruiConnection:
        """校验 hello 帧、登记连接并回 hello_ack。

        校验不通过抛 `TxruiProtocolError`，由调用方关闭连接。
        """
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise TxruiProtocolError("first frame must be hello")
        self._verify_hello(hello)

        self._conn_seq += 1
        conn_id = f"c{self._conn_seq}"
        conn = TxruiConnection(
            ws,
            connector_id=str(hello.get("connector_id") or ""),
            account_ids=str(hello.get("account_ids") or ""),
            capabilities=hello.get("capabilities") or {},
            client_info=str(hello.get("client_info") or ""),
        )
        self._connections[conn_id] = conn
        await conn.send_json({"type": "hello_ack", "protocol_version": PROTOCOL_VERSION})
        logger.info(
            "txrui 连接已建立 id=%s connector_id=%s account_ids=%s",
            conn_id,
            conn.connector_id,
            conn.account_ids,
        )
        return conn

    async def _handler(self, ws: Any) -> None:
        """单连接的帧处理循环。"""
        conn_id = ""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=self.handshake_timeout)
            hello = json.loads(raw)
            conn = await self._handle_hello_frame(ws, hello)
            conn_id = next(
                (k for k, v in self._connections.items() if v is conn),
                "",
            )

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    logger.warning("txrui 收到非法 JSON 帧，忽略")
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    await self._dispatch(conn, msg)
                except Exception as e:  # noqa: BLE001
                    logger.exception("txrui 帧处理失败 type=%s: %s", msg.get("type"), e)
        except TxruiProtocolError as e:
            logger.warning("txrui 握手被拒: %s", e)
            try:
                await ws.send(
                    json.dumps({"type": "hello_reject", "error": str(e)[:200]}, ensure_ascii=False)
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                await ws.close(code=1008, reason="handshake rejected")
            except Exception:  # noqa: BLE001
                pass
        except asyncio.TimeoutError:
            logger.warning("txrui 握手超时（未在 15s 内发送 hello）")
        except Exception as e:  # noqa: BLE001
            logger.exception("txrui 连接异常: %s", e)
        finally:
            if conn_id and conn_id in self._connections:
                conn = self._connections.pop(conn_id)
                conn.fail_pending("disconnected")
                logger.info("txrui 连接已断开 id=%s", conn_id)

    # ---------- 帧分发 ----------

    async def _dispatch(self, conn: TxruiConnection, msg: dict) -> None:
        mtype = str(msg.get("type") or "")
        if mtype == "heartbeat":
            await self._handle_heartbeat(conn)
            return
        if mtype == "event":
            await self._handle_event(conn, msg)
            return
        if mtype == "media_upload":
            await self._handle_media_upload(conn, msg)
            return
        if mtype == "media_fetch":
            await self._handle_media_fetch(conn, msg)
            return
        if mtype == "send_ack":
            self._handle_send_ack(conn, msg)
            return
        if mtype == "ack":
            # core 语义（入库确认），AstrBot 侧不需要，忽略即可
            return
        logger.debug("txrui 收到未知帧类型: %s", mtype)

    async def _handle_heartbeat(self, conn: TxruiConnection) -> None:
        """应答心跳。

        `heartbeat_interval` > 0 时做节流：间隔内的重复心跳直接忽略，
        避免客户端高频心跳把服务端写锁占满。
        """
        if self.heartbeat_interval > 0:
            now = time.time()
            if now - self._last_heartbeat_ack < self.heartbeat_interval:
                return
            self._last_heartbeat_ack = now
        await conn.send_json({"type": "heartbeat_ack", "ts": int(time.time())})

    async def _handle_event(self, conn: TxruiConnection, msg: dict) -> None:
        """回执仅确认协议接收/适配器入队，不表示机器人处理或回复完成。"""
        payload = msg.get("payload")
        ack = {"type": "ack", "event_id": "", "client_seq": "",
               "ok": False, "status": "rejected", "error": ""}
        if not isinstance(payload, dict):
            ack["error"] = "invalid_payload"
        else:
            ack["event_id"] = str(payload.get("event_id") or "")
            ack["client_seq"] = str(payload.get("client_seq") or "")
            if self._on_event is None:
                ack["error"] = "no_event_handler"
            else:
                try:
                    result = self._on_event(payload, conn)
                    if asyncio.iscoroutine(result):
                        result = await result
                    if isinstance(result, dict):
                        ack.update({key: result[key] for key in ("ok", "status", "error")
                                    if key in result})
                    else:
                        # 兼容旧回调：返回 None 只确认已交给回调，不谎称已入队。
                        ack.update(ok=True, status="received")
                except Exception:  # noqa: BLE001
                    logger.exception("txrui event 处理失败")
                    ack.update(ok=False, status="failed", error="event_handler_failed")
        await conn.send_json(ack)

    async def _handle_media_upload(self, conn: TxruiConnection, msg: dict) -> None:
        if not self.enable_media:
            await conn.send_json(
                {
                    "type": "media_ack",
                    "client_seq": str(msg.get("client_seq") or ""),
                    "ok": False,
                    "media_id": "",
                    "mime": "",
                    "size": 0,
                    "error": "media_disabled",
                }
            )
            return
        client_seq = str(msg.get("client_seq") or "")
        data_b64 = str(msg.get("data_b64") or "")
        filename = str(msg.get("filename") or "media")
        mime = str(msg.get("mime") or "")
        try:
            data = base64.b64decode(data_b64) if data_b64 else b""
        except Exception:  # noqa: BLE001
            data = b""
        if not data:
            await conn.send_json(
                {
                    "type": "media_ack",
                    "client_seq": client_seq,
                    "ok": False,
                    "media_id": "",
                    "mime": mime,
                    "size": 0,
                    "error": "empty_media",
                }
            )
            return
        if len(data) > self.max_media_bytes:
            await conn.send_json(
                {
                    "type": "media_ack",
                    "client_seq": client_seq,
                    "ok": False,
                    "media_id": "",
                    "mime": mime,
                    "size": len(data),
                    "error": "too_large",
                }
            )
            return
        try:
            entry = self.media_store.put(data, filename=filename, mime=mime)
        except OSError as e:
            await conn.send_json(
                {
                    "type": "media_ack",
                    "client_seq": client_seq,
                    "ok": False,
                    "media_id": "",
                    "mime": mime,
                    "size": len(data),
                    "error": f"storage_failed:{str(e)[:100]}",
                }
            )
            return
        await conn.send_json(
            {
                "type": "media_ack",
                "client_seq": client_seq,
                "ok": True,
                "media_id": entry.media_id,
                "mime": entry.mime or mime,
                "size": entry.size,
                "error": "",
            }
        )

    async def _handle_media_fetch(self, conn: TxruiConnection, msg: dict) -> None:
        client_seq = str(msg.get("client_seq") or "")
        media_id = str(msg.get("media_id") or "")
        found = self.media_store.read(media_id)
        if found is None:
            await conn.send_json(
                {
                    "type": "media_fetch_ack",
                    "client_seq": client_seq,
                    "ok": False,
                    "media_id": media_id,
                    "mime": "",
                    "filename": "",
                    "size": 0,
                    "data_b64": "",
                    "error": "media_not_found",
                }
            )
            return
        data, entry = found
        await conn.send_json(
            {
                "type": "media_fetch_ack",
                "client_seq": client_seq,
                "ok": True,
                "media_id": entry.media_id,
                "mime": entry.mime,
                "filename": entry.filename,
                "size": entry.size,
                "data_b64": base64.b64encode(data).decode("ascii"),
                "error": "",
            }
        )

    def _handle_send_ack(self, conn: TxruiConnection, msg: dict) -> None:
        request_id = str(msg.get("request_id") or "")
        result = {
            "ok": bool(msg.get("ok", False)),
            "unknown": bool(msg.get("unknown", False)),
            "platform_message_id": str(msg.get("platform_message_id") or ""),
            "error": str(msg.get("error") or ""),
        }
        conn.resolve_pending(request_id, result)

    # ---------- 出站 ----------

    async def send_outbound(
        self,
        chat_id: str,
        content: list[dict],
        account_id: str = "",
        request_id: str = "",
        chat_type: str = "",
    ) -> dict:
        """下发一条 outbound 指令并等待 send_ack。

        返回 send_ack 的结果 dict（超时/失败时 `ok=False`）。
        """
        conn = self.find_connection(chat_id, account_id=account_id)
        if conn is None:
            return {"ok": False, "error": "no_connection", "unknown": False}
        if not content:
            return {"ok": False, "error": "empty_content", "unknown": False}

        rid = request_id or f"out_{uuid.uuid4().hex[:16]}"
        # 幂等：同一个 request_id 重复下发时，不重复发，直接回放上次结果
        if rid in conn._seen_outbound:
            return dict(conn._seen_outbound[rid])

        fut = conn.register_pending(rid)
        payload = {
            "action": "message.send",
            "account_id": account_id or self.account_id,
            "chat_id": chat_id,
            "content": content,
        }
        normalized_type = str(chat_type or "").strip().lower()
        if normalized_type in ("group", "private"):
            payload["chat_type"] = normalized_type
        try:
            await conn.send_json(
                {"type": "outbound", "request_id": rid, "payload": payload}
            )
        except Exception as e:  # noqa: BLE001
            conn._pending.pop(rid, None)
            return {"ok": False, "unknown": True, "error": f"send_failed:{str(e)[:120]}"}

        try:
            result = await asyncio.wait_for(
                asyncio.shield(fut), timeout=self.send_ack_timeout
            )
        except asyncio.TimeoutError:
            conn._pending.pop(rid, None)
            return {"ok": False, "unknown": True, "error": "timeout_unknown"}
        except Exception as e:  # noqa: BLE001
            conn._pending.pop(rid, None)
            return {"ok": False, "unknown": True, "error": f"ack_failed:{str(e)[:80]}"}

        conn.remember_outbound(rid, result)
        return result


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_MEDIA_BYTES",
    "OUTBOUND_SEGMENT_TYPES",
    "PROTOCOL_VERSION",
    "MediaEntry",
    "MediaStore",
    "TxruiConnection",
    "TxruiProtocolError",
    "TxruiWSServer",
]
