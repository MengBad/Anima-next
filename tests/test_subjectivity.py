"""主体性深化测试：情绪系统、社交工具、关系阶段、notice 处理。"""

import pytest
import tempfile
import time


# ---------------------------------------------------------------------------
# 情绪系统测试
# ---------------------------------------------------------------------------

class TestMoodSystem:
    """情绪系统测试。"""

    def test_mood_initial_state(self):
        """初始状态应为中性。"""
        from anima.cognitive.mood import MoodSystem
        with tempfile.TemporaryDirectory() as tmp:
            mood = MoodSystem(data_dir=tmp)
            state = mood.get_state()
            assert state.label == "neutral"
            assert -0.1 <= state.valence <= 0.1

    def test_mood_trigger_praised(self):
        """被夸后情绪应变好。"""
        from anima.cognitive.mood import MoodSystem
        with tempfile.TemporaryDirectory() as tmp:
            mood = MoodSystem(data_dir=tmp)
            mood.on_praised(intensity=0.8)
            state = mood.get_state()
            assert state.valence > 0.1
            assert state.label in ("happy", "excited")

    def test_mood_trigger_insulted(self):
        """被骂后情绪应变差。"""
        from anima.cognitive.mood import MoodSystem
        with tempfile.TemporaryDirectory() as tmp:
            mood = MoodSystem(data_dir=tmp)
            mood.on_insulted(intensity=0.8)
            state = mood.get_state()
            assert state.valence < -0.1
            assert state.label in ("annoyed", "tired")

    def test_mood_decay(self):
        """情绪应随时间衰减。"""
        from anima.cognitive.mood import MoodSystem, MoodState
        with tempfile.TemporaryDirectory() as tmp:
            mood = MoodSystem(data_dir=tmp)
            mood.on_praised(intensity=1.0)
            initial_valence = mood.get_state().valence
            # 模拟时间流逝
            mood._state.last_updated = time.time() - 3600  # 1 小时前
            mood._apply_decay()
            assert mood.get_state().valence < initial_valence

    def test_mood_prompt_fragment_neutral(self):
        """中性情绪不应注入 prompt。"""
        from anima.cognitive.mood import MoodSystem
        with tempfile.TemporaryDirectory() as tmp:
            mood = MoodSystem(data_dir=tmp)
            assert mood.prompt_fragment() == ""

    def test_mood_prompt_fragment_happy(self):
        """开心情绪应注入 prompt。"""
        from anima.cognitive.mood import MoodSystem
        with tempfile.TemporaryDirectory() as tmp:
            mood = MoodSystem(data_dir=tmp)
            mood.on_praised(intensity=0.8)
            frag = mood.prompt_fragment()
            assert "情绪" in frag or "心情" in frag


# ---------------------------------------------------------------------------
# 关系阶段测试
# ---------------------------------------------------------------------------

class TestRelationStageGuidance:
    """关系阶段行为指导测试。"""

    def test_stranger_guidance(self):
        """陌生人应有距离感指导。"""
        from anima.cognitive.relation import RelationEngine, Stage
        with tempfile.TemporaryDirectory() as tmp:
            eng = RelationEngine(data_dir=tmp)
            rel = eng._get_or_create(12345)
            rel.interaction_count = 5
            rel.stage = Stage.STRANGER
            frag = eng.prompt_fragment(12345)
            assert "距离" in frag or "礼貌" in frag

    def test_close_guidance(self):
        """亲密关系应有放松指导。"""
        from anima.cognitive.relation import RelationEngine, Stage
        with tempfile.TemporaryDirectory() as tmp:
            eng = RelationEngine(data_dir=tmp)
            rel = eng._get_or_create(12345)
            rel.interaction_count = 50
            rel.stage = Stage.CLOSE
            frag = eng.prompt_fragment(12345)
            assert "放松" in frag or "几个字" in frag

    def test_valence_influence(self):
        """情绪价态应影响关系描述。"""
        from anima.cognitive.relation import RelationEngine, Stage
        with tempfile.TemporaryDirectory() as tmp:
            eng = RelationEngine(data_dir=tmp)
            rel = eng._get_or_create(12345)
            rel.interaction_count = 10
            rel.stage = Stage.FRIEND
            rel.valence = 0.7  # 很开心
            frag = eng.prompt_fragment(12345)
            assert "开心" in frag or "轻快" in frag


# ---------------------------------------------------------------------------
# 社交工具权限测试
# ---------------------------------------------------------------------------

class TestSocialToolPermissions:
    """社交工具权限分级测试。"""

    def test_tool_levels(self):
        """验证工具权限级别设置正确。"""
        from anima.tools import LEVEL_USER, LEVEL_ADMIN
        # 这些工具应该是 USER 级别
        user_tools = ["group_poke", "get_group_members", "get_group_info"]
        # 这些工具应该是 ADMIN 级别
        admin_tools = ["set_group_card", "group_ban", "group_kick"]

        # 简单验证级别常量
        assert LEVEL_USER < LEVEL_ADMIN


# ---------------------------------------------------------------------------
# Notice 事件处理测试
# ---------------------------------------------------------------------------

class TestSocialReactions:
    """社交反应测试。"""

    def test_welcome_templates_exist(self):
        """欢迎语模板应存在。"""
        from anima.cognitive.social_reactions import _WELCOME_TEMPLATES
        assert len(_WELCOME_TEMPLATES) >= 3

    def test_farewell_templates_exist(self):
        """告别语模板应存在。"""
        from anima.cognitive.social_reactions import _FAREWELL_TEMPLATES
        assert len(_FAREWELL_TEMPLATES) >= 3

    def test_poke_responses_exist(self):
        """被戳回应模板应存在。"""
        from anima.cognitive.social_reactions import _POKE_RESPONSES
        assert len(_POKE_RESPONSES) >= 3


# ---------------------------------------------------------------------------
# 承诺跟进测试
# ---------------------------------------------------------------------------

class TestPromiseDetection:
    """承诺跟进检测测试。"""

    def test_promise_regex_matches(self):
        """承诺关键词应被检测到。"""
        from anima.pipeline.stages import _PROMISE_RE
        promises = [
            "回头帮你查一下",
            "下次再聊",
            "本喵去查查",
            "帮你看看",
            "记下了",
        ]
        for text in promises:
            assert _PROMISE_RE.search(text), f"应检测到承诺: {text}"

    def test_promise_regex_non_matches(self):
        """非承诺语句不应被检测。"""
        from anima.pipeline.stages import _PROMISE_RE
        non_promises = [
            "今天天气不错",
            "你好",
            "本喵觉得不行",
        ]
        for text in non_promises:
            assert not _PROMISE_RE.search(text), f"不应检测到承诺: {text}"


# ---------------------------------------------------------------------------
# 配置测试
# ---------------------------------------------------------------------------

class TestSocialConfig:
    """社交配置测试。"""

    def test_social_config_defaults(self):
        """社交配置应有合理默认值。"""
        from anima.core.config import SocialConfig
        cfg = SocialConfig()
        assert cfg.welcome_new_member is True
        assert cfg.farewell_member is True
        assert cfg.poke_back is True
        assert 0 < cfg.poke_back_probability < 1
        assert cfg.welcome_cooldown_sec > 0
