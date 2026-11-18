"""会话类型（chat_type）解析测试。

与 txrui core 侧口径对齐：
- core 白名单是 ``private`` / ``group``
- 缺失时留空串，**不谎报私信**（前端显示「未知」）

覆盖：
- 顶层 ``chat_type`` -> GROUP_MESSAGE / FRIEND_MESSAGE
- ``extra.chat_type`` 回退（部分采集端把类型放在 extra 里）
- 大小写 / 空白归一化
- 缺失 / 无法识别 -> OTHER_MESSAGE（不谎报私信）
- 群消息的 group_id 用 chat_id
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pytest

from astrbot.api.platform import MessageType

from astrbot_plugin_txrui.txrui_adapter import TxruiAdapter


def _adapter(tmp_path) -> TxruiAdapter:
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
    return TxruiAdapter(cfg, {}, asyncio.Queue())


def _payload(chat_id="会话A", chat_type=None, *, extra=None):
    """构造一条最小 event payload。

    `chat_type=None` 表示**不放**这个键（模拟缺失）。
    """
    payload = {
        "chat_id": chat_id,
        "sender": {"platform_user_id": "u1", "display_name": "张三"},
        "content": [{"type": "text", "text": "你好"}],
    }
    if chat_type is not None:
        payload["chat_type"] = chat_type
    if extra is not None:
        payload["extra"] = extra
    return payload


class TestResolveChatType:
    def test_top_level_private(self, tmp_path):
        assert TxruiAdapter._resolve_chat_type({"chat_type": "private"}) == "private"

    def test_top_level_group(self, tmp_path):
        assert TxruiAdapter._resolve_chat_type({"chat_type": "group"}) == "group"

    def test_extra_fallback(self, tmp_path):
        """顶层没有时，回退到 extra.chat_type。"""
        assert (
            TxruiAdapter._resolve_chat_type({"extra": {"chat_type": "group"}})
            == "group"
        )

    def test_top_level_wins_over_extra(self, tmp_path):
        """顶层 chat_type 优先于 extra。"""
        assert (
            TxruiAdapter._resolve_chat_type(
                {"chat_type": "private", "extra": {"chat_type": "group"}}
            )
            == "private"
        )

    def test_case_and_whitespace_normalized(self, tmp_path):
        """大小写与空白归一化。"""
        assert TxruiAdapter._resolve_chat_type({"chat_type": "  GROUP "}) == "group"
        assert TxruiAdapter._resolve_chat_type({"chat_type": "Group"}) == "group"

    def test_missing_returns_empty_string(self, tmp_path):
        """缺失时返回空串，**不谎报成 private**。"""
        assert TxruiAdapter._resolve_chat_type({}) == ""
        assert TxruiAdapter._resolve_chat_type({"chat_type": ""}) == ""
        assert TxruiAdapter._resolve_chat_type({"chat_type": None}) == ""

    def test_non_dict_extra_is_ignored(self, tmp_path):
        """extra 不是 dict 时安全跳过。"""
        assert TxruiAdapter._resolve_chat_type({"extra": "oops"}) == ""
        assert TxruiAdapter._resolve_chat_type({"extra": ["group"]}) == ""


class TestMessageTypeMapping:
    def test_group_maps_to_group_message(self, tmp_path):
        """chat_type=group -> GroupMessage，group_id 用 chat_id。"""
        adapter = _adapter(tmp_path)
        msg = adapter.convert_message(_payload("群A", "group"))
        assert msg is not None
        assert msg.type == MessageType.GROUP_MESSAGE
        assert msg.group_id == "群A"
        assert msg.group is not None
        assert msg.group.group_id == "群A"

    def test_private_maps_to_friend_message(self, tmp_path):
        """chat_type=private -> FriendMessage，无 group。"""
        adapter = _adapter(tmp_path)
        msg = adapter.convert_message(_payload("李四", "private"))
        assert msg is not None
        assert msg.type == MessageType.FRIEND_MESSAGE
        assert msg.group_id == ""
        assert msg.group is None

    def test_extra_chat_type_maps_to_group_message(self, tmp_path):
        """extra.chat_type=group 也能正确识别为群聊。"""
        adapter = _adapter(tmp_path)
        msg = adapter.convert_message(
            _payload("群B", None, extra={"chat_type": "group"})
        )
        assert msg is not None
        assert msg.type == MessageType.GROUP_MESSAGE
        assert msg.group_id == "群B"

    def test_missing_chat_type_does_not_claim_private(self, tmp_path):
        """缺失类型 -> OTHER_MESSAGE，不谎报私信。"""
        adapter = _adapter(tmp_path)
        msg = adapter.convert_message(_payload("某人"))
        assert msg is not None
        assert msg.type == MessageType.OTHER_MESSAGE
        assert msg.group_id == ""

    def test_unknown_chat_type_is_other_message(self, tmp_path):
        """无法识别的类型 -> OTHER_MESSAGE。"""
        adapter = _adapter(tmp_path)
        msg = adapter.convert_message(_payload("某人", "channel"))
        assert msg is not None
        assert msg.type == MessageType.OTHER_MESSAGE

    def test_uppercase_group_still_group(self, tmp_path):
        """大小写归一化后仍按群聊处理。"""
        adapter = _adapter(tmp_path)
        msg = adapter.convert_message(_payload("群C", "GROUP"))
        assert msg is not None
        assert msg.type == MessageType.GROUP_MESSAGE
        assert msg.group_id == "群C"

    def test_session_id_is_chat_id(self, tmp_path):
        """session_id 始终是 chat_id（群/私聊一致）。"""
        adapter = _adapter(tmp_path)
        for chat_type in ("group", "private", None):
            msg = adapter.convert_message(_payload("会话X", chat_type))
            assert msg is not None
            assert msg.session_id == "会话X"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
