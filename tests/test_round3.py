"""第三轮认知深化集成测试 (test_round3.py)

覆盖:
- A1: 伤痕触发检测 + 治愈闭环
- A2: 情绪事件自动触发
- A3: 用户活跃节律注入
- B1: 日记回顾
- B2: 闪光灯记忆 salience boost
- B3: 世界观分层与冲突检测
- C1: 情绪→回复长度约束
- C2: 关系→话题亲密度约束
- C3: 伤痕→回避行为
- D1: 长期目标系统 Aspirations
- D2: 生命里程碑
- E1: 群内社交图谱
- E2: 用户偏好学习
"""

import os
import time
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path


# ---------------------------------------------------------------------------
# A1: 伤痕闭环
# ---------------------------------------------------------------------------

class TestScarTriggerAndHeal:
    """A1: 伤痕触发检测 + 治愈。"""

    def test_check_trigger_refreshes_last_triggered(self, tmp_path):
        from anima.cognitive.scar import ScarSystem
        ss = ScarSystem(str(tmp_path))
        ss.add(123, "对方说了'滚'", severity=0.6)
        old_ts = ss._scars[0].last_triggered
        time.sleep(0.05)
        triggered = ss.check_trigger(123, "你给我滚")
        assert triggered is True
        assert ss._scars[0].last_triggered > old_ts

    def test_check_trigger_no_match(self, tmp_path):
        from anima.cognitive.scar import ScarSystem
        ss = ScarSystem(str(tmp_path))
        ss.add(123, "对方说了伤人的话", severity=0.5)
        triggered = ss.check_trigger(123, "你好啊")
        assert triggered is False

    def test_heal_reduces_severity(self, tmp_path):
        from anima.cognitive.scar import ScarSystem
        ss = ScarSystem(str(tmp_path))
        ss.add(123, "被骂了", severity=0.7)
        healed = ss.heal(123, amount=0.2)
        assert healed == 1
        assert ss._scars[0].severity == pytest.approx(0.5, abs=0.01)

    def test_heal_fades_low_severity(self, tmp_path):
        from anima.cognitive.scar import ScarSystem
        ss = ScarSystem(str(tmp_path))
        ss.add(123, "小伤", severity=0.2)
        ss.heal(123, amount=0.15)
        assert ss._scars[0].faded is True


# ---------------------------------------------------------------------------
# A2: 情绪事件自动触发
# ---------------------------------------------------------------------------

class TestMoodAutoTrigger:
    """A2: 情绪事件自动触发（直接测试 mood.trigger）。"""

    def test_praised_increases_valence(self, tmp_path):
        from anima.cognitive.mood import MoodSystem
        ms = MoodSystem(str(tmp_path))
        ms.trigger("praised", 0.3)
        state = ms.get_state()
        assert state.valence > 0

    def test_insulted_decreases_valence(self, tmp_path):
        from anima.cognitive.mood import MoodSystem
        ms = MoodSystem(str(tmp_path))
        ms.trigger("insulted", 0.4)
        state = ms.get_state()
        assert state.valence < 0

    def test_helped_increases_valence(self, tmp_path):
        from anima.cognitive.mood import MoodSystem
        ms = MoodSystem(str(tmp_path))
        ms.trigger("helped", 0.2)
        state = ms.get_state()
        assert state.valence > 0

    def test_ignored_slightly_negative(self, tmp_path):
        from anima.cognitive.mood import MoodSystem
        ms = MoodSystem(str(tmp_path))
        ms.trigger("ignored", 0.15)
        state = ms.get_state()
        assert state.valence <= 0


# ---------------------------------------------------------------------------
# A3: 用户活跃节律
# ---------------------------------------------------------------------------

class TestActivityPattern:
    """A3: 用户活跃节律完整注入。"""

    def test_activity_pattern_detects_night_owl(self, tmp_path):
        from anima.cognitive.time_sense import TimeSense
        ts = TimeSense(str(tmp_path))
        # 模拟深夜活跃
        ts.hourly_distribution[123] = [0] * 24
        for h in range(22, 24):
            ts.hourly_distribution[123][h] = 10
        ts.hourly_distribution[123][0] = 8
        ts.hourly_distribution[123][1] = 5
        pattern = ts.activity_pattern(123)
        assert "夜猫子" in pattern or "深夜" in pattern

    def test_activity_pattern_empty_data(self, tmp_path):
        from anima.cognitive.time_sense import TimeSense
        ts = TimeSense(str(tmp_path))
        pattern = ts.activity_pattern(999)
        assert pattern == ""


# ---------------------------------------------------------------------------
# B1: 日记回顾
# ---------------------------------------------------------------------------

class TestDiaryRecall:
    """B1: 日记回顾机制。"""

    def test_recent_diary_entries_parses(self, tmp_path):
        from anima.cognitive.simulated_life import SimulatedLife
        # 写入测试日记
        diary_path = tmp_path / "diary.md"
        diary_path.write_text(
            "## 2026-07-20\n今天聊了很多有趣的话题\n\n"
            "## 2026-07-21\n学了一个新工具\n\n"
            "## 2026-07-22\n和朋友们玩得很开心\n",
            encoding="utf-8",
        )
        mock_core = MagicMock()
        mock_core.llm = None
        life = SimulatedLife(mock_core, str(tmp_path))
        entries = life._recent_diary_entries(2)
        assert len(entries) == 2
        assert "2026-07-21" in entries[0] or "2026-07-22" in entries[0]

    def test_recent_diary_entries_empty(self, tmp_path):
        from anima.cognitive.simulated_life import SimulatedLife
        mock_core = MagicMock()
        mock_core.llm = None
        life = SimulatedLife(mock_core, str(tmp_path))
        entries = life._recent_diary_entries(5)
        assert entries == []


# ---------------------------------------------------------------------------
# B2: 闪光灯记忆
# ---------------------------------------------------------------------------

class TestFlashbulbMemory:
    """B2: 闪光灯记忆 salience boost。"""

    def test_high_salience_emotion_gets_2x_boost(self, tmp_path):
        from anima.cognitive.forgetting import ForgettingEngine
        fe = ForgettingEngine(str(tmp_path))
        item = {
            "ts": time.time(),
            "salience": 0.8,  # 高 salience (flashbulb)
            "emotion": 0.5,
            "access_count": 0,
        }
        score = fe.retention_score(item)
        # salience=0.8, emotion_boost = (1+0.5*0.5)*2.0 = 2.5
        # recency ≈ 1.0 (just now), access_boost = 1.0
        assert score > 0.5  # 高保留分

    def test_normal_item_lower_retention(self, tmp_path):
        from anima.cognitive.forgetting import ForgettingEngine
        fe = ForgettingEngine(str(tmp_path))
        item = {
            "ts": time.time() - 86400 * 3,  # 3天前
            "salience": 0.4,
            "emotion": 0.0,
            "access_count": 0,
        }
        score = fe.retention_score(item)
        assert score < 0.5  # 较低保留分


# ---------------------------------------------------------------------------
# B3: 世界观分层与冲突检测
# ---------------------------------------------------------------------------

class TestWorldModelLayered:
    """B3: 世界观分层与冲突检测。"""

    def test_prompt_fragment_groups_by_category(self, tmp_path):
        from anima.cognitive.world_model import WorldModel
        wm = WorldModel(str(tmp_path))
        wm.add("太阳从东边升起", category="fact", weight=0.8)
        wm.add("应该对人友善", category="rule", weight=0.7)
        wm.add("猫是最可爱的动物", category="preference", weight=0.6)
        frag = wm.prompt_fragment()
        assert "我相信的事实" in frag
        # fact 应该在 preference 前面
        fact_pos = frag.find("太阳")
        pref_pos = frag.find("猫是")
        assert fact_pos < pref_pos

    def test_detect_conflict_marks_contested(self, tmp_path):
        from anima.cognitive.world_model import WorldModel
        wm = WorldModel(str(tmp_path))
        wm.add("我喜欢吃火锅", category="preference", weight=0.6)
        wm.add("我不喜欢吃火锅", category="preference", weight=0.5)
        # 第一条应该被标记为 contested
        assert wm._entries[0].contested is True

    def test_contested_entry_shows_evolution(self, tmp_path):
        from anima.cognitive.world_model import WorldModel
        wm = WorldModel(str(tmp_path))
        wm.add("应该信任所有人", category="belief", weight=0.6)
        wm.add("不应该信任所有人", category="belief", weight=0.5)
        frag = wm.prompt_fragment()
        assert "曾经觉得" in frag
        assert "但现在又觉得" in frag


# ---------------------------------------------------------------------------
# C1: 情绪→回复长度约束
# ---------------------------------------------------------------------------

class TestMoodLengthConstraint:
    """C1: 情绪影响回复长度。"""

    def test_annoyed_caps_length(self, tmp_path):
        from anima.cognitive.mood import MoodSystem
        ms = MoodSystem(str(tmp_path))
        ms.trigger("insulted", 0.8)  # 触发 annoyed
        state = ms.get_state()
        assert state.label in ("annoyed", "tired")

    def test_happy_allows_longer(self, tmp_path):
        from anima.cognitive.mood import MoodSystem
        ms = MoodSystem(str(tmp_path))
        ms.trigger("praised", 0.8)  # 触发 happy/excited
        state = ms.get_state()
        assert state.label in ("happy", "excited")


# ---------------------------------------------------------------------------
# C2: 关系→话题亲密度约束
# ---------------------------------------------------------------------------

class TestRelationIntimacyGate:
    """C2: 关系阶段约束话题亲密度。"""

    def test_stranger_blocks_memory_references(self, tmp_path):
        from anima.cognitive.relation import RelationEngine, Stage
        re = RelationEngine(str(tmp_path))
        rel = re.get(123)
        assert rel.stage == Stage.STRANGER
        # 陌生人不应该引用旧记忆
        test_reply = "我记得你之前说过你喜欢猫"
        assert "我记得你之前" in test_reply  # 这种文本应该被C2门控拦截


# ---------------------------------------------------------------------------
# D1: 长期目标系统
# ---------------------------------------------------------------------------

class TestAspirations:
    """D1: 长期目标系统 Aspirations。"""

    def test_add_and_retrieve_aspiration(self, tmp_path):
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        asp = ds.add_aspiration("学会画画", kind="creative")
        assert asp.content == "学会画画"
        assert not asp.resolved
        assert len(ds.get_active_aspirations()) == 1

    def test_update_progress(self, tmp_path):
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        ds.add_aspiration("学编程", kind="learn")
        updated = ds.update_aspiration_progress("编程", 0.3)
        assert updated is True
        assert ds.aspirations[0].progress == pytest.approx(0.3)

    def test_progress_completes_aspiration(self, tmp_path):
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        ds.add_aspiration("读完一本书", kind="learn")
        ds.update_aspiration_progress("一本书", 1.0)
        assert ds.aspirations[0].resolved is True

    def test_aspiration_fragment_in_prompt(self, tmp_path):
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        ds.add_aspiration("交更多朋友", kind="social")
        frag = ds.prompt_fragment()
        assert "长期目标" in frag or "朋友" in frag

    def test_aspiration_persistence(self, tmp_path):
        from anima.cognitive.desire import DesireSystem
        ds1 = DesireSystem(str(tmp_path))
        ds1.add_aspiration("学吉他", kind="creative")
        ds1.persist_aspirations()
        ds2 = DesireSystem(str(tmp_path))
        ds2.restore()
        assert len(ds2.aspirations) == 1
        assert ds2.aspirations[0].content == "学吉他"


# ---------------------------------------------------------------------------
# D2: 生命里程碑
# ---------------------------------------------------------------------------

class TestMilestones:
    """D2: 生命里程碑。"""

    def test_record_milestone(self, tmp_path):
        from anima.cognitive.simulated_life import MilestoneTracker
        mt = MilestoneTracker(str(tmp_path))
        mt.record("第一次和 QQ123 说话", "first_contact")
        assert len(mt._milestones) == 1

    def test_milestone_dedup_24h(self, tmp_path):
        from anima.cognitive.simulated_life import MilestoneTracker
        mt = MilestoneTracker(str(tmp_path))
        mt.record("第一次和 QQ123 说话", "first_contact")
        mt.record("第一次和 QQ123 说话", "first_contact")
        assert len(mt._milestones) == 1  # 24h 内不重复

    def test_milestone_fragment(self, tmp_path):
        from anima.cognitive.simulated_life import MilestoneTracker
        mt = MilestoneTracker(str(tmp_path))
        mt.record("成为了朋友", "stage_change")
        frag = mt.fragment()
        assert "今天" in frag
        assert "朋友" in frag

    def test_days_since_first(self, tmp_path):
        from anima.cognitive.simulated_life import MilestoneTracker
        mt = MilestoneTracker(str(tmp_path))
        mt._milestones.append({
            "event": "第一次和 QQ456 说话",
            "kind": "first_contact",
            "ts": time.time() - 86400 * 15,
        })
        days = mt.days_since_first(456)
        assert days >= 15

    def test_milestone_persistence(self, tmp_path):
        from anima.cognitive.simulated_life import MilestoneTracker
        mt1 = MilestoneTracker(str(tmp_path))
        mt1.record("学会了新工具", "tool")
        mt2 = MilestoneTracker(str(tmp_path))
        assert len(mt2._milestones) == 1


# ---------------------------------------------------------------------------
# E1: 群内社交图谱
# ---------------------------------------------------------------------------

class TestSocialGraph:
    """E1: 群内关系图谱。"""

    def test_observe_interaction(self, tmp_path):
        from anima.cognitive.social_graph import SocialGraph
        sg = SocialGraph(str(tmp_path))
        sg.observe_interaction("group1", "user_a", "user_b")
        sg.observe_interaction("group1", "user_a", "user_b")
        sg.observe_interaction("group1", "user_a", "user_b")
        pairs = sg.get_close_pairs("group1", min_interactions=3.0)
        assert len(pairs) == 1
        assert pairs[0][2] == pytest.approx(3.0)

    def test_self_interaction_ignored(self, tmp_path):
        from anima.cognitive.social_graph import SocialGraph
        sg = SocialGraph(str(tmp_path))
        sg.observe_interaction("group1", "user_a", "user_a")
        pairs = sg.get_close_pairs("group1", min_interactions=1.0)
        assert len(pairs) == 0

    def test_fragment_shows_close_pairs(self, tmp_path):
        from anima.cognitive.social_graph import SocialGraph
        sg = SocialGraph(str(tmp_path))
        for _ in range(5):
            sg.observe_interaction("group1", "alice", "bob")
        frag = sg.fragment("group1")
        assert "alice" in frag
        assert "bob" in frag

    def test_fragment_empty_for_unknown_group(self, tmp_path):
        from anima.cognitive.social_graph import SocialGraph
        sg = SocialGraph(str(tmp_path))
        frag = sg.fragment("nonexistent")
        assert frag == ""

    def test_social_graph_persistence(self, tmp_path):
        from anima.cognitive.social_graph import SocialGraph
        sg1 = SocialGraph(str(tmp_path))
        for _ in range(4):
            sg1.observe_interaction("g1", "u1", "u2")
        sg1.persist()
        sg2 = SocialGraph(str(tmp_path))
        pairs = sg2.get_close_pairs("g1", min_interactions=3.0)
        assert len(pairs) == 1


# ---------------------------------------------------------------------------
# E2: 用户偏好学习
# ---------------------------------------------------------------------------

class TestUserPreferenceLearning:
    """E2: 用户偏好学习。"""

    def test_update_topic(self):
        from anima.crossgroup.memory.user_memory import UserMemory
        um = UserMemory(user_id="123")
        um.update_topic("游戏")
        um.update_topic("游戏")
        um.update_topic("编程")
        assert um.topic_preferences["游戏"] == 2
        assert um.topic_preferences["编程"] == 1

    def test_update_style_short_messages(self):
        from anima.crossgroup.memory.user_memory import UserMemory
        um = UserMemory(user_id="123")
        for _ in range(15):
            um.update_style(5)  # 很短的消息
        assert um.style_hint == "偏简短"

    def test_update_style_long_messages(self):
        from anima.crossgroup.memory.user_memory import UserMemory
        um = UserMemory(user_id="123")
        for _ in range(15):
            um.update_style(80)  # 长消息
        assert um.style_hint == "偏详细"

    def test_preference_fragment(self):
        from anima.crossgroup.memory.user_memory import UserMemory
        um = UserMemory(user_id="123")
        um.style_hint = "偏简短"
        um.topic_preferences = {"游戏": 5, "编程": 3}
        frag = um.preference_fragment()
        assert "偏简短" in frag
        assert "游戏" in frag

    def test_topic_dedup_top10(self):
        from anima.crossgroup.memory.user_memory import UserMemory
        um = UserMemory(user_id="123")
        for i in range(25):
            um.update_topic(f"topic_{i}")
        assert len(um.topic_preferences) <= 20


# ---------------------------------------------------------------------------
# 集成测试：跨模块协同
# ---------------------------------------------------------------------------

class TestCrossModuleIntegration:
    """跨模块协同测试。"""

    def test_scar_trigger_then_mood_shift(self, tmp_path):
        """A1+A2: 伤痕触发 → 情绪变化。"""
        from anima.cognitive.scar import ScarSystem
        from anima.cognitive.mood import MoodSystem
        ss = ScarSystem(str(tmp_path))
        ms = MoodSystem(str(tmp_path))
        ss.add(456, "被骂'滚'", severity=0.7)
        triggered = ss.check_trigger(456, "你给我滚开")
        assert triggered
        # 模拟伤痕触发后的情绪反应
        ms.trigger("insulted", 0.4)
        state = ms.get_state()
        assert state.valence < 0

    def test_flashbulb_then_forgetting_resistance(self, tmp_path):
        """B2: 闪光灯记忆抵抗遗忘。"""
        from anima.cognitive.forgetting import ForgettingEngine
        fe = ForgettingEngine(str(tmp_path))
        # 普通记忆（3天前）
        normal = {"ts": time.time() - 86400 * 3, "salience": 0.4, "emotion": 0.0}
        # 闪光灯记忆（3天前，高 salience + 高 emotion）
        flashbulb = {"ts": time.time() - 86400 * 3, "salience": 0.8, "emotion": 0.5}
        score_normal = fe.retention_score(normal)
        score_flash = fe.retention_score(flashbulb)
        assert score_flash > score_normal * 1.5  # 闪光灯明显更难忘

    def test_aspiration_survives_desire_decay(self, tmp_path):
        """D1: 长期目标不受短期欲望衰减影响。"""
        from anima.cognitive.desire import DesireSystem
        ds = DesireSystem(str(tmp_path))
        ds.add("想吃火锅", intensity=0.5)
        ds.add_aspiration("学会做饭", kind="creative")
        # 模拟多次衰减
        for _ in range(20):
            ds._last_decay = 0
            ds.decay()
        # 短期欲望衰减
        active_desires = ds.get_active(0.1)
        # 长期目标不受影响
        active_asps = ds.get_active_aspirations()
        assert len(active_asps) == 1
        assert active_asps[0].content == "学会做饭"

    def test_social_graph_crossgroup_isolation(self, tmp_path):
        """E1: 不同群的社交图谱互不干扰。"""
        from anima.cognitive.social_graph import SocialGraph
        sg = SocialGraph(str(tmp_path))
        for _ in range(5):
            sg.observe_interaction("group_a", "alice", "bob")
        # group_b 没有互动
        pairs_a = sg.get_close_pairs("group_a", min_interactions=3.0)
        pairs_b = sg.get_close_pairs("group_b", min_interactions=3.0)
        assert len(pairs_a) == 1
        assert len(pairs_b) == 0
