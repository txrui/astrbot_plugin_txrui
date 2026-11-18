"""发送结果必须如实反馈，主动发送必须传输媒体本体。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot_plugin_txrui.txrui_event import TxruiSendError
from test_outbound import (
    MessageSession,
    MessageType,
    TestEventSend as _TestEventSend,
    _adapter,
    _register_conn,
    _server_ack,
)


def _event(adapter, conn, tmp_path):
    return _TestEventSend()._make_event(adapter, conn, tmp_path)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("error,unknown", [
    ("paste_failed", False),
    ("no_connection", False),
    ("timeout_unknown", True),
    ("disconnected", False),
])
def test_failed_delivery_raises(tmp_path, monkeypatch, active, error, unknown):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        send = AsyncMock(return_value={"ok": False, "error": error, "unknown": unknown})
        monkeypatch.setattr(adapter.server, "send_outbound", send)
        metric = AsyncMock()
        monkeypatch.setattr(AstrMessageEvent, "send", metric)
        message = MessageChain([Plain("hello")])
        with pytest.raises(TxruiSendError, match=error) as caught:
            if active:
                session = MessageSession("txrui_default", MessageType.GROUP_MESSAGE, "群A")
                await adapter.send_by_session(session, message)
            else:
                await _event(adapter, conn, tmp_path).send(message)
        assert caught.value.unknown is unknown
        assert send.await_count == 1  # 结果未知也不自动重试。
        metric.assert_not_awaited()
    asyncio.run(scenario())


def test_no_server_raises(tmp_path):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        event = _event(adapter, conn, tmp_path)
        event.server = None
        with pytest.raises(TxruiSendError, match="no_server"):
            await event.send(MessageChain([Plain("hello")]))
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing", "storage", "disabled", "record_disabled"])
@pytest.mark.parametrize("fallback", [False, True])
def test_media_failure_never_reports_success(tmp_path, monkeypatch, failure, fallback):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        event = _event(adapter, conn, tmp_path)
        event.media_fallback_to_text = fallback
        path = tmp_path / "image.png"
        if failure != "missing":
            path.write_bytes(b"image")
        image = Image.fromFileSystem(str(path))
        if failure == "storage":
            monkeypatch.setattr(event, "_store_media", lambda *args: {
                "ok": False, "error": "storage_failed:disk_full",
            })
        elif failure == "disabled":
            event.enable_media = False
        elif failure == "record_disabled":
            from astrbot.api.message_components import Record
            image = Record.fromFileSystem(str(path))
            event.record_as_file = False
        send = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(adapter.server, "send_outbound", send)
        with pytest.raises(TxruiSendError):
            await event.send(MessageChain([image]))
        if fallback and failure != "record_disabled":
            send.assert_awaited_once()
            assert send.call_args.args[1][0]["type"] == "text"
        else:
            send.assert_not_awaited()
    asyncio.run(scenario())


def test_active_image_sends_real_media(tmp_path):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        path = tmp_path / "active.png"
        path.write_bytes(b"active-image-bytes")
        session = MessageSession("txrui_default", MessageType.GROUP_MESSAGE, "群A")

        async def responder():
            while not conn.ws.sent:
                await asyncio.sleep(0)
            outbound = conn.ws.sent[-1]
            segment = outbound["payload"]["content"][0]
            assert segment["type"] == "image"
            entry = adapter.server.media_store.get(segment["media_id"])
            assert entry is not None
            from pathlib import Path
            assert Path(entry.path).read_bytes() == b"active-image-bytes"
            await _server_ack(adapter.server, conn, outbound["request_id"])

        task = asyncio.create_task(responder())
        try:
            await asyncio.wait_for(adapter.send_by_session(
                session, MessageChain([Image.fromFileSystem(str(path))]),
            ), timeout=2)
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_real_ack_failure_propagates(tmp_path):
    async def scenario():
        adapter = _adapter(tmp_path)
        conn = await _register_conn(adapter)
        event = _event(adapter, conn, tmp_path)

        async def responder():
            while not conn.ws.sent:
                await asyncio.sleep(0)
            outbound = conn.ws.sent[-1]
            await _server_ack(adapter.server, conn, outbound["request_id"],
                              ok=False, error="client_image_paste_failed")

        task = asyncio.create_task(responder())
        try:
            with pytest.raises(TxruiSendError, match="client_image_paste_failed"):
                await asyncio.wait_for(event.send(MessageChain([Plain("hello")])), 2)
            await task
            assert conn._pending == {}
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
