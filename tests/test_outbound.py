"""出站测试：outbound 帧生成 / send_ack 回填 / request_id 幂等。

覆盖：
- `send_by_session` 生成正确的 outbound 帧（request_id + payload.content[]）
- `send_ack` 按 request_id 回填 Future
- request_id 幂等：重复下发同一 request_id 不重复发，复用上次结果
- `TxruiEvent.send`：Plain -> text 段；媒体 -> 服务端落盘 + outbound 带 media_id
- 语音（Record）如实降级为 file 段并标 extra.file_fallback
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.platform import MessageType

try:
    from astrbot.api.platform import MessageSession
except ImportError:
    from astrbot.core.platform.message_session import MessageSession

from astrbot_plugin_txrui.txrui_adapter import TxruiAdapter
from astrbot_plugin_txrui.txrui_client import TxruiConnection
from astrbot_plugin_txrui.txrui_event import TxruiEvent, TxruiSendError


def _run(coro):
    return asyncio.run(coro)


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True

    async def recv(self):
        raise RuntimeError("eof")

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _adapter(tmp_path, queue=None):
    return TxruiAdapter(
        {
            "id": "txrui_default",
            "enable": False,
            "host": "127.0.0.1",
            "port": 8788,
            "token": "tok",
            "connector_id": "txrui_001",
            "account_id": "wx_001",
            "media_dir": str(tmp_path / "media"),
        },
        {},
        queue if queue is not None else asyncio.Queue(),
    )


async def _register_conn(adapter):
    """在适配器的服务端上登记一条已认证连接（跳过真实握手）。"""
    ws = _FakeWS()
    conn = TxruiConnection(
        ws,
        connector_id="txrui_001",
        account_ids="wx_001",
        capabilities={"text": True, "image": True, "file": True, "video": True, "audio": False},
    )
    adapter.server._connections["c1"] = conn
    return conn


async def _server_ack(server, conn, request_id, ok=True, platform_message_id="", error=""):
    """模拟客户端回 send_ack：走服务端的帧分发（而不是直接往客户端发帧）。"""
    await server._dispatch(
        conn,
        {
            "type": "send_ack",
            "request_id": request_id,
            "ok": ok,
            "platform_message_id": platform_message_id,
            "error": error,
        },
    )


class TestSendOutbound:
    def test_outbound_frame_shape(self, tmp_path):
        """outbound 帧形状正确：type/request_id/payload。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)

            async def ack_later():
                await asyncio.sleep(0.05)
                outbound = next(
                    f for f in conn.ws.sent if f.get("type") == "outbound"
                )
                await _server_ack(
                    adapter.server,
                    conn,
                    outbound["request_id"],
                    ok=True,
                    platform_message_id="wx-1",
                )

            task = asyncio.create_task(ack_later())
            result = await adapter.server.send_outbound(
                "群A",
                [{"type": "text", "text": "hi"}],
                account_id="wx_001",
            )
            await task
            assert result["ok"] is True
            assert result["platform_message_id"] == "wx-1"

            outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
            assert outbound["type"] == "outbound"
            assert outbound["request_id"]
            payload = outbound["payload"]
            assert payload["action"] == "message.send"
            assert payload["chat_id"] == "群A"
            assert payload["account_id"] == "wx_001"
            assert payload["content"] == [{"type": "text", "text": "hi"}]

        _run(scenario())

    def test_send_ack_resolves_future(self, tmp_path):
        """send_ack 按 request_id 回填 Future。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)

            async def ack_later():
                await asyncio.sleep(0.05)
                outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
                await _server_ack(adapter.server, conn, outbound["request_id"], ok=True)

            task = asyncio.create_task(ack_later())
            result = await adapter.server.send_outbound("c", [{"type": "text", "text": "x"}])
            await task
            assert result["ok"] is True
            # Future 已被移除
            assert conn._pending == {}

        _run(scenario())

    def test_request_id_idempotent(self, tmp_path):
        """同一 request_id 重复下发：不重复发，复用上次结果。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)

            async def ack_later():
                await asyncio.sleep(0.05)
                outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
                await _server_ack(
                    adapter.server,
                    conn,
                    outbound["request_id"],
                    ok=True,
                    platform_message_id="wx-9",
                )

            task = asyncio.create_task(ack_later())
            first = await adapter.server.send_outbound(
                "c", [{"type": "text", "text": "x"}], request_id="fixed-rid"
            )
            await task
            assert first["ok"] is True

            outbounds_before = len([f for f in conn.ws.sent if f.get("type") == "outbound"])

            # 第二次用同一 request_id
            second = await adapter.server.send_outbound(
                "c", [{"type": "text", "text": "x"}], request_id="fixed-rid"
            )
            assert second["ok"] is True
            assert second["platform_message_id"] == "wx-9"

            outbounds_after = len([f for f in conn.ws.sent if f.get("type") == "outbound"])
            assert outbounds_after == outbounds_before  # 没有重复发送

        _run(scenario())

    def test_no_connection_returns_failure(self, tmp_path):
        """没有连接时如实回失败（不谎报成功）。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            result = await adapter.server.send_outbound("c", [{"type": "text", "text": "x"}])
            assert result["ok"] is False
            assert result["error"] == "no_connection"

        _run(scenario())

    def test_media_fetch_round_trip(self, tmp_path):
        """出站媒体闭环：服务端落盘 -> outbound 带 media_id -> 客户端 media_fetch 取回字节。

        这是 AstrBot 发图给微信的关键路径：txrui 侧收到 outbound 后会发
        `media_fetch`，服务端必须能把本体原样吐回来。
        """

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)

            # 1) 服务端落盘一份媒体（模拟出站图片）
            payload = b"\x89PNG-round-trip-bytes"
            entry = adapter.server.media_store.put(
                payload, filename="rt.png", mime="image/png"
            )

            # 2) 客户端发起 media_fetch
            await adapter.server._handle_media_fetch(
                conn,
                {
                    "type": "media_fetch",
                    "client_seq": "f1",
                    "media_id": entry.media_id,
                },
            )
            ack = conn.ws.sent[-1]
            assert ack["type"] == "media_fetch_ack"
            assert ack["client_seq"] == "f1"
            assert ack["ok"] is True
            assert ack["media_id"] == entry.media_id
            assert ack["filename"] == "rt.png"
            assert base64.b64decode(ack["data_b64"]) == payload

        _run(scenario())

    def test_empty_content_returns_failure(self, tmp_path):
        """空内容 -> 失败。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            await _register_conn(adapter)
            result = await adapter.server.send_outbound("c", [])
            assert result["ok"] is False
            assert result["error"] == "empty_content"

        _run(scenario())


class TestSendBySession:
    def test_send_by_session_emits_outbound(self, tmp_path):
        """send_by_session 生成 outbound 帧并等 send_ack。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)

            async def ack_later():
                await asyncio.sleep(0.05)
                outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
                await _server_ack(adapter.server, conn, outbound["request_id"], ok=True)

            task = asyncio.create_task(ack_later())
            session = MessageSession(
                platform_name="txrui_default",
                message_type=MessageType.GROUP_MESSAGE,
                session_id="群A",
            )
            await adapter.send_by_session(session, MessageChain([Plain("你好")]))
            await task

            outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
            assert outbound["payload"]["chat_id"] == "群A"
            assert outbound["payload"]["content"] == [{"type": "text", "text": "你好"}]

        _run(scenario())


class TestEventSend:
    def _make_event(self, adapter, conn, tmp_path):
        msg = adapter.convert_message(
            {
                "chat_id": "群A",
                "chat_type": "group",
                "sender": {"platform_user_id": "u", "display_name": "u"},
                "content": [{"type": "text", "text": "hi"}],
            }
        )
        return TxruiEvent(
            message_str=msg.message_str,
            message_obj=msg,
            platform_meta=adapter.meta(),
            session_id=msg.session_id,
            server=adapter.server,
            chat_id="群A",
            account_id="wx_001",
            capabilities=conn.capabilities,
        )

    def test_plain_becomes_text_segment(self, tmp_path):
        """Plain -> outbound 的 text 段。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)
            event = self._make_event(adapter, conn, tmp_path)

            async def ack_later():
                await asyncio.sleep(0.08)
                outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
                await _server_ack(adapter.server, conn, outbound["request_id"], ok=True)

            task = asyncio.create_task(ack_later())
            await event.send(MessageChain([Plain("回复内容")]))
            await task

            outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
            assert outbound["payload"]["content"] == [{"type": "text", "text": "回复内容"}]

        _run(scenario())

    def test_image_is_stored_and_referenced_by_media_id(self, tmp_path):
        """Image -> 服务端落盘拿 media_id，outbound 段里引用该 media_id。

        客户端收到 outbound 后会走 `media_fetch` 取回本体，所以服务端必须
        确实存有这份媒体。
        """

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)
            event = self._make_event(adapter, conn, tmp_path)

            img_path = tmp_path / "pic.png"
            img_path.write_bytes(b"\x89PNG-payload")

            async def responder():
                await asyncio.sleep(0.05)
                outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
                await _server_ack(adapter.server, conn, outbound["request_id"], ok=True)

            task = asyncio.create_task(responder())
            await event.send(MessageChain([Image.fromFileSystem(str(img_path))]))
            await task

            outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
            seg = outbound["payload"]["content"][0]
            assert seg["type"] == "image"
            assert seg["media_id"]
            assert seg["mime"] == "image/png"
            assert seg["url"] == f"/api/v1/media/blobs/{seg['media_id']}"
            assert seg["text"] == "pic.png"

            # 客户端会用 media_fetch 取回本体 —— 服务端必须存得住
            stored = adapter.server.media_store.get(seg["media_id"])
            assert stored is not None
            assert Path(stored.path).read_bytes() == b"\x89PNG-payload"

        _run(scenario())

    def test_record_degrades_to_file_with_fallback_flag(self, tmp_path):
        """语音（Record）-> 如实降级为 file 段，extra.file_fallback=True。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)
            event = self._make_event(adapter, conn, tmp_path)

            audio_path = tmp_path / "a.wav"
            audio_path.write_bytes(b"RIFF-wave-bytes")

            async def responder():
                await asyncio.sleep(0.05)
                outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
                await _server_ack(adapter.server, conn, outbound["request_id"], ok=True)

            task = asyncio.create_task(responder())
            await event.send(MessageChain([Record.fromFileSystem(str(audio_path))]))
            await task

            outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
            seg = outbound["payload"]["content"][0]
            assert seg["type"] == "file"  # 不是 audio（客户端不支持真语音）
            assert seg["extra"]["file_fallback"] is True
            assert seg["extra"]["original_type"] == "audio"

        _run(scenario())

    def test_unreadable_media_does_not_reference_fake_media_id(self, tmp_path):
        """媒体不可读时不引用任何 media_id（降级为文本说明）。"""

        async def scenario():
            adapter = _adapter(tmp_path)
            conn = await _register_conn(adapter)
            event = self._make_event(adapter, conn, tmp_path)

            missing = tmp_path / "missing.png"

            async def responder():
                await asyncio.sleep(0.08)
                outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
                await _server_ack(adapter.server, conn, outbound["request_id"], ok=True)

            task = asyncio.create_task(responder())
            with pytest.raises(TxruiSendError, match="unreadable_media"):
                await event.send(MessageChain([Image.fromFileSystem(str(missing))]))
            await task

            outbound = next(f for f in conn.ws.sent if f.get("type") == "outbound")
            seg = outbound["payload"]["content"][0]
            assert seg["type"] == "text"
            assert "media_id" not in seg
            assert "发送失败" in seg["text"]

        _run(scenario())


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
