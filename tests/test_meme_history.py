"""通过实际 meme_manager 完成钩子验证 txrui 的局部兼容，不初始化平台。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot.core.agent.message import ImageURLPart, Message, TextPart, ToolCall
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot_plugin_txrui.main import TxruiPlugin
from astrbot_plugin_txrui.txrui_meme_compat import MemePersonaCompatibility

# 表情插件是可选依赖；安装了它时执行实际实现的集成测试。
meme_main = pytest.importorskip("meme_manager.main")


class Event:
    def __init__(self, platform="txrui"):
        self.platform = platform
        self.extra = {}

    def get_platform_name(self):
        return self.platform

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


def setup():
    plugin = object.__new__(meme_main.MemeSender)
    plugin.context = SimpleNamespace()
    plugin.keep_meme_send_summary = True

    async def inject(event, req):
        event.set_extra("meme_manager_send_tool_available", True)

    plugin._inject_meme_prompt_impl = inject
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="meme_manager", star_cls=plugin, activated=True,
    )])
    compatibility = MemePersonaCompatibility()
    compatibility.install(context)
    return compatibility, plugin


def messages(result='{"ok":true,"status":"sent","sent_count":1}'):
    calls = [ToolCall(id=name, function={"name": name, "arguments": "{}"})
             for name in ("preview_memes", "search_memes", "send_meme", "weather")]
    return [
        Message(role="user", content="发个表情"),
        Message(role="assistant", tool_calls=calls),
        *[Message(role="tool", tool_call_id=call.id,
                  content=result if call.id == "send_meme" else "result")
          for call in calls],
        Message(role="user", content=[
            TextPart(text="[Image from tool 'preview_memes', path='grid.jpg']"),
            ImageURLPart(image_url={"url": "grid.jpg", "id": "grid.jpg"}),
            TextPart(text="user image"),
            ImageURLPart(image_url={"url": "own.jpg", "id": "own.jpg"}),
        ]),
        Message(role="assistant", content="好了"),
    ]


def prepare_event(event, run_context):
    event.set_extra("meme_manager_tool_context", run_context)
    event.set_extra("meme_manager_preview_candidates", {1: "image"})
    event.set_extra("meme_manager_send_summary", (1, "happy", None))


@pytest.mark.parametrize("result", [
    '{"ok":true,"status":"sent","sent_count":1}',
    '{"ok":false,"reason":"timeout_unknown"}',
])
def test_txrui_preserves_real_results_and_removes_preview(result):
    async def scenario():
        compat, plugin = setup()
        event = Event()
        run_context = SimpleNamespace(messages=messages(result))
        prepare_event(event, run_context)
        assert compat.compact_history(event, run_context)
        # 执行真实的表情完成钩子，确认它不会再次删除已保留的工具记录。
        await plugin.compact_meme_tool_history(event, run_context, None)
        assert [call.function.name for call in run_context.messages[1].tool_calls] == [
            "search_memes", "send_meme", "weather",
        ]
        assert next(m.content for m in run_context.messages
                    if m.role == "tool" and m.tool_call_id == "send_meme") == result
        assert run_context.messages[-1].content == "好了"
        assert run_context.messages[-2].content == [
            TextPart(text="user image"),
            ImageURLPart(image_url={"url": "own.jpg", "id": "own.jpg"}),
        ]
        assert all(value is None for value in event.extra.values())
        assert plugin.keep_meme_send_summary is True
        compat.uninstall()
    asyncio.run(scenario())


def test_other_platform_still_uses_original_compactor():
    async def scenario():
        compat, plugin = setup()
        event = Event("qq_official")
        run_context = SimpleNamespace(messages=messages())
        prepare_event(event, run_context)
        assert not compat.compact_history(event, run_context)
        await plugin.compact_meme_tool_history(event, run_context, None)
        assert [call.function.name for m in run_context.messages
                for call in m.tool_calls or []] == ["weather"]
        assert "已发送 1 张表情" in run_context.messages[-1].content
        compat.uninstall()
    asyncio.run(scenario())


def test_no_tracking_or_unknown_plugin_leaves_original_path():
    compat, plugin = setup()
    event = Event()
    run_context = SimpleNamespace(messages=messages())
    assert not compat.compact_history(event, run_context)
    prepare_event(event, run_context)

    async def future_compactor(event, run_context, response):
        pass

    plugin.compact_meme_tool_history = future_compactor
    assert not compat.compact_history(event, run_context)
    assert event.get_extra("meme_manager_tool_context") is run_context
    compat.uninstall()


def test_prompt_is_txrui_only_idempotent_and_uninstallable():
    async def scenario():
        compat, plugin = setup()
        original = plugin._inject_meme_prompt_impl
        txrui_request = SimpleNamespace(system_prompt="existing")
        qq_request = SimpleNamespace(system_prompt="existing")
        await asyncio.gather(
            plugin._inject_meme_prompt_impl(Event(), txrui_request),
            plugin._inject_meme_prompt_impl(Event("qq_official"), qq_request),
        )
        await plugin._inject_meme_prompt_impl(Event(), txrui_request)
        assert txrui_request.system_prompt.count("[txrui 表情发送约束]") == 1
        assert qq_request.system_prompt == "existing"
        compat.uninstall()
        assert plugin._inject_meme_prompt_impl is not original
        request = SimpleNamespace(system_prompt="existing")
        await plugin._inject_meme_prompt_impl(Event(), request)
        assert request.system_prompt == "existing"
    asyncio.run(scenario())


def test_registered_completion_hook_precedes_meme_compactor():
    async def scenario():
        compat, plugin = setup()
        txrui = object.__new__(TxruiPlugin)
        txrui._meme_compat = compat
        event = Event()
        run_context = SimpleNamespace(messages=messages())
        prepare_event(event, run_context)
        txrui_handler = next(h for h in star_handlers_registry._handlers
                             if h.handler_module_path == TxruiPlugin.__module__
                             and h.handler_name == "preserve_meme_tool_history")
        meme_handler = next(h for h in star_handlers_registry._handlers
                            if h.handler_module_path == meme_main.__name__
                            and h.handler_name == "compact_meme_tool_history")
        assert txrui_handler.extras_configs["priority"] > meme_handler.extras_configs["priority"]
        # 模拟宿主以注册时持有的原始处理器执行，避免只测实例属性替换。
        await txrui_handler.handler(txrui, event, run_context, None)
        await meme_handler.handler(plugin, event, run_context, None)
        assert any(call.function.name == "send_meme"
                   for m in run_context.messages for call in m.tool_calls or [])
        compat.uninstall()
    asyncio.run(scenario())
