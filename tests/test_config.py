"""设置窗口（WebUI 可调配置项）测试。

覆盖：
- config_metadata schema 结构：每个字段的 type 合法、进阶项带 collapsed 标记
- 配置读取：整数/浮点/布尔/列表的解析、非法值回落、越界钳制
- 运行时生效：媒体上限、发送超时、心跳节流、媒体总开关、黑名单、文本限长
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest

from astrbot_plugin_txrui.txrui_adapter import TxruiAdapter
from astrbot_plugin_txrui.txrui_client import TxruiWSServer
from astrbot_plugin_txrui.txrui_config import TXRUI_CONFIG_METADATA

#: WebUI ConfigItemRenderer 支持的字段类型
SUPPORTED_TYPES = {
    "string",
    "int",
    "float",
    "bool",
    "text",
    "list",
    "dict",
    "file",
    "template_list",
}


def _adapter(tmp_path, **overrides):
    cfg = {
        "id": "txrui_default",
        "enable": False,
        "host": "127.0.0.1",
        "port": 8788,
        "token": "tok",
        "connector_id": "txrui_001",
        "account_id": "wx_001",
        "media_dir": str(tmp_path / "media"),
    }
    cfg.update(overrides)
    return TxruiAdapter(cfg, {}, asyncio.Queue())


class TestConfigSchema:
    def test_internal_icon_token_is_hidden_and_id_is_distinct(self):
        assert TXRUI_CONFIG_METADATA["logo_token"]["invisible"] is True
        assert TXRUI_CONFIG_METADATA["id"]["description"] == "机器人 ID"
        assert TXRUI_CONFIG_METADATA["display_name"]["description"] == "机器人名称"

    def test_every_field_has_supported_type(self):
        """所有字段的 type 必须是 WebUI 支持的类型。"""
        for key, meta in TXRUI_CONFIG_METADATA.items():
            assert "description" in meta, f"{key} 缺 description"
            assert meta.get("type") in SUPPORTED_TYPES, f"{key} 的 type 非法: {meta.get('type')}"

    def test_secret_field_is_token(self):
        """token 必须是密码框渲染。"""
        assert TXRUI_CONFIG_METADATA["token"]["secret"] is True

    def test_advanced_fields_are_collapsed(self):
        """进阶项带 collapsed 标记（WebUI 收进「更多配置」= 分组）。"""
        collapsed = [k for k, m in TXRUI_CONFIG_METADATA.items() if m.get("collapsed")]
        assert "debug_log" in collapsed
        assert "log_payload" in collapsed
        assert "blocked_chat_ids" in collapsed
        # 常用项不应折叠
        assert not TXRUI_CONFIG_METADATA["host"].get("collapsed")
        assert not TXRUI_CONFIG_METADATA["token"].get("collapsed")

    def test_numeric_fields_have_slider_bounds(self):
        """数值字段的 slider 范围合法（min < max）。"""
        for key, meta in TXRUI_CONFIG_METADATA.items():
            if meta.get("type") not in ("int", "float"):
                continue
            slider = meta.get("slider")
            if slider is None:
                continue
            assert slider["min"] < slider["max"], f"{key} 的 slider 范围非法"
            assert slider.get("step", 1) > 0

    def test_schema_is_json_serializable(self):
        """schema 要能进 JSON（WebUI 传给前端）。"""
        json.dumps(TXRUI_CONFIG_METADATA, ensure_ascii=False)


class TestConfigParsing:
    def test_defaults_applied_when_missing(self, tmp_path):
        """缺配置时用默认值。"""
        adapter = _adapter(tmp_path)
        assert adapter.max_media_bytes == 20 * 1024 * 1024
        assert adapter.max_frame_bytes == 64 * 1024 * 1024
        assert adapter.send_ack_timeout == 30.0
        assert adapter.enable_media is True
        assert adapter.max_text_length == 0

    def test_numeric_overrides(self, tmp_path):
        """数值配置被正确读取。"""
        adapter = _adapter(
            tmp_path,
            max_media_mb=5,
            max_frame_mb=32,
            send_ack_timeout=12.5,
            heartbeat_interval=3,
            handshake_timeout=20,
        )
        assert adapter.max_media_bytes == 5 * 1024 * 1024
        assert adapter.max_frame_bytes == 32 * 1024 * 1024
        assert adapter.send_ack_timeout == 12.5
        assert adapter.heartbeat_interval == 3
        assert adapter.handshake_timeout == 20

    def test_invalid_values_fall_back(self, tmp_path):
        """非法值回落到默认值（不抛异常）。"""
        adapter = _adapter(
            tmp_path,
            max_media_mb="not-a-number",
            send_ack_timeout="abc",
            enable_media="maybe",
        )
        assert adapter.max_media_bytes == 20 * 1024 * 1024
        assert adapter.send_ack_timeout == 30.0
        assert adapter.enable_media is True

    def test_out_of_range_clamped(self, tmp_path):
        """越界值被钳制到下限。"""
        adapter = _adapter(tmp_path, max_media_mb=0, send_ack_timeout=-5)
        assert adapter.max_media_bytes == 1 * 1024 * 1024
        assert adapter.send_ack_timeout == 1.0

    def test_bool_accepts_various_forms(self, tmp_path):
        """布尔接受 true/false、"1"/"0"、1/0。"""
        assert _adapter(tmp_path, enable_media=False).enable_media is False
        assert _adapter(tmp_path, enable_media="0").enable_media is False
        assert _adapter(tmp_path, enable_media="false").enable_media is False
        assert _adapter(tmp_path, enable_media="1").enable_media is True
        assert _adapter(tmp_path, enable_media="true").enable_media is True

    def test_list_accepts_string_or_list(self, tmp_path):
        """黑名单接受逗号分隔字符串或 list。"""
        a = _adapter(tmp_path, blocked_chat_ids="公众号A, 公众号B")
        assert a.blocked_chat_ids == {"公众号A", "公众号B"}
        b = _adapter(tmp_path, blocked_chat_ids=["x", "y"])
        assert b.blocked_sender_ids == set()
        assert b.blocked_chat_ids == {"x", "y"}

    def test_frame_limit_never_below_media_limit(self, tmp_path):
        """帧上限自动对齐到不小于单文件上限。"""
        adapter = _adapter(tmp_path, max_media_mb=64, max_frame_mb=16)
        assert adapter.max_frame_bytes == adapter.max_media_bytes


class TestRuntimeEffects:
    def test_blocked_chat_is_dropped(self, tmp_path):
        """黑名单会话的消息不入队。"""
        adapter = _adapter(tmp_path, blocked_chat_ids=["垃圾会话"])
        assert adapter._is_blocked({"chat_id": "垃圾会话"}) is True
        assert adapter._is_blocked({"chat_id": "正常会话"}) is False

    def test_blocked_sender_is_dropped(self, tmp_path):
        """黑名单发送者的消息不入队。"""
        adapter = _adapter(tmp_path, blocked_sender_ids=["u_bad"])
        payload = {"chat_id": "c", "sender": {"platform_user_id": "u_bad"}}
        assert adapter._is_blocked(payload) is True
        payload2 = {"chat_id": "c", "sender": {"platform_user_id": "u_ok"}}
        assert adapter._is_blocked(payload2) is False

    def test_text_length_truncation(self, tmp_path):
        """超长文本被截断。"""
        adapter = _adapter(tmp_path, max_text_length=10)
        assert adapter._process_text("x" * 50) == "x" * 10

    def test_text_length_zero_means_unlimited(self, tmp_path):
        """max_text_length=0 表示不限制。"""
        adapter = _adapter(tmp_path, max_text_length=0)
        assert adapter._process_text("x" * 5000) == "x" * 5000

    def test_strip_at_mention(self, tmp_path):
        """开启后剥离 @某人 前缀。"""
        adapter = _adapter(tmp_path, strip_at_in_text=True)
        assert adapter._process_text("@张三 你好") == "你好"

    def test_strip_at_disabled_keeps_text(self, tmp_path):
        """关闭时保留原文。"""
        adapter = _adapter(tmp_path, strip_at_in_text=False)
        assert adapter._process_text("@张三 你好") == "@张三 你好"

    def test_media_disabled_downgrades_to_text(self, tmp_path):
        """媒体总开关关闭 -> 入站媒体降级为占位文本。"""
        from astrbot.api.message_components import Plain

        adapter = _adapter(tmp_path, enable_media=False)
        entry = adapter.server.media_store.put(b"x", filename="a.png")
        payload = {
            "chat_id": "c",
            "chat_type": "private",
            "sender": {"platform_user_id": "u", "display_name": "u"},
            "content": [
                {"type": "image", "text": "a.png", "media_id": entry.media_id},
            ],
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert isinstance(msg.message[0], Plain)
        assert msg.message[0].text == "[图片]"

    def test_media_disabled_skips_when_no_fallback(self, tmp_path):
        """媒体关闭且不降级 -> 该段直接跳过。"""
        adapter = _adapter(tmp_path, enable_media=False, media_fallback_to_text=False)
        entry = adapter.server.media_store.put(b"x", filename="a.png")
        payload = {
            "chat_id": "c",
            "chat_type": "private",
            "sender": {"platform_user_id": "u", "display_name": "u"},
            "content": [
                {"type": "image", "text": "a.png", "media_id": entry.media_id},
                {"type": "text", "text": "你好"},
            ],
        }
        msg = adapter.convert_message(payload)
        assert msg is not None
        assert len(msg.message) == 1
        assert msg.message_str == "你好"


class TestServerLimits:
    def test_media_limit_is_configurable(self, tmp_path):
        """媒体上限可配：超过配置值的上传被拒绝。"""

        async def scenario():
            server = TxruiWSServer(
                host="127.0.0.1",
                port=0,
                token="tok",
                media_dir=str(tmp_path / "m"),
                max_media_bytes=1024,
            )

            class _WS:
                def __init__(self):
                    self.sent = []

                async def send(self, raw):
                    self.sent.append(json.loads(raw))

                async def close(self, code=1000, reason=""):
                    pass

            from astrbot_plugin_txrui.txrui_client import TxruiConnection

            conn = TxruiConnection(_WS(), connector_id="txrui_001")
            import base64

            data = b"x" * 2048
            await server._handle_media_upload(
                conn,
                {
                    "type": "media_upload",
                    "client_seq": "m1",
                    "filename": "big.bin",
                    "mime": "",
                    "data_b64": base64.b64encode(data).decode(),
                },
            )
            ack = conn.ws.sent[-1]
            assert ack["ok"] is False
            assert ack["error"] == "too_large"

        asyncio.run(scenario())

    def test_media_disabled_rejects_upload(self, tmp_path):
        """媒体总开关关闭 -> media_upload 被拒。"""

        async def scenario():
            server = TxruiWSServer(
                host="127.0.0.1",
                port=0,
                token="tok",
                media_dir=str(tmp_path / "m"),
                enable_media=False,
            )

            class _WS:
                def __init__(self):
                    self.sent = []

                async def send(self, raw):
                    self.sent.append(json.loads(raw))

                async def close(self, code=1000, reason=""):
                    pass

            from astrbot_plugin_txrui.txrui_client import TxruiConnection

            conn = TxruiConnection(_WS(), connector_id="txrui_001")
            import base64

            await server._handle_media_upload(
                conn,
                {
                    "type": "media_upload",
                    "client_seq": "m1",
                    "filename": "a.png",
                    "mime": "image/png",
                    "data_b64": base64.b64encode(b"x").decode(),
                },
            )
            ack = conn.ws.sent[-1]
            assert ack["ok"] is False
            assert ack["error"] == "media_disabled"

        asyncio.run(scenario())

    def test_heartbeat_throttle(self, tmp_path):
        """心跳节流：间隔内的重复心跳不再回帧。"""

        async def scenario():
            server = TxruiWSServer(
                host="127.0.0.1",
                port=0,
                token="tok",
                media_dir=str(tmp_path / "m"),
                heartbeat_interval=60,
            )

            class _WS:
                def __init__(self):
                    self.sent = []

                async def send(self, raw):
                    self.sent.append(json.loads(raw))

                async def close(self, code=1000, reason=""):
                    pass

            from astrbot_plugin_txrui.txrui_client import TxruiConnection

            conn = TxruiConnection(_WS(), connector_id="txrui_001")
            await server._dispatch(conn, {"type": "heartbeat"})
            assert len(conn.ws.sent) == 1
            # 第二次在节流窗口内 -> 不再应答
            await server._dispatch(conn, {"type": "heartbeat"})
            assert len(conn.ws.sent) == 1

        asyncio.run(scenario())


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
