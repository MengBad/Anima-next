"""Task1: Bot 识别系统。"""
import time

from anima.cognitive import bot_social


def test_is_bot_sender_flag_true():
    ev = {"user_id": 838916454, "sender_is_bot": True}
    assert bot_social.is_bot_sender(ev) is True


def test_is_bot_sender_role_bot():
    ev = {"user_id": 838916454, "sender": {"role": "bot", "nickname": "丰川刻子"}}
    assert bot_social.is_bot_sender(ev) is True


def test_is_bot_sender_human_false():
    ev = {"user_id": 123456, "sender": {"role": "member", "nickname": "阿布"}}
    assert bot_social.is_bot_sender(ev) is False


def test_is_bot_sender_known_ids():
    # sender_is_bot 未置位，但 uid 命中 known_ids
    ev = {"user_id": 838916454, "sender": {"role": "member"}}
    assert bot_social.is_bot_sender(ev, known_ids={838916454}) is True
    assert bot_social.is_bot_sender(ev, known_ids={111}) is False


def test_is_bot_uid():
    assert bot_social.is_bot_uid(838916454, {838916454, 999}) is True
    assert bot_social.is_bot_uid("838916454", {838916454}) is True
    assert bot_social.is_bot_uid(123, {838916454}) is False
    assert bot_social.is_bot_uid(None, {1}) is False


def test_discover_bots_from_members():
    members = [
        {"user_id": 838916454, "nickname": "丰川刻子", "role": "member"},
        {"user_id": 777, "nickname": "张三", "role": "member"},
        {"user_id": 888, "nickname": "BotHelper", "role": "member"},
        {"user_id": 999, "nickname": "管事喵", "role": "bot"},
    ]
    found = bot_social.discover_bots_from_members(members, strict=True)
    # 仅平台 role=='bot' 被返回；昵称启发式（丰川刻子/BotHelper）不再自动确认
    assert found == {999}
    assert 838916454 not in found  # 昵称疑似 Bot，但不自动确认
    assert 888 not in found        # 昵称命中 hint，但不自动确认
    assert 777 not in found        # 真人「张三」不被误判
    # 昵称启发式仍「发现」并仅作建议（不确认）
    suggested = bot_social.suggest_bots_from_nickname(members)
    assert 838916454 in suggested
    assert 888 in suggested


def test_event_mark_symmetry():
    # 模拟 onebot11 dispatch 设置的标记
    ev = {"user_id": 838916454, "sender_is_bot": True, "_is_bot_sender": True}
    assert bot_social.is_bot_sender(ev) is True
