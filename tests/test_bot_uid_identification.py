"""Bot 识别系统：以 UID(QQ号) 为唯一主键，昵称启发式不误判。

覆盖：
- UID 命中已知 Bot，即使昵称改成"普通用户"仍识别为 Bot
- 仅昵称疑似 Bot（未确认 UID）不自动识别
- known_bots.json 以 UID 为键 + confirmed 字段；改名不影响识别
- 权威 UID 集 = 配置 known_bot_ids ∪ known_bots.json(confirmed)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anima.cognitive import bot_social
from anima.channels.onebot11 import is_bot_message

KNOWN = {838916454}


def test_uid_identified_even_with_plain_nickname():
    """昵称改成"普通用户"，但 UID 命中已知 Bot → 仍识别为 Bot。"""
    event = {
        "user_id": 838916454,
        "sender": {"user_id": 838916454, "nickname": "普通用户", "role": "member"},
        "group_id": 900001,
    }
    assert is_bot_message(event, KNOWN) is True
    # bot_social 等价路径
    assert bot_social.is_bot_sender(event, KNOWN) is True


def test_plain_nickname_not_auto_confirmed():
    """仅昵称疑似 Bot（UID 未确认）绝不被自动识别。"""
    event = {
        "user_id": 123456,
        "sender": {"user_id": 123456, "nickname": "小Bot助手", "role": "member"},
    }
    assert is_bot_message(event, KNOWN) is False  # 仅昵称，不识别
    # discover 仅返回 role=='bot'，昵称启发式不进返回集
    members = [{"user_id": 123456, "nickname": "小Bot助手", "role": "member"}]
    assert bot_social.discover_bots_from_members(members) == set()
    # 但建议列表会包含它（仅供人工确认，绝不自动确认）
    assert 123456 in bot_social.suggest_bots_from_nickname(members)


def test_role_bot_still_identified_without_uid():
    """平台显式 role=='bot'（非昵称启发式）仍识别，即使 UID 不在集合。"""
    event = {
        "user_id": 777888,
        "sender": {"user_id": 777888, "nickname": "某Bot", "role": "bot"},
    }
    assert is_bot_message(event, KNOWN) is True


def test_known_bots_store_confirmed_and_rename(tmp_path):
    """known_bots.json：UID 为键 + confirmed；改名不影响识别（主键是 UID）。"""
    store = bot_social.KnownBotsStore(str(tmp_path))
    store.add(838916454, "丰川刻子", confirmed=True)
    assert store.is_known(838916454) is True
    assert 838916454 in store.uids()
    # 改名只刷新展示名，不破坏关系 / 识别
    store.add(838916454, "刻子改名叫喵喵", confirmed=True)
    assert store.is_known(838916454) is True
    assert store._bots["838916454"]["bot_name"] == "刻子改名叫喵喵"
    # 文件结构与字段正确
    import json
    raw = json.loads((tmp_path / "known_bots.json").read_text(encoding="utf-8"))
    assert raw["bots"]["838916454"]["confirmed"] is True


def test_authoritative_merge_config_and_store(tmp_path):
    """权威 UID 集 = 配置 known_bot_ids ∪ known_bots.json(confirmed)。"""
    store = bot_social.KnownBotsStore(str(tmp_path))
    store.add(999, "另一只Bot", confirmed=True)
    merged = bot_social.authoritative_known_bot_uids({838916454}, str(tmp_path))
    assert 838916454 in merged  # 来自配置
    assert 999 in merged        # 来自 known_bots.json
    assert 123456 not in merged  # 未确认


def test_cognition_relation_keyed_by_uid_survives_rename(tmp_path):
    """bot_relations.json 以 UID 为键，改名不影响关系（主键 UID）。"""
    store = bot_social.BotRelationStore(str(tmp_path))
    rel = store.record_interaction("838916454", "丰川刻子", "rivalry", 1000.0)
    assert rel.rivalry > 0
    uid = rel.bot_uid
    # 改名后重新 ensure：关系主键(UID)不变，仅展示名更新
    rel2 = store.ensure(uid, "刻子改名叫喵喵")
    assert rel2.bot_uid == uid
    assert rel2.rivalry == rel.rivalry  # 竞争度保留
    assert rel2.bot_name == "刻子改名叫喵喵"
