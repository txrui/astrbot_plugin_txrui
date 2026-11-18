"""协议层测试：握手鉴权 + event -> AstrBotMessage 转换。

覆盖：
- hello 握手成功 -> hello_ack（形状必须是 {"type":"hello_ack","protocol_version":"1.0"}）
- connector_id 不符 / token 不符 -> 握手被拒
- event 帧 -> AstrBotMessage：群聊 / 私聊、emoji 标记、媒体占位降级
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest

from astrbot_plugin_txrui.txrui_adapter import TxruiAdapter
from astrbot_plugin_txrui.txrui_client import PROTOCOL_VERSION, TxruiWSServer


def _run(coro):
    return asyncio.run(coro)


class _FakeWS:
    """最小 WebSocket 替身：记录发出的帧。"""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False
        self.close_code: int | None = None

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code

    async def recv(self):
        raise RuntimeError("eof")

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _server(tmp_path, token="tok", connector_id="txrui_001"):
    return TxruiWSServer(
        host="127.0.0.1",
        port=0,
        token=token,
        connector_id=connector_id,
        media_dir=str(tmp_path / "media"),
    )


def _hello(connector_id="txrui_001", token="tok"):
    return {
        "type": "hello",
        "connector_id": connector_id,
        "token": token,
        "protocol_version": "1.0",
        "account_ids": "wx_001",
        "client_info": "txrui_desktop/1.0",
        "capabilities": {"text": True, "image": True, "audio": False},
    }


class TestServerLifecycle:
    def test_address_in_use_fails_without_terminating_owner(self, tmp_path):
        async def scenario():
            owner = TxruiWSServer(host="127.0.0.1", port=0, media_dir=str(tmp_path / "owner"))
            await owner.start()
            try:
                port = owner._server.sockets[0].getsockname()[1]
                duplicate = TxruiWSServer(
                    host="127.0.0.1", port=port, media_dir=str(tmp_path / "duplicate")
                )
                with pytest.raises(OSError, match="当前 AstrBot 进程"):
                    await duplicate.start()
                assert owner._server is not None
                assert owner._running
                assert duplicate._server is None
            finally:
                await owner.stop()

        _run(scenario())


class TestHandshake:
    def test_hello_ok_returns_expected_ack_shape(self, tmp_path):
        """握手成功回 hello_ack，形状与客户端期望一致。"""

        async def scenario():
            server = _server(tmp_path)
            ws = _FakeWS()
            conn = await server._handle_hello_frame(ws, _hello())
            assert conn is not None
            assert ws.sent[0]["type"] == "hello_ack"
            assert ws.sent[0]["protocol_version"] == PROTOCOL_VERSION
            assert set(ws.sent[0].keys()) == {"type", "protocol_version"}
            # 连接已登记
            assert server.connections

        _run(scenario())

    def test_hello_records_capabilities(self, tmp_path):
        """hello.capabilities 被记下来（供下行降级判断）。"""

        async def scenario():
            server = _server(tmp_path)
            ws = _FakeWS()
            conn = await server._handle_hello_frame(ws, _hello())
            assert conn.capabilities.get("text") is True
            assert conn.capabilities.get("audio") is False
            assert conn.supports("image") is True
            assert conn.supports("audio") is False

        _run(scenario())

    def test_hello_rejects_wrong_connector_id(self, tmp_path):
        """connector_id 不符 -> 握手被拒。"""
        server = _server(tmp_path)
        with pytest.raises(Exception) as exc:
            server._verify_hello(_hello(connector_id="wrong"))
        assert "connector_id" in str(exc.value)

    def test_hello_rejects_wrong_token(self, tmp_path):
        """token 不符 -> 握手被拒。"""
        server = _server(tmp_path)
        with pytest.raises(Exception) as exc:
            server._verify_hello(_hello(token="wrong"))
        assert "token" in str(exc.value)

    def test_hello_accepts_matching_credentials(self, tmp_path):
        """connector_id + token 都匹配 -> 通过。"""
        server = _server(tmp_path)
        server._verify_hello(_hello())  # 不抛异常即通过

    def test_empty_configured_token_skips_token_check(self, tmp_path):
        """服务端未配 token 时不校验 token（但仍校验 connector_id）。"""
        server = _server(tmp_path, token="")
        server._verify_hello(_hello(token=""))  # 通过
        with pytest.raises(Exception):
            server._verify_hello(_hello(connector_id="wrong"))

    def test_non_hello_first_frame_rejected(self, tmp_path):
        """首帧不是 hello -> 拒绝。"""

        async def scenario():
            server = _server(tmp_path)
            ws = _FakeWS()
            with pytest.raises(Exception):
                await server._handle_hello_frame(ws, {"type": "event", "payload": {}})

        _run(scenario())


class TestConvertMessage:
    def _adapter(self, tmp_path):
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
            asyncio.Queue(),
        )

    def test_group_message(self, tmp_path):
        """群聊 -> GROUP_MESSAGE + Group。"""
        from astrbot.api.platform import MessageType

        adapter = self._adapter(tmp_path)
        payload = {
            "event_id": "evt_1",
            "chat_id": "群A",
            "chat_type": "group",
            "client_seq": "123_abcdef12",
            "sender": {"platform_user_id": "u1", "display_name": "张三"},
            "content": [{"type": "text", "text": "你好"}],
            "timestamp": 1700000000,
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert msg.type == MessageType.GROUP_MESSAGE
        assert msg.group is not None
        assert msg.group.group_id == "群A"
        assert msg.session_id == "群A"
        assert msg.message_id == "123_abcdef12"
        assert msg.sender.user_id == "u1"
        assert msg.sender.nickname == "张三"
        assert msg.message_str == "你好"
        assert msg.timestamp == 1700000000

    def test_private_message(self, tmp_path):
        """私聊 -> FRIEND_MESSAGE，无 group。"""
        from astrbot.api.platform import MessageType

        adapter = self._adapter(tmp_path)
        payload = {
            "event_id": "evt_2",
            "chat_id": "李四",
            "chat_type": "private",
            "sender": {"platform_user_id": "u2", "display_name": "李四"},
            "content": [{"type": "text", "text": "在吗"}],
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert msg.type == MessageType.FRIEND_MESSAGE
        assert msg.group is None
        assert msg.message_id == "evt_2"  # 无 client_seq 时回退 event_id

    def test_emoji_segment_is_text(self, tmp_path):
        """emoji 段（client 已降级为 text + extra.emoji）按文本处理。"""
        from astrbot.api.message_components import Plain

        adapter = self._adapter(tmp_path)
        payload = {
            "chat_id": "c",
            "chat_type": "private",
            "sender": {"platform_user_id": "u", "display_name": "u"},
            "content": [
                {"type": "text", "text": "[表情]", "extra": {"emoji": True}},
            ],
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert len(msg.message) == 1
        assert isinstance(msg.message[0], Plain)
        assert msg.message[0].text == "[表情]"

    def test_media_placeholder_degrades_to_text(self, tmp_path):
        """占位媒体段降级为 [图片] 等占位文本，**不指向不存在的 media_id**。"""
        from astrbot.api.message_components import Plain

        adapter = self._adapter(tmp_path)
        payload = {
            "chat_id": "c",
            "chat_type": "private",
            "sender": {"platform_user_id": "u", "display_name": "u"},
            "content": [
                {
                    "type": "image",
                    "text": "[图片]",
                    "extra": {"placeholder": True},
                },
                {
                    "type": "file",
                    "text": "[文件]",
                    "extra": {"placeholder": True},
                },
            ],
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert len(msg.message) == 2
        assert all(isinstance(c, Plain) for c in msg.message)
        assert msg.message[0].text == "[图片]"
        assert msg.message[1].text == "[文件]"

    def test_media_with_unknown_media_id_degrades(self, tmp_path):
        """media_id 指向不存在的本体时降级为占位文本。"""
        from astrbot.api.message_components import Plain

        adapter = self._adapter(tmp_path)
        payload = {
            "chat_id": "c",
            "chat_type": "private",
            "sender": {"platform_user_id": "u", "display_name": "u"},
            "content": [
                {
                    "type": "image",
                    "text": "[图片]",
                    "media_id": "deadbeef",
                    "extra": {"name": "x.png"},
                },
            ],
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert isinstance(msg.message[0], Plain)
        assert msg.message[0].text == "[图片]"

    def test_media_with_real_blob_builds_image_component(self, tmp_path):
        """media_id 对应真实落盘媒体 -> 构造 Image 组件。"""
        from astrbot.api.message_components import Image

        adapter = self._adapter(tmp_path)
        data = b"\x89PNG-test-bytes"
        entry = adapter.server.media_store.put(data, filename="a.png", mime="image/png")
        payload = {
            "chat_id": "c",
            "chat_type": "private",
            "sender": {"platform_user_id": "u", "display_name": "u"},
            "content": [
                {
                    "type": "image",
                    "text": "a.png",
                    "media_id": entry.media_id,
                    "mime": "image/png",
                    "extra": {"name": "a.png"},
                },
            ],
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert isinstance(msg.message[0], Image)
        assert os.path.isfile(entry.path)

    def test_missing_chat_id_returns_none(self, tmp_path):
        """缺 chat_id -> 返回 None（不产生无效事件）。"""
        adapter = self._adapter(tmp_path)
        assert adapter.convert_message({"chat_type": "private", "content": []}) is None


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
