"""astrbot_plugin_txrui 插件入口。

平台适配器通过装饰器在**模块导入时**注册，所以这里必须 import 适配器模块，
否则 AstrBot 的 PlatformManager 找不到 `txrui` 这个类型。
"""

from astrbot.api.star import Context, Star


class TxruiPlugin(Star):
    """txrui 微信桌面端接入插件。"""

    def __init__(self, context: Context):
        # noqa: F401  —— import 即触发 @register_platform_adapter 注册
        from .txrui_adapter import TxruiAdapter  # noqa: F401

    async def terminate(self) -> None:
        """插件卸载/重载时关闭 WS 服务端。

        平台实例由 PlatformManager 持有，这里按类型找到 txrui 适配器并终止。
        """
        try:
            from .txrui_adapter import TxruiAdapter

            for inst in list(self.context.platform_manager.platform_insts):
                if isinstance(inst, TxruiAdapter):
                    await inst.terminate()
        except Exception:  # noqa: BLE001
            # 插件卸载阶段不应抛异常打断框架流程
            pass
