"""A2 梦境生成：mock 当日聊天记录，验证生成 JSON 含 dream/reflection/mood_tag。"""
from __future__ import annotations

from pathlib import Path

from anima.cognitive import dream_engine
from anima.cognitive.dream_engine import DreamEngine, mood_tag_to_emotion


class _FakeLLM:
    """返回固定梦境 JSON，模拟一次 LLM 调用。"""
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    async def chat(self, messages, temperature=0.95, max_tokens=200, module=None, operation=None):
        self.calls += 1
        self.last_messages = messages
        return self.payload


def _fake_chat_memory(path: Path):
    data = {
        "group_727631526_123": {
            "round_1": [
                {"role": "user", "name": "亦萱", "content": "今天好开心大家一起去吃折耳根",
                 "emotion": "happy", "ts": 1725000000},
                {"role": "user", "name": "丰川刻子", "content": "头衔改成了废物喵喵",
                 "emotion": "angry", "ts": 1725000060},
            ]
        }
    }
    path.write_text(__import__("json").dumps(data), encoding="utf-8")


async def test_generate_dream_parses_json(tmp_path):
    _fake_chat_memory(tmp_path / "chat_memory.json")
    (tmp_path / "soul.json").write_text('{"emotion": "平静"}', encoding="utf-8")
    (tmp_path / "bot_relations.json").write_text('{"relations": {}}', encoding="utf-8")

    payload = (
        '```json\n'
        '{"dream": "梦到亦萱变成了一只巨大的折耳根，追着本喵跑", '
        '"reflection": "原来本喵对食物还是有阴影的", "mood_tag": "weird"}\n'
        '```'
    )
    eng = DreamEngine(str(tmp_path), core=None)
    dream = await eng.generate_dream(llm=_FakeLLM(payload))
    assert "dream" in dream and dream["dream"]
    assert "reflection" in dream and dream["reflection"]
    assert dream["mood_tag"] == "weird"
    assert "timestamp" in dream
    assert isinstance(dream.get("source_keywords"), list)
    assert "happy" in dream["source_keywords"]  # 当日情绪关键词被提取


async def test_generate_dream_prompt_includes_inputs(tmp_path):
    _fake_chat_memory(tmp_path / "chat_memory.json")
    (tmp_path / "soul.json").write_text('{"emotion": "兴奋"}', encoding="utf-8")
    (tmp_path / "bot_relations.json").write_text(
        '{"relations": {"838916454": {"bot_name": "丰川刻子", "rivalry": 0.4}}}',
        encoding="utf-8",
    )
    fake = _FakeLLM('{"dream":"d","reflection":"r","mood_tag":"happy"}')
    eng = DreamEngine(str(tmp_path), core=None)
    await eng.generate_dream(llm=fake)
    prompt = fake.last_messages[0]["content"]
    assert "兴奋" in prompt            # 今日情绪
    assert "丰川刻子" in prompt        # bot 互动
    assert "折耳根" in prompt or "happy" in prompt  # 当日关键词


def test_mood_tag_to_emotion_mapping():
    assert mood_tag_to_emotion("weird") == "happy"
    assert mood_tag_to_emotion("anxious") == "sad"
    assert mood_tag_to_emotion("happy") == "excited"
    assert mood_tag_to_emotion("bogus") == "calm"  # 无效回退


async def test_maybe_generate_once_per_night(tmp_path):
    _fake_chat_memory(tmp_path / "chat_memory.json")
    (tmp_path / "soul.json").write_text('{"emotion": "平静"}', encoding="utf-8")
    fake = _FakeLLM('{"dream":"梦到在草地上晒太阳","reflection":"暖暖的","mood_tag":"happy"}')
    eng = DreamEngine(str(tmp_path), core=None)
    # 第一次：今晚还没梦 → 生成并保存
    d1 = await eng.maybe_generate_during_sleep(now=1725007200, llm=fake)  # 2026-07-30 约 03:00 北京
    assert d1 is not None
    assert eng.dreams_path.exists()
    # 第二次：同一「晚」→ 跳过（返回 None）
    d2 = await eng.maybe_generate_during_sleep(now=1725007500, llm=fake)
    assert d2 is None
    # 文件只有一条
    lines = [l for l in eng.dreams_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
