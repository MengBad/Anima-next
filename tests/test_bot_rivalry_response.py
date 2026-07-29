"""Task4: Bot 间回应策略（根据关系）。"""
import time

from anima.cognitive import bot_social

RIVAL_UID = "838916454"
RIVAL_NAME = "丰川刻子"


def test_rivalry_context_is_hostile():
    rel = bot_social.BotRelation(bot_uid=RIVAL_UID, bot_name=RIVAL_NAME, rivalry=0.6, trust=0.2)
    ctx = bot_social.build_relation_context(rel)
    assert "带刺" in ctx or "紧张" in ctx
    assert "0.6" in ctx
    assert RIVAL_NAME in ctx


def test_trust_context_is_friendly():
    rel = bot_social.BotRelation(bot_uid=RIVAL_UID, bot_name=RIVAL_NAME, trust=0.7, familiarity=0.5)
    ctx = bot_social.build_relation_context(rel)
    assert "合作" in ctx or "撒娇" in ctx


def test_stranger_context_is_distant():
    rel = bot_social.BotRelation(bot_uid=RIVAL_UID, bot_name=RIVAL_NAME, familiarity=0.1)
    ctx = bot_social.build_relation_context(rel)
    assert "礼貌" in ctx or "疏远" in ctx


def test_rivalry_recorded_on_title_conflict(tmp_path):
    store = bot_social.BotRelationStore(str(tmp_path))
    before = store.get(RIVAL_UID)
    assert before is None
    store.record_interaction(RIVAL_UID, RIVAL_NAME, "rivalry", time.time())
    rel = store.get(RIVAL_UID)
    assert rel is not None
    assert rel.rivalry == 0.3
    assert rel.trust == 0.0  # 初始 0 减 0.2 仍夹紧为 0


def test_mentioned_me_boosts_familiarity(tmp_path):
    store = bot_social.BotRelationStore(str(tmp_path))
    ev = {
        "user_id": int(RIVAL_UID), "group_id": 900002, "self_id": 2752719822,
        "mentions_self": True, "sender_is_bot": True, "post_type": "message",
    }
    reason = bot_social.classify_bot_reply_skip(
        ev, "@哈基米 你怎么看", self_id=2752719822, now=time.time(),
        data_dir=str(tmp_path), cooldown_sec=30, max_depth=2,
    )
    assert reason is None  # 敌对 Bot @我 仍允许一次回复
    store.load()  # classify 内部用独立 BotRelationStore 实例写盘，本实例需从磁盘刷新
    rel = store.get(RIVAL_UID)
    assert rel is not None
    assert rel.familiarity == 0.1  # 熟悉度 +0.1
    # 关系上下文应可注入（敌对/友好/陌生取决于关系值，敌对分支已由 test_rivalry_context_is_hostile 覆盖）
    store.record_interaction(RIVAL_UID, RIVAL_NAME, "rivalry", time.time())
    ctx = store.get_relation_context(RIVAL_UID)
    assert ctx is not None
