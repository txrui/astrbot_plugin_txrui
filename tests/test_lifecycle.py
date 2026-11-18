"""平台生命周期回归：启动时不能早于管理器重复创建微信实例。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_txrui.main import TxruiPlugin


def _plugin(instances=None):
    config = {"id": "txrui_default", "type": "txrui", "enable": True}
    manager = SimpleNamespace(
        platform_insts=list(instances or []),
        platforms_config=[config],
        _inst_map={},
    )

    async def load(cfg):
        inst = SimpleNamespace(config=cfg, meta=lambda: SimpleNamespace(name="txrui"))
        manager.platform_insts.append(inst)
        manager._inst_map[cfg["id"]] = {"inst": inst}

    manager.load_platform = AsyncMock(side_effect=load)
    plugin = object.__new__(TxruiPlugin)
    plugin.context = SimpleNamespace(platform_manager=manager)
    plugin._meme_compat = SimpleNamespace(install=lambda context: None)
    return plugin, manager


def test_startup_leaves_platform_loading_to_core():
    async def scenario():
        plugin, manager = _plugin()
        await plugin.initialize()
        manager.load_platform.assert_not_awaited()
        # 核心接着加载平台；完成钩子不能重复创建。
        await manager.load_platform(manager.platforms_config[0])
        await plugin.restore_platforms()
        assert manager.load_platform.await_count == 1

    asyncio.run(scenario())


def test_loaded_hook_restores_missing_platform_once():
    async def scenario():
        plugin, manager = _plugin()
        await plugin.restore_platforms()
        await plugin.restore_platforms()
        assert manager.load_platform.await_count == 1

    asyncio.run(scenario())


def test_runtime_enable_restores_missing_platform():
    async def scenario():
        webchat = SimpleNamespace(
            config={}, meta=lambda: SimpleNamespace(name="webchat")
        )
        plugin, manager = _plugin([webchat])
        await plugin.initialize()
        assert manager.load_platform.await_count == 1
        await plugin.initialize()
        assert manager.load_platform.await_count == 1

    asyncio.run(scenario())
