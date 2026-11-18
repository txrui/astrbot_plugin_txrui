"""平台页配置（WebUI「平台」页 → 编辑 txrui 机器人实例）测试。

覆盖：
- `config_metadata` 与 `default_config_tmpl` 的结构与一致性
- 适配器直接按平台配置建实例（不依赖插件配置弹窗）
- 平台配置里缺字段时回落到默认值
- 适配器不再暴露插件配置注入入口
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
from astrbot_plugin_txrui.txrui_config import (
    TXRUI_CONFIG_DEFAULTS,
    TXRUI_CONFIG_KEYS,
    TXRUI_CONFIG_METADATA,
    build_default_config_tmpl,
)

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
    """模拟 PlatformManager 用平台配置创建适配器。"""
    cfg = {
        "id": "txrui_default",
        "type": "txrui",
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


class TestConfigMetadata:
    def test_every_field_has_supported_type(self):
        """所有字段的 type 必须是 WebUI 支持的类型。"""
        for key, meta in TXRUI_CONFIG_METADATA.items():
            assert "description" in meta, f"{key} 缺 description"
            assert meta.get("type") in SUPPORTED_TYPES, f"{key} 的 type 非法: {meta.get('type')}"

    def test_metadata_matches_defaults(self):
        """metadata 与默认值表的键完全一致。"""
        assert set(TXRUI_CONFIG_METADATA.keys()) == set(TXRUI_CONFIG_DEFAULTS.keys())
        assert set(TXRUI_CONFIG_KEYS) == set(TXRUI_CONFIG_DEFAULTS.keys())

    def test_metadata_is_json_serializable(self):
        """metadata 要能进 JSON（WebUI 传给前端）。"""
        json.dumps(TXRUI_CONFIG_METADATA, ensure_ascii=False)

    def test_secret_field_is_token(self):
        """token 必须是密码框渲染。"""
        assert TXRUI_CONFIG_METADATA["token"]["secret"] is True


class TestDefaultConfigTemplate:
    def test_template_covers_all_keys(self):
        """默认模板包含全部可配字段。"""
        template = build_default_config_tmpl()
        assert set(template.keys()) == set(TXRUI_CONFIG_KEYS)

    def test_template_values_match_defaults(self):
        """默认模板的值与默认值表一致。"""
        template = build_default_config_tmpl()
        for key, default in TXRUI_CONFIG_DEFAULTS.items():
            assert template[key] == default, f"{key} 的默认值不一致"

    def test_template_is_json_serializable(self):
        """默认模板要能写进 cmd_config.json。"""
        json.dumps(build_default_config_tmpl(), ensure_ascii=False)


class TestAdapterReadsPlatformConfig:
    def test_platform_config_applied_on_construction(self, tmp_path):
        """平台配置在建实例时就生效（无需任何插件侧同步）。"""
        adapter = _adapter(
            tmp_path,
            host="0.0.0.0",
            port=9911,
            token="secret-token",
            connector_id="tx_002",
            account_id="wx_002",
            max_media_mb=8,
            max_frame_mb=16,
            enable_media=False,
            max_text_length=120,
            blocked_chat_ids=["垃圾会话"],
            strip_at_in_text=True,
        )
        assert adapter.host == "0.0.0.0"
        assert adapter.port == 9911
        assert adapter.token == "secret-token"
        assert adapter.connector_id == "tx_002"
        assert adapter.account_id == "wx_002"
        assert adapter.max_media_bytes == 8 * 1024 * 1024
        assert adapter.max_frame_bytes == 16 * 1024 * 1024
        assert adapter.enable_media is False
        assert adapter.max_text_length == 120
        assert adapter.blocked_chat_ids == {"垃圾会话"}
        assert adapter.strip_at_in_text is True

    def test_server_built_from_platform_config(self, tmp_path):
        """WS 服务端按平台配置建立。"""
        adapter = _adapter(tmp_path, host="127.0.0.1", port=9922, token="t")
        assert adapter.server.host == "127.0.0.1"
        assert adapter.server.port == 9922
        assert adapter.server.token == "t"

    def test_missing_fields_fall_back_to_defaults(self, tmp_path):
        """平台配置里没写的字段走默认值。"""
        adapter = TxruiAdapter(
            {"id": "txrui_default", "type": "txrui", "enable": True},
            {},
            asyncio.Queue(),
        )
        assert adapter.port == 8788
        assert adapter.max_media_bytes == 20 * 1024 * 1024
        assert adapter.max_frame_bytes == 64 * 1024 * 1024
        assert adapter.enable_media is True

    def test_plugin_config_entrypoint_removed(self):
        """不再有插件配置注入入口（配置只在平台页）。"""
        assert not hasattr(TxruiAdapter, "apply_plugin_config")


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
