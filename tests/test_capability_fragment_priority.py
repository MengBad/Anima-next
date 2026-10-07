"""能力清单片段的预算优先级（r23，2026-10-06）。

## 背景：生产 7d 账本驱动的定序修正

生产 128 个「system 超预算」回合里，各片段被丢频次（读数，非推断）：

    capabilities            128/128 = 100%
    external_exploration     63/128 =  49%
    reply_learning           30/128 =  23%
    tool_learning            25/128 =  20%

而代码里 `capabilities` 的 priority 是 **20**（全表最低，`cognition.py:787`），
`external_exploration` 是 **25**（`cognition.py:798`）。

🔴 **这是定序反了**：一个 100% 被丢的片段，排在只丢 49% 的片段**之下**。
`ContextComposer.compose_system` 按 `(priority, 注入序)` 升序删除
（`pipeline/context.py:162`）⇒ priority 越低越先被删 ⇒ `capabilities` 必然第一个出局。

## 为什么零和代价可接受（量化依据，不是拍脑袋）

生产实测（venv 内真跑 `get_capability_manager(...).build_fragment()`）：
**能力库 3 条、片段 210 字 / 估 281 token**（`capabilities.py:345-363` 已做过三重瘦身：
删 3 行固定开场白、不带 `how_to_use`、每条压一行）。

把它抬到 `external_exploration` 之上，代价 = 挤掉 `external_exploration` 的
**部分**回合（不是全部 —— 只有「余量 < 281 token 且两者都在场」的回合才二选一）。

## 🔴 判据不覆盖的边界（诚实声明）

本判据只钉**相对定序**（capabilities 不得比 external_exploration 更先被丢），
**不**钉「capabilities 必须 100% 进 prompt」—— 那需要抬高总预算，属 owner 预算决策，
不在本轮范围（见 reports/2026-10-06_inout-audit-production-healthcheck.md §8）。

反向对照同样重要：`vector_recall`(60)/`relationship`(100) 等高优先级片段
**必须仍然优先于 capabilities 保留**，防「无脑抬高」把所有东西都顶上来。
"""

from __future__ import annotations

import ast
from pathlib import Path

from anima.pipeline.context import (
    ContextComposer,
    PromptFragment,
    estimate_text_tokens,
)

_COGNITION = Path(__file__).resolve().parents[1] / "anima" / "pipeline" / "stages" / "cognition.py"


def _priorities() -> dict[str, int]:
    """用 AST 取 ``add_system_fragment(<字面量 key>, ..., <字面量 priority>)`` 的定序。

    🔴 **刻意用 AST 不用子串**：`capabilities` 这个词在文件里出现多次
    （`get_capability_manager` / `cap_cfg` / `capability_cfg_dict`），
    子串匹配会把无关行算进来 —— 那正是「门禁字面量断言必须查全」的同类坑。
    """
    tree = ast.parse(_COGNITION.read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "add_system_fragment"):
            continue
        if len(node.args) < 3:
            continue
        key_node, _content_node, prio_node = node.args[0], node.args[1], node.args[2]
        # 只认「key 与 priority 都是常量」的调用（键名变量/优先级变量的另行人工确认）
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue
        if not (isinstance(prio_node, ast.Constant) and isinstance(prio_node.value, int)):
            continue
        out[key_node.value] = prio_node.value
    return out


# ── ① 定序判据：capabilities 不得比 external_exploration 更先被丢 ──────────

def test_capabilities_priority_is_not_below_external_exploration():
    """生产账本：capabilities 100% 被丢 vs external_exploration 49%
    ⇒ priority 必须让 capabilities **后**被丢。"""
    prios = _priorities()
    assert "capabilities" in prios, "AST 未取到 capabilities 的定序（键名变成变量了？）"
    assert "external_exploration" in prios, "AST 未取到 external_exploration 的定序"
    assert prios["capabilities"] >= prios["external_exploration"], (
        "capabilities=%s 仍低于 external_exploration=%s ⇒ 生产账本里的 100%% vs 49%% "
        "会被原样复现（定序反了）" % (prios["capabilities"], prios["external_exploration"])
    )


def test_capabilities_priority_strictly_exceeds_external_exploration():
    """🔴 **平齐不够，必须严格大于** —— 这条是本轮真踩出来的坑。

    `compose_system` 的排序键是 `(priority, 注入序)`：**同 priority 时由注入序决定**。
    而 capabilities 在 `cognition.py` 里**先注入**（:787 < :798）⇒ 平齐时它照样先被丢。

    本地同预算（11000）实测：
        prio=20 → dropped=capabilities,external_exploration
        prio=25 → dropped=capabilities,external_exploration   ← 改了等于没改
        prio=26 → dropped=external_exploration                  ← 生效

    ⚠️ 只断言「常量大小」的判据会写出假绿：``>=`` 通过但行为没变。
    """
    prios = _priorities()
    assert prios["capabilities"] > prios["external_exploration"], (
        "capabilities=%s 与 external_exploration=%s 平齐 ⇒ 由**注入序**决定，"
        "而 capabilities 先注入（cognition.py:787 < :798）⇒ 行为不变"
        % (prios["capabilities"], prios["external_exploration"])
    )


def test_capabilities_injected_before_exploration_in_source():
    """钉住「注入序」这个前提：若将来调换注入顺序，本判据的推理需重新评估。

    这是**显式记录前提**而非行为断言 —— 它自己不红，但拆掉它会让上面那条
    判据的推理链断掉而没人察觉。
    """
    tree = ast.parse(_COGNITION.read_text(encoding="utf-8"))
    order: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "add_system_fragment"):
            continue
        if not node.args:
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if key.value in ("capabilities", "external_exploration"):
                order.append(key.value)
    assert "capabilities" in order and "external_exploration" in order, (
        "两个片段的 add_system_fragment 调用没都取到，注入序前提无法验证（order=%s）" % order
    )
    assert order.index("capabilities") < order.index("external_exploration"), (
        "注入序已变化：现在 external_exploration 先注入 ⇒ 若两者平齐，"
        "**external_exploration 反而会先被丢** ⇒ 上面的判据需重写（order=%s）" % order
    )


def test_capabilities_priority_is_not_global_minimum():
    """它曾经是**全表最低**（20）。约束：不得再低于任何其他可选片段。"""
    prios = _priorities()
    others = {k: v for k, v in prios.items() if k != "capabilities"}
    assert others, "AST 只取到 capabilities 一个，判据失去意义"
    lowest_other = min(others.values())
    assert prios["capabilities"] >= lowest_other, (
        "capabilities=%s 低于其它片段最低值 %s（%s）⇒ 它仍会第一个被丢"
        % (prios["capabilities"], lowest_other,
           sorted(k for k, v in others.items() if v == lowest_other))
    )


# ── ② 行为级判据：同预算下 capabilities 应活过 external_exploration ───────

def test_composer_keeps_capabilities_when_exploration_must_be_dropped():
    """行为级（不只看常量）：预算只够保一个时，capabilities 必须活。

    🔴 **按真实注入序**（capabilities 先、exploration 后，对齐 `cognition.py:787/798`）
    且**优先级严格大于** —— 这样「活下来」唯一可能的原因是 priority 定序，
    排除了「碰巧后注入」这个混淆项。
    🔴 **预算按内容算，不写魔法数字**：`ContextComposer` 有 ``max(1024, ...)``
    下限，写 1024 会让两个片段都放不下 ⇒ 测到的是「预算不足」而非「定序错」。
    """
    base = "人格核心。" * 25
    suffix = "\n## 身份锁定\n永远是哈基米。"
    frag = "\n片段内容。" * 120
    budget = estimate_text_tokens(base + suffix + frag) + 40
    composer = ContextComposer(system_token_budget=budget)
    prompt, stats = composer.compose_system(
        base,
        [
            PromptFragment("capabilities", frag, 26),    # 真实定序：先注入 + 更高 priority
            PromptFragment("external_exploration", frag, 25),
        ],
        suffix,
    )
    assert "capabilities" not in stats.dropped_keys, (
        "capabilities 被丢了（dropped=%s, budget=%d）—— 同预算下它必须活过 "
        "external_exploration" % (stats.dropped_keys, budget)
    )
    assert "external_exploration" in stats.dropped_keys, (
        "reverse 对照失败：不该丢的反而丢了（dropped=%s）" % (stats.dropped_keys,)
    )
    assert prompt.startswith("人格核心。")


def test_composer_equal_priority_is_not_enough():
    """🔴 反向钉住「平齐无效」这个坑：同 priority 时**先注入者先被丢**。

    这条不随实现变（它断言的是 `compose_system` 的既有语义），
    作用是让「把 priority 调成 25 就以为修好了」这个错误在 CI 里就红。
    """
    composer = ContextComposer(system_token_budget=1100)
    base = "人格核心。" * 25
    suffix = "\n## 身份锁定\n永远是哈基米。"
    frag = "\n片段内容。" * 120
    _prompt, stats = composer.compose_system(
        base,
        [
            PromptFragment("capabilities", frag, 25),    # 同 priority 但先注入
            PromptFragment("external_exploration", frag, 25),
        ],
        suffix,
    )
    assert stats.dropped_keys and stats.dropped_keys[0] == "capabilities", (
        "同 priority + 先注入 ⇒ capabilities 应第一个被丢，实际 dropped=%s"
        % (stats.dropped_keys,)
    )


# ── ③ 反向对照：高优先级片段**必须**仍优先于 capabilities 保留 ────────────

def test_high_priority_fragments_still_win_over_capabilities():
    """防「无脑抬高」：priority 60/100 的记忆类片段必须仍然先于 capabilities 保留。

    🔴 **预算要给够**（改前 1024 会把三个片段全挤掉 ⇒ 三个断言一起红，
    那是「预算不足」不是「定序错」，判据必须能区分这两件事）：
    用 2100，让 60/100 两个片段必然留下、只有 capabilities 需要被丢。
    """
    composer = ContextComposer(system_token_budget=2100)
    frag = "\n片段内容。" * 120
    _, stats = composer.compose_system(
        "人格核心。" * 25,
        [
            PromptFragment("capabilities", frag, 26),      # 真实定序
            PromptFragment("vector_recall", frag, 60),
            PromptFragment("relationship", frag, 100),
        ],
        "\n## 身份锁定\n永远是哈基米。",
    )
    dropped = set(stats.dropped_keys)
    assert "vector_recall" not in dropped, (
        "记忆片段被挤掉了 —— 抬 capabilities 过头了（dropped=%s）" % (sorted(dropped),))
    assert "relationship" not in dropped, (
        "关系片段被挤掉了 —— 抬 capabilities 过头了（dropped=%s）" % (sorted(dropped),))
    assert "capabilities" in dropped, (
        "本判据只约束相对定序；预算只够一个时 capabilities 仍可被丢（那是总预算问题）"
    )


# ── ④ 零影响点：不动 protected 片段与人格/护栏 ───────────────────────────

def test_protected_and_required_content_unaffected_by_priority_change():
    """反人格：无论可选片段怎么定序，人格 base 与身份护栏 suffix 都不可裁。"""
    composer = ContextComposer(system_token_budget=1024)
    base = "人格核心。" * 25
    suffix = "\n## 身份锁定\n永远是哈基米。"
    frag = "\n片段内容。" * 120
    prompt, stats = composer.compose_system(
        base,
        [
            PromptFragment("capabilities", frag, 26),
            PromptFragment("inner_state", frag, 55, protected=True),
        ],
        suffix,
    )
    assert prompt.startswith(base) and prompt.endswith(suffix)
    assert "inner_state" not in stats.dropped_keys, "protected 片段被裁了"
    assert stats.protected_over_budget is False or stats.estimated_tokens > 1024
