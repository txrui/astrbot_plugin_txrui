"""AstrBot 标准适配器接口回归测试，无需微信客户端。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot_plugin_txrui.txrui_event import TxruiEvent, TxruiSendError
from test_outbound import MessageSession, MessageType, _adapter, _register_conn


def _message(adapter, **overrides):
    payload = {
        "chat_id": "会话A",
        "chat_type": "private",
        "sender": {"platform_user_id": "u1"},
        "content": [{"type": "text", "text": "你好"}],
    }
    payload.update(overrides)
    return adapter.convert_message(payload)


def test_get_client_returns_protocol_server(tmp_path):
    adapter = _adapter(tmp_path)
    assert adapter.get_client() is adapter.server


def test_create_event_preserves_context(tmp_path):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        message = _message(adapter, account_id="wx_inbound")
        event = adapter.create_event(message, connection=conn)
        assert isinstance(event, TxruiEvent)
        assert event.message_obj is message
        assert message.self_id == "wx_inbound"
        assert event.account_id == "wx_inbound"
        assert event.chat_id == message.session_id == "会话A"
        assert event.server is adapter.get_client()
        assert event.capabilities == conn.capabilities
        assert event.enable_media == adapter.enable_media
        assert event.media_fallback_to_text == adapter.media_fallback_to_text
        assert event.record_as_file == adapter.record_as_file
        assert event.get_platform_name() == "txrui"
    asyncio.run(scenario())


def test_framework_created_event_sends_real_image(tmp_path, monkeypatch):
    async def scenario():
        adapter = _adapter(tmp_path)
        await _register_conn(adapter)
        message = _message(adapter)
        message.raw_message = None  # 框架注入的消息不必带采集端原始载荷。
        event = adapter.create_event(message)
        path = tmp_path / "framework.png"
        path.write_bytes(b"framework-image")
        send = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(adapter.server, "send_outbound", send)
        monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
        await event.send(MessageChain([Image.fromFileSystem(str(path))]))
        send.assert_awaited_once()
        assert send.call_args.args[0] == "会话A"
        segment = send.call_args.args[1][0]
        assert segment["type"] == "image"
        data, _ = adapter.server.media_store.read(segment["media_id"])
        assert data == b"framework-image"
    asyncio.run(scenario())


def test_inbound_uses_event_factory(tmp_path, monkeypatch):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        create = adapter.create_event
        calls = []
        def factory(message, **kwargs):
            calls.append(kwargs)
            return create(message, **kwargs)
        monkeypatch.setattr(adapter, "create_event", factory)
        await adapter._on_event({
            "chat_id": "会话A", "chat_type": "private",
            "content": [{"type": "text", "text": "你好"}],
        }, conn)
        event = adapter._event_queue.get_nowait()
        assert isinstance(event, TxruiEvent)
        assert calls == [{"connection": conn}]
    asyncio.run(scenario())


@pytest.mark.parametrize("chat_id,message_type,error", [
    ("", MessageType.FRIEND_MESSAGE, "missing_chat_id"),
    ("会话A", MessageType.OTHER_MESSAGE, "unsupported_session_type"),
])
def test_invalid_proactive_session_rejected(tmp_path, monkeypatch, chat_id, message_type, error):
    async def scenario():
        adapter = _adapter(tmp_path)
        send = AsyncMock()
        monkeypatch.setattr(adapter.server, "send_outbound", send)
        session = MessageSession("txrui_default", message_type, chat_id)
        with pytest.raises(TxruiSendError, match=error):
            await adapter.send_by_session(session, MessageChain([Plain("hello")]))
        send.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("message_type", [MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE])
def test_proactive_send_uses_event_factory(tmp_path, monkeypatch, message_type):
    async def scenario():
        adapter = _adapter(tmp_path)
        await _register_conn(adapter)
        created = []
        factory = adapter.create_event
        def create(message):
            event = factory(message)
            created.append(event)
            return event
        monkeypatch.setattr(adapter, "create_event", create)
        send = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(adapter.server, "send_outbound", send)
        session = MessageSession("txrui_default", message_type, "会话A")
        await adapter.send_by_session(session, MessageChain([Plain("hello")]))
        assert len(created) == 1
        assert created[0].message_obj.type == message_type
        assert created[0].capabilities["image"] is True
        send.assert_awaited_once()
    asyncio.run(scenario())


def test_create_event_rejects_missing_route(tmp_path):
    adapter = _adapter(tmp_path)
    message = _message(adapter)
    message.session_id = ""
    with pytest.raises(TxruiSendError, match="missing_chat_id"):
        adapter.create_event(message)
