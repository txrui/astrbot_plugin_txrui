"""端到端验证：模拟 AstrBot 平台页保存配置 -> 平台实例重建。

覆盖真实链路：
1. 适配器用 `config_metadata` 注册，WebUI「平台」页能拿到表单 schema
2. `PlatformManager.load_platform()` 用平台配置创建适配器，配置直接生效
3. 模拟 WebUI 保存配置 -> `platform_manager.reload()` -> 实例重建，新配置生效
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from astrbot.core.platform.register import platform_registry

from astrbot_plugin_txrui.txrui_adapter import TxruiAdapter
from astrbot_plugin_txrui.txrui_config import (
    TXRUI_CONFIG_METADATA,
    build_default_config_tmpl,
)


def _platform_metadata():
    """从注册表里取 txrui 的 PlatformMetadata。"""
    for pm in platform_registry:
        if pm.name == "txrui":
            return pm
    raise AssertionError("txrui 平台适配器未注册")


class TestRegisteredMetadata:
    def test_adapter_is_registered(self):
        """import 适配器模块即完成注册。"""
        assert _platform_metadata() is not None

    def test_config_metadata_is_exposed(self):
        """平台页表单 schema 通过 config_metadata 暴露。"""
        pm = _platform_metadata()
        assert pm.config_metadata == TXRUI_CONFIG_METADATA
        assert "host" in pm.config_metadata
        assert "token" in pm.config_metadata

    def test_default_config_tmpl_has_all_fields(self):
        """默认模板包含全部可配字段（新增实例时预填）。"""
        pm = _platform_metadata()
        tmpl = pm.default_config_tmpl
        assert tmpl is not None
        expected = build_default_config_tmpl()
        # AstrBot 会额外补上 id / type / enable，这里只校验配置字段本身。
        for key, value in expected.items():
            assert key in tmpl, f"默认模板缺少字段 {key}"
            assert tmpl[key] == value, f"{key} 的默认值不一致"
        assert tmpl["port"] == 8788
        assert tmpl["host"] == "0.0.0.0"

    def test_default_config_tmpl_has_enable_flag(self):
        """register_platform_adapter 自动补齐 enable 开关。"""
        pm = _platform_metadata()
        assert pm.default_config_tmpl["enable"] is False

    def test_custom_bot_name_does_not_change_platform_identity(self):
        pm = _platform_metadata()
        assert pm.default_config_tmpl["display_name"] == ""
        assert pm.config_metadata["display_name"]["type"] == "string"
        assert pm.config_metadata["display_name"]["description"] == "机器人名称"
        assert pm.name == "txrui"
        assert pm.default_config_tmpl["type"] == "txrui"

    def test_display_name_and_streaming_flag(self):
        """展示名与流式标记符合预期。"""
        pm = _platform_metadata()
        assert pm.adapter_display_name == "txrui（微信）"
        assert pm.support_streaming_message is False


class TestPlatformConfigLifecycle:
    def test_load_platform_uses_config(self, tmp_path):
        """模拟 PlatformManager.load_platform：用平台配置创建适配器。"""
        platform_config = {
            "id": "txrui_default",
            "type": "txrui",
            "enable": True,
            "host": "127.0.0.1",
            "port": 9911,
            "token": "secret-token",
            "connector_id": "txrui_001",
            "account_id": "wx_001",
            "media_dir": str(tmp_path / "media"),
            "max_media_mb": 8,
            "max_frame_mb": 16,
            "enable_media": False,
            "max_text_length": 120,
            "blocked_chat_ids": ["垃圾会话"],
            "strip_at_in_text": True,
        }
        adapter = TxruiAdapter(platform_config, {}, asyncio.Queue())

        assert adapter.port == 9911
        assert adapter.token == "secret-token"
        assert adapter.max_media_bytes == 8 * 1024 * 1024
        assert adapter.max_frame_bytes == 16 * 1024 * 1024
        assert adapter.enable_media is False
        assert adapter.max_text_length == 120
        assert adapter.blocked_chat_ids == {"垃圾会话"}
        assert adapter.strip_at_in_text is True

    def test_reload_rebuilds_instance_with_new_config(self, tmp_path):
        """模拟 WebUI 保存 -> platform_manager.reload() -> 实例重建。"""
        old_config = {
            "id": "txrui_default",
            "type": "txrui",
            "enable": True,
            "host": "127.0.0.1",
            "port": 8788,
            "token": "old",
            "media_dir": str(tmp_path / "media"),
            "max_text_length": 0,
        }
        old_adapter = TxruiAdapter(old_config, {}, asyncio.Queue())
        assert old_adapter.port == 8788

        # 用户在平台页改了端口与限制并保存
        new_config = {**old_config, "port": 9955, "token": "new", "max_text_length": 33}

        # reload 的语义：终止旧实例，用新配置重新 load
        asyncio.run(old_adapter.terminate())
        new_adapter = TxruiAdapter(new_config, {}, asyncio.Queue())

        assert new_adapter.port == 9955
        assert new_adapter.token == "new"
        assert new_adapter.max_text_length == 33


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
