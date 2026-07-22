"""笔记压缩 compact_notes 测试（隔离 tmp_path）。

覆盖：无需压缩 / 前段过短直接截断 / LLM 摘要路径 / LLM 异常退化 / LLM 返回空退化。
"""
import asyncio
import pytest

from anima.cognitive.learning import SelfLearning


class FakeLLM:
    """可注入返回值的假 LLM：reply 为 Exception 实例时抛出，否则原样返回。"""
    def __init__(self, reply="历史摘要内容"):
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, temperature=0.7, max_tokens=120):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _make_notes(n: int, prefix: str = "line") -> str:
    return "\n".join(f"[ts] {prefix} {i:03d}" for i in range(n))


def _read(data_dir) -> str:
    p = data_dir / "self_notes.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _compact(sl, llm, max_keep=10):
    return asyncio.run(sl.compact_notes(llm, max_keep=max_keep))


def test_noop_when_under_threshold(tmp_path):
    sl = SelfLearning(str(tmp_path))
    sl._persist.write_text("self_notes.md", _make_notes(5))
    llm = FakeLLM()
    assert _compact(sl, llm) is False
    assert llm.calls == 0
    assert _read(tmp_path) == _make_notes(5)


def test_truncate_when_front_short(tmp_path):
    sl = SelfLearning(str(tmp_path))
    # 前 3 行极短（总前段 < 50 字）→ 走截断路径，不调 LLM
    front_lines = ["short"] * 3
    tail_lines = [f"[ts] note {i}" for i in range(10)]
    sl._persist.write_text("self_notes.md", "\n".join(front_lines + tail_lines))
    llm = FakeLLM()
    assert _compact(sl, llm) is True
    assert llm.calls == 0  # 前段过短不调 LLM
    out = _read(tmp_path)
    assert out == "\n".join(tail_lines)  # 只保留后 10 行，前段被丢弃


def test_llm_summary_path(tmp_path):
    sl = SelfLearning(str(tmp_path))
    sl._persist.write_text("self_notes.md", _make_notes(50, prefix="note"))
    llm = FakeLLM(reply="这是历史反思摘要ABC")
    assert _compact(sl, llm) is True
    assert llm.calls == 1
    out = _read(tmp_path)
    assert out.startswith("[历史反思摘要] 这是历史反思摘要ABC")
    # 保留段为最后 10 行
    kept = _make_notes(50, prefix="note").split("\n")[-10:]
    assert out.endswith("\n".join(kept))


def test_fallback_on_llm_error(tmp_path):
    sl = SelfLearning(str(tmp_path))
    sl._persist.write_text("self_notes.md", _make_notes(50, prefix="note"))
    llm = FakeLLM(reply=RuntimeError("boom"))
    # 即使 LLM 抛错也应退化截断并返回 True（保住「不膨胀」硬目标）
    assert _compact(sl, llm) is True
    out = _read(tmp_path)
    kept = _make_notes(50, prefix="note").split("\n")[-10:]
    assert out == "\n".join(kept)


def test_fallback_on_empty_summary(tmp_path):
    sl = SelfLearning(str(tmp_path))
    sl._persist.write_text("self_notes.md", _make_notes(50, prefix="note"))
    llm = FakeLLM(reply=None)  # 空返回 → 视为失败 → 退化截断
    assert _compact(sl, llm) is True
    out = _read(tmp_path)
    kept = _make_notes(50, prefix="note").split("\n")[-10:]
    assert out == "\n".join(kept)


def test_empty_file_is_noop(tmp_path):
    sl = SelfLearning(str(tmp_path))
    llm = FakeLLM()
    assert _compact(sl, llm) is False
    assert llm.calls == 0


def test_repeated_compaction_merges_summaries(tmp_path):
    """反复压缩会把上一轮的 [历史反思摘要] 一并再次摘要，不会堆叠多个摘要头。"""
    sl = SelfLearning(str(tmp_path))
    sl._persist.write_text("self_notes.md", _make_notes(50, prefix="note"))
    llm = FakeLLM(reply="摘要一")
    assert _compact(sl, llm) is True
    first = _read(tmp_path)
    assert first.count("[历史反思摘要]") == 1
    # 再追加 50 行后再次压缩：前段含旧的 [历史反思摘要] 头
    sl._persist.write_text("self_notes.md", first + "\n" + _make_notes(50, prefix="more"))
    llm2 = FakeLLM(reply="摘要二（合并）")
    assert _compact(sl, llm2) is True
    second = _read(tmp_path)
    # 仍然只有一个摘要头（旧的被合并进新摘要，而非保留两个）
    assert second.count("[历史反思摘要]") == 1
    assert second.startswith("[历史反思摘要] 摘要二（合并）")
