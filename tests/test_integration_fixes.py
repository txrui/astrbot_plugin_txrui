"""群聊类型、接收回执、表情图片和视频预览的适配器回归测试。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain, Video
from astrbot.api.platform import MessageType
from test_outbound import MessageSession, _adapter, _register_conn


def _payload(**patch):
    payload = {
        "event_id": "evt_test", "client_seq": "seq_test", "account_id": "wx_001",
        "chat_id": "群A", "chat_type": "group",
        "sender": {"platform_user_id": "member", "display_name": "成员"},
        "content": [{"type": "text", "text": "你好"}],
    }
    payload.update(patch)
    return payload


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("chat_type,message_type", [
    ("group", MessageType.GROUP_MESSAGE),
    ("private", MessageType.FRIEND_MESSAGE),
])
def test_reply_and_proactive_send_preserve_type(tmp_path, monkeypatch, active, chat_type, message_type):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        send = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(adapter.server, "send_outbound", send)
        monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
        chain = MessageChain([Plain("回复")])
        if active:
            await adapter.send_by_session(MessageSession("txrui_default", message_type, "群A"), chain)
        else:
            event = adapter.create_event(adapter.convert_message(_payload(chat_type=chat_type)), connection=conn)
            await event.send(chain)
        assert send.call_args.kwargs["chat_type"] == chat_type
        assert send.call_args.kwargs["account_id"] == "wx_001"
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,status,ok", [
    ("valid", "queued", True), ("blocked", "filtered", True),
    ("invalid", "rejected", False), ("full", "failed", False),
])
def test_adapter_event_ack_status(tmp_path, mode, status, ok):
    async def scenario():
        adapter = _adapter(tmp_path, asyncio.Queue(maxsize=1))
        conn = await _register_conn(adapter)
        payload = _payload()
        if mode == "blocked":
            adapter.blocked_chat_ids.add("群A")
        elif mode == "invalid":
            payload["chat_id"] = ""
        elif mode == "full":
            adapter._event_queue.put_nowait(object())
        await adapter.server._dispatch(conn, {"type": "event", "payload": payload})
        ack = conn.ws.sent[-1]
        assert ack["type"] == "ack"
        assert ack["event_id"] == "evt_test"
        assert ack["client_seq"] == "seq_test"
        assert ack["status"] == status
        assert ack["ok"] is ok
        if mode == "valid":
            assert adapter._event_queue.qsize() == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("placeholder", [False, True])
def test_text_emoji_uses_real_image_or_placeholder(tmp_path, placeholder):
    adapter = _adapter(tmp_path)
    entry = adapter.server.media_store.put(b"\x89PNG\r\n\x1a\nemoji", "emoji.png", "image/png")
    seg = {"type": "text", "text": "[表情]", "media_id": entry.media_id,
           "extra": {"emoji": True, "placeholder": placeholder}}
    message = adapter.convert_message(_payload(content=[seg]))
    assert isinstance(message.message[0], Plain if placeholder else Image)
    assert message.message_str == "[表情]"


@pytest.mark.parametrize("legacy", [False, True])
def test_video_preview_is_image(tmp_path, legacy):
    adapter = _adapter(tmp_path)
    entry = adapter.server.media_store.put(b"\x89PNG\r\n\x1a\npreview", "old.mp4" if legacy else "video_preview.png", "video/mp4" if legacy else "image/png")
    seg = {"type": "video" if legacy else "image", "media_id": entry.media_id,
           "extra": {} if legacy else {"original_type": "video", "preview": True}}
    message = adapter.convert_message(_payload(content=[seg]))
    assert isinstance(message.message[0], Image)
    assert message.message_str == "[视频预览]"


def test_real_video_remains_video(tmp_path):
    adapter = _adapter(tmp_path)
    entry = adapter.server.media_store.put(b"\x00\x00\x00\x18ftypmp42real-video", "real.mp4", "video/mp4")
    message = adapter.convert_message(_payload(content=[{"type": "video", "media_id": entry.media_id}]))
    assert isinstance(message.message[0], Video)
    assert message.message_str == "[视频]"
