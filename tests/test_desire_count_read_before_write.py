"""X4 回归：`desire_count` 读早于写（Sylann 侧恒 0）。

2026-10-14 白天复核后用「读早于写」模式全仓扫描（AST）发现的**第二例**。
第一例是 generation.py 的 `helped`（X1，已修）；本例在 cognition.py：

    cognition_stage():
      L750   await _observe_sylann(sylann, ctx)
               └─ _sylann_values(ctx) → ctx.meta.get("desire_count", 0)   ← 读
      L1138  ctx.meta["desire_count"] = len(desire.get_active())          ← 写

读早于写 388 行 ⇒ `_sylann_values()` 恒取默认 0.0 ⇒ Sylann 人格适配层的
`desire_count` 特征**永远是 0**，对该层零区分度。与 X1 同一静默失效族：
不报错、测试只验结构不验值 ⇒ 上一轮功能扫描完全漏掉。

放大条件：`PipelineContext` 每回合新建（唯一创建点 anima/llm/handler.py:1175），
`ctx.meta` 不跨回合复用 ⇒ 读取侧永远拿不到本回合写入值。

修复：把 `ctx.meta["desire_count"] = ...` 前移到 `_observe_sylann` 调用之前。
`desire` 在 cognition_stage L381 已定义，且 `desire.add` 只发生在 L444
（早于前移点）⇒ 前移后数值与原写入点**逐字节相同**，generation 侧读到的值不变。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
COG = ROOT / "anima" / "pipeline" / "stages" / "cognition.py"


def _lines():
    return COG.read_text(encoding="utf-8").splitlines()


def _line_of(needle, start=0):
    for i in range(start, len(_lines())):
        if needle in _lines()[i]:
            return i
    return -1


def test_write_happens_before_observe_call():
    """核心：desire_count 的写入必须早于 _observe_sylann 调用。"""
    lines = _lines()
    obs = next(i for i, ln in enumerate(lines) if "await _observe_sylann(" in ln)
    wr = next(i for i, ln in enumerate(lines) if 'ctx.meta["desire_count"] =' in ln)

    assert wr < obs, (
        "ctx.meta['desire_count'] 写入(L%d) 晚于 _observe_sylann 调用(L%d) ⇒ "
        "_sylann_values() 经该调用读到的恒为默认 0.0（X4 同 X1 的读早于写模式）"
        % (wr + 1, obs + 1)
    )


def test_write_is_single_source():
    """只保留一处写入（避免两处漂移）。"""
    lines = _lines()
    n = sum(1 for ln in lines if 'ctx.meta["desire_count"] =' in ln)
    assert n == 1, "desire_count 出现 %d 处写入，应只有 1 处（前移后删除原处）" % n


def test_sylann_values_defaults_zero_without_meta():
    """坐实后果：meta 无 desire_count 时，_sylann_values 返回 0.0（非报错）。"""
    from anima.pipeline.stages.cognition import _sylann_values

    ctx = SimpleNamespace(text="你好", group_id=1, meta={})
    vals = _sylann_values(ctx)
    assert vals["desire_count"] == 0.0, (
        "期望默认 0.0（这正是 bug 的表现：恒 0 且无任何报错）"
    )


def test_sylann_values_reads_meta_when_present():
    """meta 有值时正常读出（证明读取通路本身是好的，只是时序错）。"""
    from anima.pipeline.stages.cognition import _sylann_values

    ctx = SimpleNamespace(text="你好", group_id=1, meta={"desire_count": 7})
    vals = _sylann_values(ctx)
    assert abs(vals["desire_count"] - 0.7) < 1e-9, vals


def test_desire_defined_before_write():
    """结构性守卫：写入点必须晚于 desire 变量的定义（否则前移会 NameError）。"""
    lines = _lines()
    d = next(i for i, ln in enumerate(lines)
             if ln.strip().startswith("desire = get_desire_system()"))
    w = next(i for i, ln in enumerate(lines) if 'ctx.meta["desire_count"] =' in ln)
    assert d < w, "desire 定义(L%d) 必须早于写入(L%d)" % (d + 1, w + 1)


def test_no_second_desire_add_between():
    """守卫：前移点与原写入点之间不得有 desire.add（否则数值会漂移）。"""
    lines = _lines()
    w = next(i for i, ln in enumerate(lines) if 'ctx.meta["desire_count"] =' in ln)
    obs = next(i for i, ln in enumerate(lines) if "await _observe_sylann(" in ln)
    seg = "\n".join(lines[w:obs])
    assert "desire.add(" not in seg, (
        "写入点与 _observe_sylann 之间出现 desire.add ⇒ 前移会改变数值，需重新评估"
    )
