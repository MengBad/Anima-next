"""戳一戳（poke）回应逻辑回归测试。

核心不变量：bot 只在「被戳（target == 自身 QQ）」时才回应/反戳；
戳群内第三人必须忽略，绝不能盲触发。

历史 bug：SocialReactions 原先只从 config.qq.self_id 取自身 QQ，
而该字段生产环境常为 None（self_id 实际由适配器从 meta_event 自适应），
导致 target_id 校验短路、别人戳别人也被误判为「戳自己」而反戳。
修复后优先使用事件自带的 self_id，拿不到则保守跳过。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from anima.core.config import QQConfig, SocialConfig
from anima.cognitive.social_reactions import (
    SocialReactions,
    _parse_poke_custom,
    _raw_info_dict,
    _POKE_NEUTRAL,
)
from anima.channels.onebot11 import parse_onebot_event

BOT = 2752719822
OTHER = 999999
GROUP = 727631526


def _make_core(self_id_in_config=None, self_id_in_channel=None, poke_prob=1.0,
               member_card=None):
    cfg = MagicMock()
    cfg.social = SocialConfig(poke_back=True, poke_back_probability=poke_prob)
    cfg.qq = QQConfig(self_id=self_id_in_config)

    ch = MagicMock()
    ch.send_message = AsyncMock()
    ch.send_group_poke = AsyncMock()
    ch.self_id = self_id_in_channel
    ch.get_group_member_info = AsyncMock(
        return_value={"card": member_card} if member_card else None)

    gw = MagicMock()
    gw.channel = ch

    core = MagicMock()
    core.config = cfg
    core.gateway = gw
    return core, ch


def _poke_event(user_id, target_id, self_id, notice_type="notify", sub_type="poke",
                action=None, suffix=None, raw_info=None):
    ev = {
        "notice_type": notice_type,
        "sub_type": sub_type,
        "user_id": user_id,
        "target_id": target_id,
        "self_id": self_id,
        "group_id": GROUP,
    }
    if action is not None:
        ev["action"] = action
    if suffix is not None:
        ev["suffix"] = suffix
    if raw_info is not None:
        ev["raw_info"] = raw_info
    return ev


def _ran(ch):
    return ch.send_group_poke.called or ch.send_message.called


def test_poke_self_triggers():
    """戳 bot 自己（事件自带 self_id）→ 应回应。"""
    core, ch = _make_core(self_id_in_config=None, self_id_in_channel=None)
    sr = SocialReactions(core)
    asyncio.run(sr.handle_notice(_poke_event(100, BOT, BOT)))
    assert _ran(ch), "戳 bot 自己应当触发回应"


def test_poke_other_ignored():
    """别人戳别人（target 是第三人）→ 必须忽略。"""
    core, ch = _make_core(self_id_in_config=None, self_id_in_channel=None)
    sr = SocialReactions(core)
    asyncio.run(sr.handle_notice(_poke_event(100, OTHER, BOT)))
    assert not _ran(ch), "戳第三人不应触发回应"


def test_poke_self_with_config_self_id():
    """正常配置 self_id 的场景：戳 bot 仍应回应。"""
    core, ch = _make_core(self_id_in_config=BOT, self_id_in_channel=None)
    sr = SocialReactions(core)
    asyncio.run(sr.handle_notice(_poke_event(100, BOT, BOT)))
    assert _ran(ch), "配置 self_id 且戳 bot 自己应触发回应"


def test_poke_other_with_config_self_id_ignored():
    """正常配置 self_id 的场景：戳第三人仍应忽略。"""
    core, ch = _make_core(self_id_in_config=BOT, self_id_in_channel=None)
    sr = SocialReactions(core)
    asyncio.run(sr.handle_notice(_poke_event(100, OTHER, BOT)))
    assert not _ran(ch), "配置 self_id 且戳第三人不应触发回应"


def test_poke_self_via_channel_self_id():
    """适配器自适应 self_id（config 为 None）也能正确识别。"""
    core, ch = _make_core(self_id_in_config=None, self_id_in_channel=BOT)
    sr = SocialReactions(core)
    asyncio.run(sr.handle_notice(_poke_event(100, BOT, BOT)))
    assert _ran(ch), "经 channel.self_id 也应识别为戳自己"


def test_no_self_id_conservative_skip():
    """自身 QQ 完全无法解析时：保守跳过，绝不盲触发。"""
    core, ch = _make_core(self_id_in_config=None, self_id_in_channel=None)
    sr = SocialReactions(core)
    # 事件不带 self_id，target 写成 bot 的号也无济于事（无法确认）
    ev = _poke_event(100, BOT, None)
    asyncio.run(sr.handle_notice(ev))
    assert not _ran(ch), "无法确认 self_id 时必须保守跳过"


def test_private_poke_triggers():
    """私聊戳一戳（无第三人）→ 应回应。"""
    core, ch = _make_core(self_id_in_config=None, self_id_in_channel=None)
    sr = SocialReactions(core)
    ev = _poke_event(100, None, BOT, notice_type="private_poke", sub_type=None)
    asyncio.run(sr.handle_notice(ev))
    assert _ran(ch), "私聊戳一戳应回应"


# ---------------------------------------------------------------------------
# 自定义戳一戳文案 + NapCat 昵称佐证
# ---------------------------------------------------------------------------

def test_parse_poke_custom_top_level():
    """自定义文案提到事件顶层（action/suffix）→ 正确解析。"""
    action, suffix = _parse_poke_custom(
        _poke_event(100, BOT, BOT, action="拍了拍", suffix="的脑袋"))
    assert action == "拍了拍"
    assert suffix == "的脑袋"


def test_parse_poke_custom_raw_info_dict():
    """自定义文案藏在 raw_info(dict) → 正确解析。"""
    action, suffix = _parse_poke_custom(_poke_event(
        100, BOT, BOT, raw_info={"action": "比心", "actionText": "的后脑勺"}))
    assert action == "比心"
    assert suffix == "的后脑勺"


def test_parse_poke_custom_raw_info_json_string():
    """部分 NapCat 版本 raw_info 是 JSON 字符串 → 仍应解析。"""
    action, suffix = _parse_poke_custom(_poke_event(
        100, BOT, BOT, raw_info='{"action_str": "戳了戳", "action_str_suffix": "的脸"}'))
    assert action == "戳了戳"
    assert suffix == "的脸"


def test_compose_reply_known_action():
    """已知动作（拍了拍）+ 昵称 → 回应含动作、后缀、昵称。"""
    sr = SocialReactions(MagicMock())
    text = sr._compose_poke_reply("拍了拍", "的脑袋", "小明")
    assert "拍了拍" in text and "的脑袋" in text and "小明" in text


def test_compose_reply_unknown_action():
    """未知动作 → 用动词动态造句（仍个性化）。"""
    sr = SocialReactions(MagicMock())
    text = sr._compose_poke_reply("摸了摸", "", "小红")
    assert "摸了摸" in text and "小红" in text


def test_compose_reply_neutral():
    """完全拿不到动作 → 中性回复池。"""
    sr = SocialReactions(MagicMock())
    text = sr._compose_poke_reply(None, None, None)
    assert text in _POKE_NEUTRAL  # noqa: F821  (_POKE_NEUTRAL imported at top)


def test_poke_custom_end_to_end_personalized():
    """端到端：自定义「拍了拍」+ NapCat 拉到昵称「小明」→ 口头回应含关键词。
    用 patch 固定 random.random=0.99：概率检查通过、且走口头回应分支。"""
    core, ch = _make_core(self_id_in_config=None, self_id_in_channel=None,
                          member_card="小明")
    sr = SocialReactions(core)
    ev = _poke_event(100, BOT, BOT, action="拍了拍", suffix="的脑袋")
    with patch("anima.cognitive.social_reactions.random.random", return_value=0.99):
        asyncio.run(sr.handle_notice(ev))
    assert ch.send_message.called, "应走口头回应分支"
    sent = ch.send_message.call_args.args[2]
    assert "拍了拍" in sent and "的脑袋" in sent and "小明" in sent


def test_parse_onebot_event_forwards_raw_info():
    """parse_onebot_event 必须把 raw_info/action 透传进事件，否则自定义解析失效。"""
    raw = {
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "self_id": BOT, "user_id": 100, "target_id": BOT, "group_id": GROUP,
        "raw_info": {"action": "比心", "actionText": "的脑袋"},
    }
    ev = parse_onebot_event(raw)
    assert "raw_info" in ev, "raw_info 未透传"
    action, suffix = _parse_poke_custom(ev)
    assert action == "比心" and suffix == "的脑袋"


def test_raw_info_dict_handles_nonjson():
    """_raw_info_dict 遇到非 dict/非 JSON 应优雅返回 None。"""
    assert _raw_info_dict(123) is None
    assert _raw_info_dict("not-json") is None
    assert _raw_info_dict({"a": 1}) == {"a": 1}
