"""D4/D5 埋点回归测试（2026-09-27）。

D4：把「入站 → 出站」之间 ReAct **之前**的耗时拆开可测 —— pipeline 逐阶段毫秒、
    pipeline 起点 → ReAct 锚、入站 → pipeline 锚。**纯观测**：不改阶段顺序、
    短路语义与返回值（本文件专门守护最后这点）。
D5：护栏整段改写「破角色分析体」时，把**完整原文 + 改写结果**落盘成可回测样本
    （journal 里那条 warning 只有 120 字摘要且会滚动 ⇒ 事后无法用原文回测判据）。

对应生产事件：`struct>=2&long` 已累计 3 例误杀，但每例都因「不留原文」而无法收敛。
"""

import ast
import json
import tokenize
import types
from pathlib import Path

import pytest

from anima.pipeline import Pipeline, PipelineContext, StageResult
from anima.pipeline.stages.guardrail import (
    _ANALYSIS_ESSAY_FALLBACKS,
    guardrail_stage,
)

ROOT = Path(__file__).resolve().parents[1]

# 与 test_guardrail_analysis.py 同源的破角色分析体（含「政治社会学 / 实证科学」）。
BREAK_ROLE_REPLY = (
    "@[某用户]你是 MengWuy🍥，也就是那个在上海的群友。\n\n"
    "关于你问的 GOP 和那个群体的事，从政治社会学和传播学的角度来看，"
    "这其实是互联网政治模因的一种解构。\n\n"
    "从实证科学的角度来说，没有任何具备统计学信度的数据能支持这种说法。"
    "把一个庞大且复杂的选民群体做单一化的标签绑定是不合逻辑的。\n\n"
    "本喵觉得这就是个无厘头的网络解构，不用太当真 ( ﾟ∀ ﾟ)"
)
CASUAL_REPLY = "哈哈哈你这人也太逗了，接着聊呗～"


def _ctx(*, text="哈哈", reply="", meta=None):
    ctx = PipelineContext(
        event={}, text=text, user_id=888, group_id=222,
        detail_type="group", sender_name="Alice",
        meta=dict(meta or {}),
    )
    ctx.reply = reply
    return ctx


class _Cfg:
    def __init__(self, data_dir):
        self.persistence = types.SimpleNamespace(data_dir=str(data_dir))
        self.context = None
        self.danger = None
        self.contradiction = None
        self.crossgroup = None


class _FakeCore:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)
        self.vector_memory = None
        self.llm = None


# ─────────────────────────────────────────────────────────────────────────────
# D4：pipeline 逐阶段计时
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_records_per_stage_ms():
    async def _a(ctx):
        return StageResult.CONTINUE

    async def _b(ctx):
        return StageResult.CONTINUE

    pipe = Pipeline().use(_a).use(_b)
    ctx = _ctx()
    await pipe.run(ctx)

    ms = ctx.meta.get("_stage_ms")
    assert isinstance(ms, dict), "未记录逐阶段耗时"
    assert set(ms) == {"_a", "_b"}, f"阶段名缺失: {ms}"
    assert all(isinstance(v, float) and v >= 0 for v in ms.values())
    assert isinstance(ctx.meta.get("_pipe_t0"), float), "缺 pipeline 起点时刻"


@pytest.mark.asyncio
async def test_pipeline_total_logged(caplog):
    async def _a(ctx):
        return StageResult.CONTINUE

    with caplog.at_level("INFO", logger="anima.pipeline"):
        await Pipeline().use(_a).run(_ctx())

    hits = [r.getMessage() for r in caplog.records if "[阶段耗时]" in r.getMessage()]
    assert hits, "未打出 [阶段耗时] 汇总日志"
    assert "total=" in hits[0] and "_a=" in hits[0]


@pytest.mark.asyncio
async def test_pipeline_short_circuit_semantics_unchanged():
    """STOP 之后不得执行后续阶段——埋点不得悄悄改变短路语义。"""
    ran = []

    async def _stop(ctx):
        ran.append("stop")
        ctx.stop_reason = "nope"
        return StageResult.STOP

    async def _after(ctx):
        ran.append("after")
        return StageResult.CONTINUE

    ctx = _ctx()
    await Pipeline().use(_stop).use(_after).run(ctx)

    assert ran == ["stop"], f"STOP 后仍执行了后续阶段: {ran}"
    assert set(ctx.meta.get("_stage_ms") or {}) == {"_stop"}


@pytest.mark.asyncio
async def test_pipeline_exception_isolation_kept():
    """非生成阶段抛异常仍需跳过并继续（埋点挂在 finally，不得吃掉异常路径）。"""
    async def _boom(ctx):
        raise ValueError("x")

    async def _ok(ctx):
        return StageResult.CONTINUE

    ctx = _ctx()
    await Pipeline().use(_boom).use(_ok).run(ctx)
    assert set(ctx.meta.get("_stage_ms") or {}) == {"_boom", "_ok"}


def _string_literals(src: str) -> set[str]:
    """收集源码里的字符串字面量（含 Python 3.12+ 的 f-string 文本段）。

    🔴 只查 `tokenize.STRING` 会漏掉 f-string 内的文本（那是 `FSTRING_MIDDLE`），
    门禁曾因此假红一次。
    """
    out: set[str] = set()
    kinds = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)}
    for tok in tokenize.generate_tokens(iter(src.splitlines(keepends=True)).__next__):
        if tok.type in kinds:
            out.add(tok.string)
    return out


def _reads_meta_key(src: str, key: str) -> bool:
    """源码里是否真的读了这个 meta key（AST 级，不是子串匹配）。"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "meta"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == key):
            return True
    return False


def test_generation_has_pre_react_anchor():
    src = (ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(encoding="utf-8")
    lits = _string_literals(src)
    assert any("[阶段耗时]" in s and "pre_react" in s for s in lits), \
        "generation 缺 pre_react 锚（注意 f-string token）"
    assert _reads_meta_key(src, "_pipe_t0"), "pre_react 锚未从 ctx.meta 读 _pipe_t0"


def test_handler_has_entry_anchor():
    src = (ROOT / "anima" / "llm" / "handler.py").read_text(encoding="utf-8")
    lits = _string_literals(src)
    assert any("entry_to_pipeline" in s for s in lits), "handler 缺 entry_to_pipeline 锚"
    assert '_t_entry' in src and 'event["_t_entry"] = time.monotonic()' in src, \
        "handle() 未记录回合起点"


# ─────────────────────────────────────────────────────────────────────────────
# D5：护栏改写样本落盘
# ─────────────────────────────────────────────────────────────────────────────

def _samples(data_dir: Path) -> list[dict]:
    path = data_dir / "guardrail_essay_samples.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.mark.asyncio
async def test_essay_hit_writes_full_sample(tmp_path):
    core = _FakeCore(tmp_path)
    ctx = _ctx(text="怎么说", reply=BREAK_ROLE_REPLY, meta={"core": core})
    await guardrail_stage(ctx)

    rows = _samples(tmp_path)
    assert len(rows) == 1, f"命中未落盘样本: {rows}"
    row = rows[0]
    assert row["rule"], "样本缺规则名"
    # 必须是**完整原文**（回测用），不是 journal 那条 120 字摘要。
    assert len(row["reply"]) > len(BREAK_ROLE_REPLY) * 0.8, "样本疑似被截断成摘要"
    assert "互联网政治模因的一种解构" in row["reply"]
    assert "实证科学" in row["reply"]
    assert row["reply_len"] == len(row["reply"])
    assert row["user_text"] == "怎么说"
    assert row["rewritten_to"] == ctx.reply
    assert ctx.reply in _ANALYSIS_ESSAY_FALLBACKS


@pytest.mark.asyncio
async def test_casual_reply_writes_nothing(tmp_path):
    core = _FakeCore(tmp_path)
    ctx = _ctx(text="哈哈", reply=CASUAL_REPLY, meta={"core": core})
    await guardrail_stage(ctx)
    assert _samples(tmp_path) == [], "未命中却落了样本（会污染回测集）"
    assert ctx.reply == CASUAL_REPLY


@pytest.mark.asyncio
async def test_user_requested_analysis_sample_still_written(tmp_path):
    """`user_wants` 分支同样改写 → 同样要留原文（否则那类误杀无法回测）。"""
    core = _FakeCore(tmp_path)
    long_essay = BREAK_ROLE_REPLY + "还有就是第三点，补充说明一下，综上所述。" * 30
    ctx = _ctx(text="帮我解析一下这个设定", reply=long_essay, meta={"core": core})
    await guardrail_stage(ctx)
    rows = _samples(tmp_path)
    assert len(rows) == 1
    assert rows[0]["user_text"] == "帮我解析一下这个设定"
    assert rows[0]["rule"] == "user_wants_long"


@pytest.mark.asyncio
async def test_sample_is_fail_soft_without_data_dir():
    """取不到 data_dir 时宁可不写，也绝不能抛异常打断回复。"""
    core = types.SimpleNamespace(config=None, vector_memory=None, llm=None)
    ctx = _ctx(text="怎么说", reply=BREAK_ROLE_REPLY, meta={"core": core})
    result = await guardrail_stage(ctx)          # 不抛即通过
    assert result == StageResult.CONTINUE
    assert ctx.reply in _ANALYSIS_ESSAY_FALLBACKS


@pytest.mark.asyncio
async def test_sample_is_fail_soft_when_core_missing():
    ctx = _ctx(text="怎么说", reply=BREAK_ROLE_REPLY, meta={})
    assert ctx.meta.get("core") is None and getattr(ctx, "core", None) is None
    result = await guardrail_stage(ctx)
    assert result == StageResult.CONTINUE


@pytest.mark.asyncio
async def test_samples_append_not_overwrite(tmp_path):
    core = _FakeCore(tmp_path)
    for _ in range(3):
        await guardrail_stage(_ctx(text="怎么说", reply=BREAK_ROLE_REPLY, meta={"core": core}))
    rows = _samples(tmp_path)
    assert len(rows) == 3, f"样本应为追加（覆盖会丢历史）: {len(rows)}"
