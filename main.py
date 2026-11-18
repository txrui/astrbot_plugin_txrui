"""Txrui 插件入口：平台类型随插件启用而注册，随插件关闭而注销。

配置来自 WebUI「平台」页，关闭插件时保留已保存的机器人配置。
"""

from __future__ import annotations

import logging

from astrbot.api.event import filter
from astrbot.api.star import Context, Star

from .txrui_meme_compat import MemePersonaCompatibility

logger = logging.getLogger(__name__)

PLATFORM_TYPE = "txrui"
DEFAULT_PLATFORM_ID = "txrui_default"


class TxruiPlugin(Star):
    """txrui 微信桌面端接入插件。"""

    def __init__(self, context: Context, config=None):
        from astrbot.core.platform.register import platform_cls_map

        from .txrui_adapter import TxruiAdapter, _register_txrui_adapter

        # 首次导入由装饰器注册；模块仍被缓存时，重新启用需要显式注册。
        if PLATFORM_TYPE not in platform_cls_map:
            _register_txrui_adapter(TxruiAdapter)

        self.context = context
        self.config = config
        self._meme_compat = MemePersonaCompatibility()

    async def initialize(self) -> None:
        """安装兼容层；仅运行时重新启用时立即补载平台实例。"""
        self._meme_compat.install(self.context)
        manager = self.context.platform_manager
        # 核心先初始化插件，再调用 PlatformManager.initialize() 加载全部平台。
        # 此时抢先补载会创建两个相同 ID 的实例，造成端口冲突及收发路由分离。
        # webchat 是管理器初始化末尾创建的内置实例，可用于识别运行时重新启用。
        if any(inst.meta().name == "webchat" for inst in manager.platform_insts):
            await self.restore_platforms()

    @filter.on_astrbot_loaded()
    async def restore_platforms(self) -> None:
        """核心完成平台初始化后补载；运行时重新启用也复用此幂等检查。"""
        manager = self.context.platform_manager
        configs = getattr(manager, "platforms_config", []) or []
        loaded_ids = {
            str(getattr(inst, "config", {}).get("id", ""))
            for inst in manager.platform_insts
            if getattr(inst, "config", {}).get("type") == PLATFORM_TYPE
        }
        restored = 0
        for platform_config in configs:
            if platform_config.get("type") != PLATFORM_TYPE or not platform_config.get("enable"):
                continue
            platform_id = str(platform_config.get("id", ""))
            if platform_id in loaded_ids:
                continue
            try:
                await manager.load_platform(platform_config)
                if platform_id in getattr(manager, "_inst_map", {}):
                    loaded_ids.add(platform_id)
                    restored += 1
            except Exception:
                logger.exception("txrui 平台实例恢复失败：%s", platform_id)
        logger.info(
            "txrui 插件已加载，恢复平台实例 %s 个（已保存启用配置 %s 个）",
            restored,
            sum(1 for c in configs if c.get("type") == PLATFORM_TYPE and c.get("enable")),
        )

    @filter.on_llm_request(priority=100000)
    async def ensure_meme_compatibility(self, event, req):
        """在表情插件处理之前安装兼容层，也支持表情插件晚加载或重载。"""
        if event.get_platform_name() == PLATFORM_TYPE:
            self._meme_compat.install(self.context)

    @filter.on_agent_done(priority=100000)
    async def preserve_meme_tool_history(self, event, run_context, response):
        """先于表情插件的 99999 优先级完成钩子保留 txrui 的真实工具回执。"""
        if event.get_platform_name() == PLATFORM_TYPE:
            self._meme_compat.compact_history(event, run_context)

    async def terminate(self) -> None:
        """关闭平台实例并注销平台类型，保留磁盘上的机器人配置。"""
        from astrbot.core.platform.register import unregister_platform_adapters_by_module

        from .txrui_adapter import TxruiAdapter

        self._meme_compat.uninstall()
        manager = self.context.platform_manager
        try:
            for inst in list(manager.platform_insts):
                if not isinstance(inst, TxruiAdapter):
                    continue
                try:
                    # 交给管理器清理实例索引与运行任务，避免只关闭 WS 后
                    # 已停止的实例仍被后续发送逻辑获取到。
                    await manager.terminate_platform(inst.config["id"])
                except Exception:
                    logger.exception("txrui 平台实例停止失败")
        finally:
            # 添加机器人的选项来自平台注册表，而不是 WS 服务状态。
            # 只注销本插件适配器，避免影响其他插件或内置平台。
            unregister_platform_adapters_by_module(TxruiAdapter.__module__)
            logger.info("txrui 插件已关闭，平台类型已注销，机器人配置已保留")


__all__ = ["TxruiPlugin", "PLATFORM_TYPE", "DEFAULT_PLATFORM_ID"]
