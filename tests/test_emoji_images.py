"""表情图片只在 txrui 适配器内处理，不修改其他插件。"""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image as PILImage

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot_plugin_txrui.txrui_adapter import TxruiAdapter


def adapter_for(tmp_path):
    return TxruiAdapter(
        {"id": "txrui_test", "host": "127.0.0.1", "token": "test",
         "media_dir": str(tmp_path / "media")}, {}, asyncio.Queue()
    )


def gif_bytes():
    stream = BytesIO()
    frames = [PILImage.new("RGB", (2, 2), color) for color in ("red", "blue")]
    frames[0].save(stream, format="GIF", save_all=True,
                   append_images=frames[1:], duration=100, loop=0)
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["image", "emoji", "sticker"])
def test_inbound_emoji_is_an_image(tmp_path, kind):
    adapter = adapter_for(tmp_path)
    entry = adapter.server.media_store.put(gif_bytes(), filename="happy.gif", mime="image/gif")
    message = adapter.convert_message({
        "chat_id": "friend", "chat_type": "private",
        "content": [{"type": kind, "media_id": entry.media_id}],
    })
    assert isinstance(message.message[0], Image)
    assert message.message_str == ("[图片]" if kind == "image" else "[表情]")


@pytest.mark.parametrize("kind", ["emoji", "sticker"])
def test_missing_emoji_stays_a_placeholder(tmp_path, kind):
    adapter = adapter_for(tmp_path)
    message = adapter.convert_message({
        "chat_id": "friend", "content": [{"type": kind, "media_id": "missing"}],
    })
    assert isinstance(message.message[0], Plain)
    assert message.message[0].text == "[表情]"


@pytest.mark.parametrize("source", ["local", "uri", "base64", "data"])
def test_gif_original_bytes_and_real_format(tmp_path, source):
    async def scenario():
        adapter = adapter_for(tmp_path)
        data = gif_bytes()
        path = tmp_path / "happy.png"  # 故意错误的后缀
        path.write_bytes(data)
        encoded = base64.b64encode(data).decode()
        image = {
            "local": lambda: Image(file=str(path)),
            "uri": lambda: Image.fromFileSystem(str(path)),
            "base64": lambda: Image.fromBase64(encoded),
            "data": lambda: Image(file=f"data:image/gif;base64,{encoded}"),
        }[source]()
        adapter.server.send_outbound = AsyncMock(return_value={"ok": True})
        message = adapter.convert_message({"chat_id": "friend", "chat_type": "private"})
        event = adapter.create_event(message)
        await event._send_outbound_message(MessageChain([image]))
        segment = adapter.server.send_outbound.call_args.args[1][0]
        assert segment["type"] == "image"
        assert segment["mime"] == "image/gif"
        assert segment["text"].endswith(".gif")
        entry = adapter.server.media_store.get(segment["media_id"])
        assert Path(entry.path).read_bytes() == data
        assert entry.filename == segment["text"]
        with PILImage.open(entry.path) as stored:
            assert stored.n_frames == 2
    asyncio.run(scenario())


def test_local_image_does_not_use_framework_conversion(tmp_path, monkeypatch):
    async def scenario():
        adapter = adapter_for(tmp_path)
        path = tmp_path / "happy.gif"
        path.write_bytes(gif_bytes())
        converter = AsyncMock(side_effect=AssertionError("must preserve local image"))
        monkeypatch.setattr(Image, "convert_to_file_path", converter)
        event = adapter.create_event(adapter.convert_message({"chat_id": "friend"}))
        resolved, filename = await event._resolve_media_path(Image.fromFileSystem(str(path)))
        assert resolved == str(path)
        assert filename == "happy.gif"
        converter.assert_not_awaited()
    asyncio.run(scenario())
