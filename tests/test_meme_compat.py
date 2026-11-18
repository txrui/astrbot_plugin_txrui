"""人格兼容范围、并发隔离、卸载和新接口兼容测试。"""
import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_txrui.txrui_meme_compat import MemePersonaCompatibility


class ModernPersona:
    async def resolve_selected_persona(self, *, umo, platform_name):
        await asyncio.sleep(0)
        return ("effective-persona", {"name": "effective-persona"}, False)


class Meme:
    def __init__(self, context):
        self.context = context

    async def _inject_meme_prompt_impl(self, event, req):
        a, b, c, d = await self.context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            platform_name=event.get_platform_name(),
            conversation_persona_id="obsolete-conversation-persona",
            provider_settings={"persona_id": "obsolete-config-persona"},
        )
        return a, b, c, d


def setup(persona=None):
    persona = persona or ModernPersona()
    context = SimpleNamespace(persona_manager=persona)
    meme = Meme(context)
    metadata = SimpleNamespace(root_dir_name="meme_manager", star_cls=meme, activated=True)
    context.get_all_stars = lambda: [metadata]
    compatibility = MemePersonaCompatibility()
    compatibility.install(context)
    return compatibility, context, meme, metadata


def event(platform="txrui"):
    return SimpleNamespace(get_platform_name=lambda: platform, unified_msg_origin=f"{platform}:FriendMessage:friend")


def test_txrui_legacy_call_and_original_interface_remain_distinct():
    async def scenario():
        compat, context, meme, _ = setup()
        result = await meme._inject_meme_prompt_impl(event(), None)
        assert result == ("effective-persona", {"name": "effective-persona"}, False, None)
        assert len(await context.persona_manager.resolve_selected_persona(umo="x", platform_name="txrui")) == 3
        # 作用域已经退出：即使访问表情插件代理，也不再兼容历史参数。
        with pytest.raises(TypeError, match="conversation_persona_id"):
            await meme.context.persona_manager.resolve_selected_persona(
                umo="x", platform_name="txrui", conversation_persona_id="old")
        compat.uninstall()
        assert meme.context is context
        assert "_inject_meme_prompt_impl" not in meme.__dict__
    asyncio.run(scenario())


def test_other_platform_concurrent_request_is_not_changed():
    async def scenario():
        _, _, meme, _ = setup()
        results = await asyncio.gather(
            meme._inject_meme_prompt_impl(event(), None),
            meme._inject_meme_prompt_impl(event("qq_official"), None),
            return_exceptions=True,
        )
        assert results[0][0] == "effective-persona"
        assert isinstance(results[1], TypeError)
    asyncio.run(scenario())


def test_exception_does_not_leak_scope():
    class BrokenPersona(ModernPersona):
        async def resolve_selected_persona(self, *, umo, platform_name):
            raise RuntimeError("database failure")

    async def scenario():
        _, _, meme, _ = setup(BrokenPersona())
        with pytest.raises(RuntimeError, match="database failure"):
            await meme._inject_meme_prompt_impl(event(), None)
        with pytest.raises(TypeError):
            await meme.context.persona_manager.resolve_selected_persona(
                umo="x", platform_name="txrui", provider_settings={})
    asyncio.run(scenario())


def test_old_host_keeps_original_parameters_and_four_values():
    class OldPersona:
        async def resolve_selected_persona(self, *, umo, platform_name,
                                           conversation_persona_id, provider_settings):
            assert conversation_persona_id == "obsolete-conversation-persona"
            return ("legacy", None, False, "unchanged")

    async def scenario():
        _, _, meme, _ = setup(OldPersona())
        assert await meme._inject_meme_prompt_impl(event(), None) == ("legacy", None, False, "unchanged")
    asyncio.run(scenario())


def test_install_is_idempotent_and_supports_reloaded_meme():
    compat, context, meme, metadata = setup()
    wrapper = meme._inject_meme_prompt_impl
    compat.install(context)
    assert meme._inject_meme_prompt_impl is wrapper
    new_meme = Meme(context)
    metadata.star_cls = new_meme
    compat.install(context)
    assert new_meme.context is not context
    compat.uninstall()
    assert new_meme.context is context
    assert meme.context is context


def test_new_plugin_call_without_legacy_parameters_still_returns_three():
    class NewMeme(Meme):
        async def _inject_meme_prompt_impl(self, event, req):
            return await self.context.persona_manager.resolve_selected_persona(
                umo=event.unified_msg_origin, platform_name=event.get_platform_name())

    async def scenario():
        compat, context, _, metadata = setup()
        metadata.star_cls = NewMeme(context)
        compat.install(context)
        assert len(await metadata.star_cls._inject_meme_prompt_impl(event(), None)) == 3
    asyncio.run(scenario())
