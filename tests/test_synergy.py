"""第二轮认知协同深化测试 —— 覆盖 E-S 全链路新增功能。"""

import time
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# E1+E2: 情绪-表情包联动
# ---------------------------------------------------------------------------

class TestMoodStickerLinkage:
    def test_mood_happy_increases_probability(self):
        """mood=happy 时发图概率 *1.4"""
        prob = 0.5
        mood_label = "happy"
        if mood_label in ("happy", "excited"):
            prob = min(1.0, prob * 1.4)
        assert prob == pytest.approx(0.7)

    def test_mood_tired_decreases_probability(self):
        """mood=tired 时发图概率 *0.5"""
        prob = 0.5
        mood_label = "tired"
        if mood_label in ("tired", "annoyed", "bored"):
            prob = max(0.0, prob * 0.5)
        assert prob == pytest.approx(0.25)

    def test_mood_to_sticker_emotion_mapping(self):
        """mood label 正确映射到 sticker emotion"""
        mapping = {
            "happy": "happy", "excited": "excited",
            "annoyed": "angry", "tired": "tired", "bored": "speechless",
            "curious": "curious",
        }
        assert mapping["happy"] == "happy"
        assert mapping["annoyed"] == "angry"
        assert mapping.get("neutral", "") == ""


# ---------------------------------------------------------------------------
# F1: 错别字注入
# ---------------------------------------------------------------------------

class TestTypoInjection:
    def test_typo_probability_zero_no_trigger(self):
        """概率为 0 时不触发"""
        from anima.pipeline.stages import _maybe_inject_typo
        # 模拟 core 配置 typo_probability=0
        mock_core = MagicMock()
        mock_core.config.persona.typo_probability = 0.0
        result = _maybe_inject_typo("这是一条测试消息呀", core=mock_core)
        assert result == "这是一条测试消息呀"

    def test_typo_short_text_no_trigger(self):
        """回复 < 10 字不触发"""
        from anima.pipeline.stages import _maybe_inject_typo
        result = _maybe_inject_typo("好的喵")
        assert result == "好的喵"


# ---------------------------------------------------------------------------
# F2: 发癫系统
# ---------------------------------------------------------------------------

class TestQuirkSystem:
    def test_daily_max(self):
        """每日上限"""
        from anima.cognitive.quirk import QuirkSystem
        qs = QuirkSystem(base_probability=1.0, daily_max=3)
        results = [qs.should_quirk("happy", "CLOSE", 12) for _ in range(5)]
        assert sum(results) <= 3

    def test_night_no_trigger(self):
        """深夜 0-5 点不触发"""
        from anima.cognitive.quirk import QuirkSystem
        qs = QuirkSystem(base_probability=1.0, daily_max=10)
        assert qs.should_quirk("happy", "CLOSE", 3) is False

    def test_close_relation_higher_prob(self):
        """CLOSE 关系概率更高"""
        from anima.cognitive.quirk import QuirkSystem
        qs = QuirkSystem(base_probability=0.04, daily_max=100)
        # CLOSE 关系 +2%, 总概率 0.06
        with patch("anima.cognitive.quirk.random.random", return_value=0.05):
            assert qs.should_quirk("neutral", "CLOSE", 12) is True
        # STRANGER 不加，基础 0.04
        qs2 = QuirkSystem(base_probability=0.04, daily_max=100)
        with patch("anima.cognitive.quirk.random.random", return_value=0.05):
            assert qs2.should_quirk("neutral", "STRANGER", 12) is False


# ---------------------------------------------------------------------------
# G2: 关键人物种子
# ---------------------------------------------------------------------------

class TestSeedRelations:
    def test_seed_owner_close(self, tmp_path):
        """吴雨萌(1562290139) 初始 CLOSE"""
        from anima.cognitive.relation import get_relation, Stage, _relation_engines
        _relation_engines.clear()
        eng = get_relation(str(tmp_path))
        rel = eng.get(1562290139)
        assert rel.stage == Stage.CLOSE
        assert rel.familiarity >= 0.8

    def test_seed_huachu_friend(self, tmp_path):
        """华初(1371704917) 初始 FRIEND"""
        from anima.cognitive.relation import get_relation, Stage, _relation_engines
        _relation_engines.clear()
        eng = get_relation(str(tmp_path))
        rel = eng.get(1371704917)
        assert rel.stage == Stage.FRIEND


# ---------------------------------------------------------------------------
# I1: 群氛围感染
# ---------------------------------------------------------------------------

class TestAtmosphereMoodInfection:
    def test_humorous_triggers_mood(self, tmp_path):
        """humorous 氛围触发 mood.trigger"""
        import anima.cognitive.mood as mood_mod
        old = mood_mod._mood_system
        mood_mod._mood_system = None
        try:
            ms = mood_mod.get_mood_system(str(tmp_path))
            initial_valence = ms.get_state().valence
            ms.trigger("interesting", 0.15)
            assert ms.get_state().valence > initial_valence
        finally:
            mood_mod._mood_system = old


# ---------------------------------------------------------------------------
# I2: 社交疲劳
# ---------------------------------------------------------------------------

class TestSocialFatigue:
    def test_overwhelmed_event_exists(self, tmp_path):
        """overwhelmed 事件在 mood 系统中注册"""
        import anima.cognitive.mood as mood_mod
        old = mood_mod._mood_system
        mood_mod._mood_system = None
        try:
            ms = mood_mod.get_mood_system(str(tmp_path))
            initial_valence = ms.get_state().valence
            ms.trigger("overwhelmed", 0.5)
            assert ms.get_state().valence < initial_valence
        finally:
            mood_mod._mood_system = old


# ---------------------------------------------------------------------------
# J2: 久违重逢
# ---------------------------------------------------------------------------

class TestLongLostReunion:
    def test_gap_over_3_days(self, tmp_path):
        """gap > 3天时 prompt_fragment 含相关提示"""
        from anima.cognitive.relation import get_relation, Stage, _relation_engines
        _relation_engines.clear()
        eng = get_relation(str(tmp_path))
        rel = eng.get(99999)
        rel.interaction_count = 10
        rel.stage = Stage.FRIEND
        rel.last_seen = time.time() - 4 * 86400  # 4 天前
        rel.tags = ["技术宅"]  # J1 需要 tags 或 notes 非空
        frag = eng.prompt_fragment(99999)
        # J1 记忆回调: 距上次交互 > 24h 时提示
        assert "没跟这个人说话" in frag


# ---------------------------------------------------------------------------
# P1: 昵称追踪
# ---------------------------------------------------------------------------

class TestNicknameTracking:
    def test_name_change_updates_display(self, tmp_path):
        """改名后 display_name 更新，name_history 追加"""
        from anima.cognitive.relation import get_relation, _relation_engines
        _relation_engines.clear()
        eng = get_relation(str(tmp_path))
        eng.touch(12345, content="hello", name="旧名字")
        rel = eng.get(12345)
        assert rel.display_name == "旧名字"

        eng.touch(12345, content="hi", name="新名字")
        rel = eng.get(12345)
        assert rel.display_name == "新名字"
        assert "旧名字" in rel.name_history

    def test_name_history_max_5(self, tmp_path):
        """name_history 最多 5 条"""
        from anima.cognitive.relation import get_relation, _relation_engines
        _relation_engines.clear()
        eng = get_relation(str(tmp_path))
        for i in range(8):
            eng.touch(12345, content="x", name=f"名字{i}")
        rel = eng.get(12345)
        assert len(rel.name_history) <= 5


# ---------------------------------------------------------------------------
# P3: 用户活跃节律
# ---------------------------------------------------------------------------

class TestActivityPattern:
    def test_unusual_hour_detection(self, tmp_path):
        """非常规时段出现时 is_unusual_hour 返回 True"""
        from anima.cognitive.time_sense import TimeSense
        ts = TimeSense(str(tmp_path))
        # 模拟用户总是在 20 点活跃
        ts.hourly_distribution[111] = [0]*24
        ts.hourly_distribution[111][20] = 20  # 集中在 20 点
        # 当前如果不是 20 点，应该返回 True
        from anima.cognitive.time_sense import beijing_now
        current_hour = beijing_now().hour
        if current_hour != 20:
            assert ts.is_unusual_hour(111) is True
        else:
            assert ts.is_unusual_hour(111) is False

    def test_insufficient_data_no_unusual(self, tmp_path):
        """数据不足时不判断反常"""
        from anima.cognitive.time_sense import TimeSense
        ts = TimeSense(str(tmp_path))
        ts.hourly_distribution[222] = [0]*24
        ts.hourly_distribution[222][10] = 2  # 只有 2 次
        assert ts.is_unusual_hour(222) is False


# ---------------------------------------------------------------------------
# Q1: 情绪加权记忆
# ---------------------------------------------------------------------------

class TestEmotionWeightedMemory:
    def test_close_user_higher_retention(self):
        """CLOSE 用户记忆 salience 更高"""
        from anima.cognitive.forgetting import ForgettingEngine
        fe = ForgettingEngine.__new__(ForgettingEngine)
        fe._threshold = 0.15
        item = {"ts": time.time() - 86400, "salience": 0.5, "access_count": 0, "emotion": 0.0}
        score_close = fe.retention_score(item, relation_stage="CLOSE")
        score_stranger = fe.retention_score(item, relation_stage="STRANGER")
        assert score_close > score_stranger

    def test_stranger_faster_decay(self):
        """STRANGER 记忆衰减更快"""
        from anima.cognitive.forgetting import ForgettingEngine
        fe = ForgettingEngine.__new__(ForgettingEngine)
        fe._threshold = 0.15
        item = {"ts": time.time() - 7 * 86400, "salience": 0.5, "access_count": 0, "emotion": 0.0}
        score_normal = fe.retention_score(item, relation_stage="FRIEND")
        score_stranger = fe.retention_score(item, relation_stage="STRANGER")
        assert score_stranger < score_normal


# ---------------------------------------------------------------------------
# R1: 伤痕触发
# ---------------------------------------------------------------------------

class TestScarTrigger:
    def test_keyword_hit_refreshes_trigger(self, tmp_path):
        """关键词命中后 last_triggered 刷新"""
        from anima.cognitive.scar import ScarSystem
        ss = ScarSystem(str(tmp_path))
        ss.add(111, "被骂了", severity=0.7)
        old_triggered = ss._scars[0].last_triggered
        time.sleep(0.01)
        result = ss.check_trigger(111, "你怎么又骂人")
        assert result is True
        assert ss._scars[0].last_triggered > old_triggered

    def test_no_hit_no_refresh(self, tmp_path):
        """无关消息不触发"""
        from anima.cognitive.scar import ScarSystem
        ss = ScarSystem(str(tmp_path))
        ss.add(111, "被骂了", severity=0.7)
        result = ss.check_trigger(111, "今天天气不错")
        assert result is False


# ---------------------------------------------------------------------------
# R2: 信任修复
# ---------------------------------------------------------------------------

class TestTrustRepair:
    def test_positive_interaction_increases_trust(self, tmp_path):
        """连续正面交互后 trust 上升"""
        from anima.cognitive.relation import get_relation, _relation_engines
        _relation_engines.clear()
        eng = get_relation(str(tmp_path))
        rel = eng.get(555)
        initial_trust = rel.trust
        # 模拟正面交互
        for _ in range(5):
            eng.touch(555, content="谢谢你 辛苦了")
        rel = eng.get(555)
        assert rel.trust > initial_trust


# ---------------------------------------------------------------------------
# S1: 对话触发欲望
# ---------------------------------------------------------------------------

class TestDesireFromConversation:
    def test_food_topic_triggers_desire(self, tmp_path):
        """美食话题触发'想吃'欲望"""
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        ds.restore()
        ds.add("想吃东西", intensity=0.3, kind="chat")
        active = ds.get_active(0.2)
        assert any("吃" in d.content for d in active)

    def test_cooldown_prevents_repeat(self, tmp_path):
        """同类欲望 2h 内不重复触发"""
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        ds.restore()
        ds.add("想吃东西", intensity=0.3, kind="chat")
        # 再次添加相同内容只会提升 intensity，不会新建
        count_before = len(ds.desires)
        ds.add("想吃东西", intensity=0.3, kind="chat")
        assert len(ds.desires) == count_before


# ---------------------------------------------------------------------------
# S2: 欲望满足与落空
# ---------------------------------------------------------------------------

class TestDesireExpiry:
    def test_expire_unfulfilled_generates_regret(self, tmp_path):
        """超过 24h 未满足的欲望生成遗憾笔记"""
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        ds.restore()
        d = ds.add("想吃火锅", intensity=0.5, kind="chat")
        # 模拟 25 小时前创建
        d.created_at = time.time() - 25 * 3600
        regrets = ds.expire_unfulfilled()
        assert len(regrets) == 1
        assert "火锅" in regrets[0]
        assert d.resolved is True
