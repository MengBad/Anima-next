"""认知内核（世界观、遗忘、伤痕）测试。"""

import asyncio
import time

import pytest

from anima.cognitive.world_model import WorldModel
from anima.cognitive.forgetting import ForgettingEngine
from anima.cognitive.scar import ScarSystem


class TestWorldModel:

    def test_add_and_retrieve(self, tmp_path):
        wm = WorldModel(data_dir=str(tmp_path), max_entries=5)
        wm.add("群聊是轻松的", category="belief", weight=0.6)
        entries = wm.get_entries()
        assert len(entries) == 1
        assert entries[0].text == "群聊是轻松的"

    def test_similar_merge(self, tmp_path):
        wm = WorldModel(data_dir=str(tmp_path), max_entries=5)
        wm.add("群聊氛围很轻松", category="belief", weight=0.5)
        wm.add("群聊很轻松", category="belief", weight=0.5)
        assert len(wm._entries) == 1
        assert wm._entries[0].weight > 0.55

    def test_prune(self, tmp_path):
        wm = WorldModel(data_dir=str(tmp_path), max_entries=2)
        wm.add("A", weight=0.1)
        wm.add("B", weight=0.9)
        wm.add("C", weight=0.8)
        assert len(wm._entries) == 2
        assert "A" not in [e.text for e in wm._entries]

    def test_prompt_fragment(self, tmp_path):
        wm = WorldModel(data_dir=str(tmp_path), max_entries=5)
        wm.add("我喜欢猫", category="preference", weight=0.8)
        frag = wm.prompt_fragment()
        assert "喜欢猫" in frag
        assert "偏好" in frag

    def test_normal_topics_are_not_word_blocked(self, tmp_path):
        """正常话题不因旧关键词黑名单而被拒答或拒绝写入。"""
        wm = WorldModel(data_dir=str(tmp_path), max_entries=5)
        entry = wm.add("讨论男娘、药娘和 MTF 时应就具体说法交流", source="rumination")
        assert entry is not None
        assert len(wm._entries) == 1
        assert WorldModel.is_blocked_topic("评价一下男娘") is False

    def test_analysis_keyword_filtered(self, tmp_path):
        """论文体/社交策略分析腔 belief 不写世界观（防 bot 被腌成分析师）。

        反刍常把社交互动写成「拟人化表达边界/以柔克刚提供情绪价值」式自我分析，
        与 persona「不写分析论文」冲突——命中 ANALYSIS_KEYWORDS 即丢弃。
        见 2026-07-12 回复诊断报告根因 1。
        """
        wm = WorldModel(data_dir=str(tmp_path), max_entries=5)
        # 论文体样本（取自线上 18 条污染 belief）
        assert wm.add("我用拟人化方式提供情绪价值，以柔克刚化解冲突") is None
        assert wm.add("面对用户烦躁，我选择顺毛以提供情绪价值") is None
        assert wm.add("在趣味与专业边界之间做好平衡") is None
        assert wm.add("用猫系人设去包容，做自我解剖") is None
        assert len(wm._entries) == 0, "论文体/分析腔 belief 不应进入世界观"
        # 合法事实/偏好不受影响
        e = wm.add("喜欢下雨天窝着不动", category="preference", weight=0.7)
        assert e is not None
        assert len(wm._entries) == 1


class TestScar:

    def test_add_and_penalty(self, tmp_path):
        ss = ScarSystem(data_dir=str(tmp_path))
        ss.add(12345, "对方羞辱了我", severity=0.7)
        scars = ss.get_for_user(12345)
        assert len(scars) == 1
        assert ss.trust_penalty(12345) >= 0.5

    def test_heal(self, tmp_path):
        ss = ScarSystem(data_dir=str(tmp_path))
        ss.add(12345, "对方羞辱了我", severity=0.5)
        ss.heal(12345, amount=0.2)
        assert ss.get_for_user(12345)[0].severity < 0.4

    def test_prompt_fragment(self, tmp_path):
        ss = ScarSystem(data_dir=str(tmp_path))
        ss.add(12345, "对方羞辱了我", severity=0.8)
        frag = ss.prompt_fragment(12345)
        assert "羞辱" in frag
        assert "敏感点" in frag

    def test_merge_same_event(self, tmp_path):
        ss = ScarSystem(data_dir=str(tmp_path))
        ss.add(12345, "对方羞辱了我", severity=0.5)
        ss.add(12345, "对方羞辱了我", severity=0.5)
        assert len(ss._scars) == 1
        assert ss._scars[0].severity > 0.6


class TestForgetting:

    def test_should_forget_old_low_salience(self, tmp_path):
        fe = ForgettingEngine(data_dir=str(tmp_path))
        item = {
            "ts": time.time() - 86400 * 30,  # 30 天前
            "salience": 0.1,
            "access_count": 0,
            "emotion": 0.0,
        }
        assert fe.should_forget(item) is True

    def test_should_retain_recent_high_emotion(self, tmp_path):
        fe = ForgettingEngine(data_dir=str(tmp_path))
        item = {
            "ts": time.time(),
            "salience": 0.8,
            "access_count": 5,
            "emotion": 0.9,
        }
        assert fe.should_forget(item) is False

    def test_forget_chat_memory_nested(self, tmp_path):
        from anima.memory.chat_memory import ChatMemory
        from collections import OrderedDict

        fe = ForgettingEngine(data_dir=str(tmp_path))
        cm = ChatMemory(persist_path="")
        # 直接构造一个旧条目
        old_ts = time.time() - 86400 * 30
        cm._store["group_1"] = OrderedDict({
            "round_1": [
                {"role": "user", "content": "hi", "ts": old_ts, "salience": 0.1, "access_count": 0, "emotion": 0.0},
            ]
        })
        removed = fe.forget_chat_memory(cm)
        assert removed == 1
        assert "group_1" not in cm._store

    def test_forget_run_interval(self, tmp_path):
        fe = ForgettingEngine(data_dir=str(tmp_path))
        fe._last_run = time.time()
        result = asyncio.run(fe.run())
        assert result == {"chat": 0, "vector": 0}
