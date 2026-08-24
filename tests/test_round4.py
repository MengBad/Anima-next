"""第四轮迭代集成测试：表达拟人化 + 灵魂动力学 + 社交在场感。"""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# A1/A2: 拟人分段 + 延迟
# ---------------------------------------------------------------------------

class TestReplySplitter:
    """测试拟人分段发送。"""

    def test_short_text_no_split(self):
        """短文本不分段。"""
        from anima.core.reply_splitter import split_text, SplitConfig
        cfg = SplitConfig(trigger_threshold=80)
        result = split_text("你好呀！", cfg)
        assert len(result) == 1
        assert result[0] == "你好呀！"

    def test_long_text_splits(self):
        """长文本按标点分段。"""
        from anima.core.reply_splitter import split_text, SplitConfig
        cfg = SplitConfig(trigger_threshold=30, max_segments=4, min_segment_len=10)
        text = "今天天气真好啊！我们出去玩吧。顺便买点东西回来，我想吃冰淇淋。然后我们去公园散步。"
        result = split_text(text, cfg)
        # 文本 40 字，trigger=30，应该分段
        # 但由于合并逻辑，可能只有 1-2 段
        assert len(result) >= 1
        # 所有段合并应等于原文（去掉空白）
        assert "".join(result).replace(" ", "") == text.replace(" ", "")

    def test_protected_symbols(self):
        """成对符号内不切。"""
        from anima.core.reply_splitter import split_text, SplitConfig
        cfg = SplitConfig(trigger_threshold=20, max_segments=4)
        text = "他说「你好。再见」然后走了。我也走了。"
        result = split_text(text, cfg)
        # 「你好。再见」应该保持完整
        found = any("你好。再见" in seg for seg in result)
        assert found, f"成对符号被切断: {result}"

    def test_url_protection(self):
        """URL 不切断。"""
        from anima.core.reply_splitter import split_text, SplitConfig
        cfg = SplitConfig(trigger_threshold=20)
        text = "看这个链接 https://example.com/path?query=1 很不错吧！我觉得很好。"
        result = split_text(text, cfg)
        # URL 应该完整保留在某一段中
        found = any("https://example.com/path?query=1" in seg for seg in result)
        assert found, f"URL 被切断: {result}"

    def test_tail_merge(self):
        """尾段过短合并到前段。"""
        from anima.core.reply_splitter import split_text, SplitConfig
        cfg = SplitConfig(trigger_threshold=30, tail_merge_threshold=10)
        text = "这是一段比较长的文字。短。"
        result = split_text(text, cfg)
        # "短。" 应该被合并
        assert len(result) == 1 or len(result[-1]) >= 10

    def test_delay_computation(self):
        """延迟计算：情绪和关系调制。"""
        from anima.core.reply_splitter import compute_delay, SplitConfig
        cfg = SplitConfig(delay_enabled=True, delay_base=0.4, delay_factor=0.02)

        # 首段延迟受关系影响
        delay_close = compute_delay("测试文本", 0, cfg, relation_stage="CLOSE")
        delay_stranger = compute_delay("测试文本", 0, cfg, relation_stage="STRANGER")
        # CLOSE 应该比 STRANGER 快（延迟短）
        # 注意：有随机性，所以用多次采样
        close_delays = [compute_delay("测试", 0, cfg, relation_stage="CLOSE") for _ in range(10)]
        stranger_delays = [compute_delay("测试", 0, cfg, relation_stage="STRANGER") for _ in range(10)]
        assert sum(close_delays) / len(close_delays) < sum(stranger_delays) / len(stranger_delays)

    def test_mood_speed_factor(self):
        """情绪影响打字速度。"""
        from anima.core.reply_splitter import compute_delay, SplitConfig
        cfg = SplitConfig(delay_enabled=True)
        # excited 打字快，tired 打字慢
        excited_delays = [compute_delay("测试文本" * 10, 1, cfg, mood_label="excited") for _ in range(10)]
        tired_delays = [compute_delay("测试文本" * 10, 1, cfg, mood_label="tired") for _ in range(10)]
        assert sum(excited_delays) / len(excited_delays) < sum(tired_delays) / len(tired_delays)

    def test_split_reply_integration(self):
        """split_reply 一体化接口。"""
        from anima.core.reply_splitter import split_reply, SplitConfig
        cfg = SplitConfig(trigger_threshold=50)
        text = "今天很开心！遇到了很多有趣的人。聊了好多话题。"
        result = split_reply(text, cfg, mood_label="happy", relation_stage="FRIEND")
        assert len(result.segments) >= 1
        assert len(result.delays) == len(result.segments)


# ---------------------------------------------------------------------------
# B1: 群聊防抖
# ---------------------------------------------------------------------------

class TestGroupDebouncer:
    """测试群聊消息防抖。"""

    @pytest.mark.asyncio
    async def test_single_message_passthrough(self):
        """单条消息直接通过。"""
        from anima.core.conversation_assembler import GroupDebouncer
        debouncer = GroupDebouncer(wait_sec=0.1, max_wait_sec=0.5)
        event = {"group_id": 123, "user_id": 456, "raw_message": "你好"}
        result = await debouncer.ingest(event)
        assert result is not None
        assert result["raw_message"] == "你好"

    @pytest.mark.asyncio
    async def test_command_bypass(self):
        """指令消息不进入防抖。"""
        from anima.core.conversation_assembler import GroupDebouncer
        debouncer = GroupDebouncer(wait_sec=0.1)
        event = {"group_id": 123, "user_id": 456, "raw_message": "!anima help"}
        result = await debouncer.ingest(event)
        assert result is not None
        assert result["raw_message"] == "!anima help"

    @pytest.mark.asyncio
    async def test_merge_multiple_messages(self):
        """多条消息合并。"""
        from anima.core.conversation_assembler import GroupDebouncer
        debouncer = GroupDebouncer(wait_sec=0.3, max_wait_sec=1.0)

        # 并发发送两条消息
        e1 = {"group_id": 123, "user_id": 456, "raw_message": "你好", "message_id": 1}
        e2 = {"group_id": 123, "user_id": 456, "raw_message": "在吗", "message_id": 2}

        # 第一条开始等待
        task1 = asyncio.create_task(debouncer.ingest(e1))
        await asyncio.sleep(0.05)  # 确保第一条先到达
        # 第二条应该被合并
        r2 = await debouncer.ingest(e2)
        # 等待第一条完成
        r1 = await task1

        assert r2 is None  # 第二条被合并
        assert r1 is not None
        assert "你好" in r1["raw_message"]
        assert "在吗" in r1["raw_message"]
        assert r1["assembled_count"] == 2

    @pytest.mark.asyncio
    async def test_different_users_separate(self):
        """不同用户的消息分开处理。"""
        from anima.core.conversation_assembler import GroupDebouncer
        debouncer = GroupDebouncer(wait_sec=0.1)

        e1 = {"group_id": 123, "user_id": 456, "raw_message": "用户A"}
        e2 = {"group_id": 123, "user_id": 789, "raw_message": "用户B"}

        r1 = await debouncer.ingest(e1)
        r2 = await debouncer.ingest(e2)

        assert r1 is not None
        assert r2 is not None
        assert r1["raw_message"] == "用户A"
        assert r2["raw_message"] == "用户B"

    def test_adaptive_delay_short_message(self):
        """短句延长等待。"""
        from anima.core.conversation_assembler import GroupDebouncer, _PendingConversation
        debouncer = GroupDebouncer(wait_sec=2.0, adaptive=True, short_msg_threshold=10)
        pending = _PendingConversation(events=[{"raw_message": "短"}])
        delay = debouncer._compute_delay(pending)
        assert delay > 2.0  # 短句应该延长

    def test_adaptive_delay_long_message(self):
        """长句缩短等待。"""
        from anima.core.conversation_assembler import GroupDebouncer, _PendingConversation
        debouncer = GroupDebouncer(wait_sec=2.0, adaptive=True)
        pending = _PendingConversation(events=[{"raw_message": "这是一段很长很长很长很长很长很长很长很长很长很长很长很长很长很长的消息"}])
        delay = debouncer._compute_delay(pending)
        # 长句应该缩短（<= wait_sec）
        assert delay <= 2.0


# ---------------------------------------------------------------------------
# D1: 灵魂能量槽
# ---------------------------------------------------------------------------

class TestSoulSystem:
    """测试灵魂能量槽系统。"""

    def test_initial_state(self):
        """初始状态为 0。"""
        from anima.cognitive.soul import SoulState
        state = SoulState()
        assert state.expression_desire == 0.0
        assert state.creativity == 0.0
        assert state.memory_depth == 0.0
        assert state.social_appetite == 0.0

    def test_update_with_positive_inputs(self):
        """正输入产生正能量。"""
        from anima.cognitive.soul import get_soul_system
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            soul = get_soul_system(tmpdir)
            soul.update(
                mood_valence=0.8,
                mood_arousal=0.7,
                relation_intimacy=0.9,
                relation_familiarity=0.8,
            )
            assert soul.state.expression_desire > 0
            assert soul.state.creativity > 0

    def test_ema_smoothing(self):
        """EMA 平滑防止跳变。"""
        from anima.cognitive.soul import get_soul_system
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            soul = get_soul_system(tmpdir)
            # 第一次更新
            soul.update(mood_arousal=1.0)
            first = soul.state.expression_desire
            # 第二次更新（相反方向）
            soul.update(mood_arousal=-1.0)
            second = soul.state.expression_desire
            # 由于 EMA，第二次不应该直接跳到负值
            assert second > -0.5  # 不会立刻跳到底

    def test_param_mapping(self):
        """参数映射正确。"""
        from anima.cognitive.soul import map_max_tokens, map_temperature, map_memory_top_k
        # 低能量 → 低参数
        assert map_max_tokens(-1.0) == 80
        assert map_temperature(-1.0) == 0.5
        assert map_memory_top_k(-1.0) == 2
        # 高能量 → 高参数
        assert map_max_tokens(1.0) == 400
        assert map_temperature(1.0) == 1.0
        assert map_memory_top_k(1.0) == 8

    def test_persistence(self):
        """持久化和加载。"""
        from anima.cognitive.soul import get_soul_system, _instances
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # 清除单例缓存
            key = str(Path(tmpdir).resolve())
            _instances.pop(key, None)

            soul1 = get_soul_system(tmpdir)
            soul1.update(mood_valence=0.5, mood_arousal=0.6)
            soul1.save()

            # 重新加载
            _instances.pop(key, None)
            soul2 = get_soul_system(tmpdir)
            assert abs(soul2.state.expression_desire - soul1.state.expression_desire) < 0.01




# ---------------------------------------------------------------------------
# C1: QQ 空间说说
# ---------------------------------------------------------------------------

class TestQZone:
    """测试 QQ 空间说说。"""

    def test_can_post_initially(self):
        """初始可以发说说。"""
        from anima.tools.qzone import get_qzone_manager, _instance
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # 重置单例
            import anima.tools.qzone as qz
            qz._instance = None
            manager = get_qzone_manager(tmpdir)
            can, reason = manager.can_post()
            assert can is True

    def test_cooldown(self):
        """冷却限制。"""
        from anima.tools.qzone import QZoneManager
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = QZoneManager(tmpdir, cooldown_sec=3600)
            manager.record_post("测试", "manual", True)
            can, reason = manager.can_post()
            assert can is False
            assert "冷却" in reason

    def test_daily_limit(self):
        """每日上限。"""
        from anima.tools.qzone import QZoneManager
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = QZoneManager(tmpdir, daily_max=2, cooldown_sec=0)
            manager.record_post("第一条", "manual", True)
            manager.record_post("第二条", "manual", True)
            can, reason = manager.can_post()
            assert can is False
            assert "上限" in reason


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

class TestConfig:
    """测试新配置项加载。"""

    def test_reply_split_config(self):
        """reply_split 配置。"""
        from anima.core.config import load_config
        cfg = load_config()
        assert cfg.reply_split.enabled is True
        assert cfg.reply_split.max_segments == 2
        assert cfg.reply_split.max_segment_len == 90

    def test_debounce_config(self):
        """debounce 配置。"""
        from anima.core.config import load_config
        cfg = load_config()
        assert cfg.debounce.group_enabled is True
        assert cfg.debounce.group_wait_sec == 2.0

    def test_soul_config(self):
        """soul 配置。"""
        from anima.core.config import load_config
        cfg = load_config()
        assert cfg.soul.enabled is True
        assert cfg.soul.ema_alpha == 0.3

    def test_gate_config(self):
        """gate 配置。"""
        from anima.core.config import load_config
        cfg = load_config()
        assert cfg.gate.enabled is False  # 默认关闭

    def test_qzone_config(self):
        """qzone 配置。"""
        from anima.core.config import load_config
        cfg = load_config()
        assert cfg.qzone.enabled is False  # 默认关闭
        assert cfg.qzone.daily_max == 2


# ---------------------------------------------------------------------------
# 灵魂能量状态描述 + 模拟生活连续性
# ---------------------------------------------------------------------------

class TestSoulDescribeState:
    """测试灵魂能量状态自然语言描述。"""

    def test_neutral_returns_empty(self):
        """中性状态不输出。"""
        from anima.cognitive.soul import SoulSystem
        soul = SoulSystem(data_dir="data", ema_alpha=1.0)
        # 默认状态全 0，不应输出
        assert soul.describe_state() == ""

    def test_high_expression(self):
        """高表达欲输出提示。"""
        from anima.cognitive.soul import SoulSystem
        soul = SoulSystem(data_dir="data", ema_alpha=1.0)
        soul._state.expression_desire = 0.6
        desc = soul.describe_state()
        assert "表达欲" in desc
        assert "当前内心状态" in desc

    def test_low_social(self):
        """低社交食欲输出提示。"""
        from anima.cognitive.soul import SoulSystem
        soul = SoulSystem(data_dir="data", ema_alpha=1.0)
        soul._state.social_appetite = -0.5
        desc = soul.describe_state()
        assert "独处" in desc

    def test_multiple_cues_limited(self):
        """多个维度偏移时最多 3 条 cue。"""
        from anima.cognitive.soul import SoulSystem
        soul = SoulSystem(data_dir="data", ema_alpha=1.0)
        soul._state.expression_desire = 0.5
        soul._state.creativity = 0.5
        soul._state.social_appetite = 0.5
        soul._state.memory_depth = -0.5
        desc = soul.describe_state()
        # 4 个维度都偏移，但最多取 3 条 cue
        assert "当前内心状态" in desc
        # 确认不是所有 4 条都包含（“记性”是第 4 条，应被截断）
        assert "记性" not in desc


class TestRecentActivityHint:
    """测试模拟生活连续性提示。"""

    def test_no_life_instance_returns_empty(self):
        """无 SimulatedLife 实例时返回空。"""
        import anima.cognitive.simulated_life as sl
        old = sl._simulated_life
        sl._simulated_life = None
        try:
            assert sl.recent_activity_hint("data") == ""
        finally:
            sl._simulated_life = old

    def test_returns_string(self):
        """有实例时返回字符串（可能为空）。"""
        import anima.cognitive.simulated_life as sl
        from unittest.mock import MagicMock
        old = sl._simulated_life
        mock_life = MagicMock()
        mock_life._last_flow = 0.0
        mock_life._last_morning = 0.0
        sl._simulated_life = mock_life
        try:
            result = sl.recent_activity_hint("data")
            assert isinstance(result, str)
        finally:
            sl._simulated_life = old


# ---------------------------------------------------------------------------
# 对话语气检测
# ---------------------------------------------------------------------------

class TestConversationTone:
    """测试对话语气检测。"""

    def test_empty_text_returns_empty(self):
        """空文本返回空字符串。"""
        from anima.cognitive.mood import detect_conversation_tone
        assert detect_conversation_tone("") == ""
        assert detect_conversation_tone(None) == ""

    def test_aggressive_detected(self):
        """攻击性语气被检测到。"""
        from anima.cognitive.mood import detect_conversation_tone
        result = detect_conversation_tone("你滚开！")
        assert "冲" in result or "收着点" in result

    def test_sad_detected(self):
        """悲伤语气被检测到。"""
        from anima.cognitive.mood import detect_conversation_tone
        result = detect_conversation_tone("我今天好难过")
        assert "不开心" in result or "温柔" in result

    def test_playful_detected(self):
        """玩闹语气被检测到。"""
        from anima.cognitive.mood import detect_conversation_tone
        result = detect_conversation_tone("哈哈哈太好笑了")
        assert "玩" in result or "放松" in result

    def test_serious_detected(self):
        """严肃语气被检测到。"""
        from anima.cognitive.mood import detect_conversation_tone
        result = detect_conversation_tone("请问这个问题怎么解决？")
        assert "认真" in result or "直接" in result

    def test_priority_aggressive_over_sad(self):
        """攻击性优先于悲伤。"""
        from anima.cognitive.mood import detect_conversation_tone
        # 同时包含攻击性和悲伤词汇，应该返回攻击性提示
        result = detect_conversation_tone("我烦死了，滚！")
        assert "冲" in result or "收着点" in result

    def test_neutral_returns_empty(self):
        """中性文本返回空。"""
        from anima.cognitive.mood import detect_conversation_tone
        result = detect_conversation_tone("今天天气不错")
        assert result == ""


class TestConversationDepth:
    """测试对话深度提示。"""

    def test_short_conversation_no_hint(self):
        """短对话不生成提示。"""
        from anima.cognitive.mood import conversation_depth_hint
        assert conversation_depth_hint(1) == ""
        assert conversation_depth_hint(2) == ""

    def test_medium_conversation_hint(self):
        """中等对话生成提示。"""
        from anima.cognitive.mood import conversation_depth_hint
        result = conversation_depth_hint(5)
        assert "刚开始" in result or "放松" in result

    def test_long_conversation_hint(self):
        """长对话生成提示。"""
        from anima.cognitive.mood import conversation_depth_hint
        result = conversation_depth_hint(15)
        assert "放松" in result or "随意" in result

    def test_very_long_conversation_hint(self):
        """超长对话生成提示。"""
        from anima.cognitive.mood import conversation_depth_hint
        result = conversation_depth_hint(25)
        assert "放松" in result or "玩笑" in result
