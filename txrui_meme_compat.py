"""仅为 txrui 会话兼容 meme_manager 的人格接口和工具历史，不修改宿主。"""
from __future__ import annotations

import inspect
from contextvars import ContextVar
from functools import wraps

_legacy_txrui_request = ContextVar("txrui_meme_legacy_persona", default=False)
_LEGACY_KEYS = {"conversation_persona_id", "provider_settings"}
_DELIVERY_RULES = """[txrui 表情发送约束]
历史中的工具结果或“已发送”摘要只说明过去的发送，不代表本轮已经发送。
用户本轮要求发送表情图片时，应实际调用 send_meme，不能只复述历史发送话术或用 Unicode emoji 代替图片。
只有本轮 send_meme 返回 ok=true、status=sent 后，才可声称本轮发送成功；失败或结果未知时如实说明，不自行重试结果未知的发送。
[/txrui 表情发送约束]"""


class _Proxy:
    def __init__(self, target):
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name):
        return getattr(self._target, name)

    def __setattr__(self, name, value):
        setattr(self._target, name, value)


class _PersonaProxy(_Proxy):
    async def resolve_selected_persona(self, *args, **kwargs):
        resolver = self._target.resolve_selected_persona
        if not _legacy_txrui_request.get() or not _LEGACY_KEYS.intersection(kwargs):
            return await resolver(*args, **kwargs)
        # 只过滤两项已移除的历史参数，其他参数错误仍正常抛出。
        params = inspect.signature(resolver).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return await resolver(*args, **kwargs)
        removed = _LEGACY_KEYS.difference(params)
        if not removed:
            return await resolver(*args, **kwargs)
        result = await resolver(*args, **{k: v for k, v in kwargs.items() if k not in removed})
        # 当前宿主返回三项，历史表情插件解包四项；第四项未被其使用。
        if isinstance(result, tuple) and len(result) == 3:
            return (*result, None)
        return result


class _ContextProxy(_Proxy):
    @property
    def persona_manager(self):
        return _PersonaProxy(self._target.persona_manager)


class MemePersonaCompatibility:
    """为表情插件提供 txrui 专用人格兼容与结构化工具历史保留。"""

    def __init__(self):
        self._installed = []

    def install(self, context):
        for metadata in context.get_all_stars():
            if getattr(metadata, "root_dir_name", None) != "meme_manager":
                continue
            plugin = getattr(metadata, "star_cls", None)
            if plugin is None or not getattr(metadata, "activated", True):
                continue
            if any(item[0] is plugin for item in self._installed):
                continue
            original = getattr(plugin, "_inject_meme_prompt_impl", None)
            original_context = getattr(plugin, "context", None)
            if not callable(original) or original_context is None:
                continue
            proxy = _ContextProxy(original_context)

            @wraps(original)
            async def compatible(event, req, _original=original):
                token = _legacy_txrui_request.set(event.get_platform_name() == "txrui")
                try:
                    result = await _original(event, req)
                    # 在 meme_manager 完成工具筛选与提示词注入后追加，且只影响 txrui。
                    if (
                        _legacy_txrui_request.get()
                        and req is not None
                        and isinstance(getattr(req, "system_prompt", None), str)
                        and event.get_extra("meme_manager_send_tool_available")
                        and _DELIVERY_RULES not in req.system_prompt
                    ):
                        req.system_prompt += "\n\n" + _DELIVERY_RULES
                    return result
                finally:
                    _legacy_txrui_request.reset(token)

            # 保存实例原始属性，卸载时恢复继承方法，而非留下绑定方法副本。
            previous = plugin.__dict__.get("_inject_meme_prompt_impl")
            had_override = "_inject_meme_prompt_impl" in plugin.__dict__
            plugin.context = proxy
            plugin._inject_meme_prompt_impl = compatible
            self._installed.append((plugin, original_context, proxy, compatible, previous, had_override))

    def compact_history(self, event, run_context) -> bool:
        """先于表情插件的完成钩子清理预览，保留真实调用及结果。

        AstrBot 注册的是已绑定的处理器，直接替换插件实例上的
        compact_meme_tool_history 方法不会改变既有的事件分发。
        因此由 txrui 的高优先级完成钩子调用，并消费原钩子的跟踪标记。
        不改变插件配置、共享函数或其他平台请求。
        """
        if event.get_platform_name() != "txrui":
            return False
        if event.get_extra("meme_manager_tool_context") is not run_context:
            return False
        for plugin, *_ in reversed(self._installed):
            compactor = getattr(plugin, "compact_meme_tool_history", None)
            function = getattr(compactor, "__func__", compactor)
            discard = getattr(function, "__globals__", {}).get(
                "discard_meme_tool_context"
            )
            # 不认识的插件版本继续使用它自己的处理逻辑，不盲目屏蔽原钩子。
            if not callable(discard):
                continue
            try:
                parameters = inspect.signature(discard).parameters
            except (TypeError, ValueError):
                continue
            if "preview_only" not in parameters:
                continue
            # 插件自带的预览清理会移除拼图、预览调用和对应结果；
            # 保留 send_meme/search_memes 的结构化记录，供下一轮区分操作与文字。
            discard(run_context.messages, preview_only=True)
            event.set_extra("meme_manager_tool_context", None)
            event.set_extra("meme_manager_preview_candidates", None)
            event.set_extra("meme_manager_send_summary", None)
            return True
        return False

    def uninstall(self):
        for plugin, context, proxy, wrapper, previous, had_override in self._installed:
            if plugin.context is proxy:
                plugin.context = context
            if plugin._inject_meme_prompt_impl is wrapper:
                if had_override:
                    plugin._inject_meme_prompt_impl = previous
                else:
                    del plugin._inject_meme_prompt_impl
        self._installed.clear()
