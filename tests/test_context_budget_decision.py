"""system 预算与 input 预算的关系约束（r25，2026-10-06；r26 重定）。

## 为什么会有这条判据

owner 指令「你来决定」是否抬 `system_token_budget`（system 长期 95%+ 顶格，
`capabilities` 100% 被丢）。实测数据：

- system 用量 p50=10610 / p90=10946 / max=10992，预算 11000 ⇒ **7d 128 回合触顶**
- input 用量 7d **只有 6 回合**触顶（`dropped=1..7` 条历史）
- 落盘侧 `avg_system=10540.7` / `avg_input=11702.8` ⇒ 历史+当前输入 ≈ 1162 token，
  距 input 预算 16000 **余 4297**

## 🔴 被证伪的旧假设（本条判据就是它的守卫）

「抬 system = 抢历史 token」—— **不成立**：
- `compose_system` 只用 `system_token_budget` 裁可选片段
- `compose_messages` 只用 `input_token_budget` 裁历史，且 `protected` 含所有 system 消息
- ⇒ **两个预算是独立裁剪器**，只有当 `input` 也顶格时，抬 system 才真的挤历史
- 生产实证：抬到 12000 前后 `context_history_dropped` **均为 0**，
  历史确实没被挤。

🔴 若哪天有人把两者改成共享池，这条判据会红 —— 那正是要重新评估的信号。

## 🔴🔴 r26（2026-10-06）：r25 抬到 12000 的效果**被生产证伪**

r25 预测「12000 下 `capabilities` 保留率 67% → 100%」。生产实测（重启后头 3 个回合）：

```
17:5x  dropped=external_exploration,capabilities,reply_learning,tool_learning        tokens=11729/12000
17:5x  dropped=external_exploration,capabilities,reply_learning,tool_learning        tokens=11833/12000
18:20  dropped=external_exploration,capabilities,reply_learning,tool_learning,
       sylann_expression,milestones,group_presence,conversation_depth               tokens=11969/12000
```

⇒ **3/3 回合仍然丢 `capabilities`**，抬 1000 远远不够。

**根因是测量口径错**：r25 用「最紧回合终值 10992 ⇒ 余量 `12000-10992=1008 >> 281`」
推断「够用」。但 `10992` 是**裁剪之后**的终值 —— 它只说明**裁到哪停手**，
**说明不了超了多少**。真实超出量 = `pre_drop - budget`，本轮之前**从未落盘**。

同理 r23 的「保留率上界 67%」也是从终值推的，两轮预测同因失效。

⇒ **纪律**：判断预算是否够用，**只能看 `pre_drop_tokens`（裁剪前总量）**；
只看 `estimated_tokens`（终值）必然得出「刚好差一点」的假象。
`CompositionStats` 现已补 `required_tokens` / `pre_drop_tokens` 并逐回合落日志。

## r26 重定值（本文件的锚）

`input_token_budget=20000` / `system_token_budget=16000`：差额 **4000**，
与旧档 `16000-12000=4000` **完全一致** ⇒ 历史额度零损失，
system 可选片段余量从 6480 抬到 10480（不可裁底量实测 5520）。
"""

from __future__ import annotations

from anima.pipeline.context import ContextComposer, PromptFragment, estimate_text_tokens


# ── ① 生产配置的三档预算关系（决策的锚）───────────────────────────────

def test_system_budget_stays_below_input_with_history_reserve():
    """r26 决策值：`input_token_budget=20000`，`system_token_budget=16000`。

    断言的是**关系**（差额够历史用），不是硬编码单值 —— 这样将来调 input 时
    判据仍能判断是否安全。
    """
    c = ContextComposer(input_token_budget=20000, system_token_budget=16000)
    assert c.system_token_budget == 16000
    assert c.system_token_budget < c.input_token_budget, "system 必须小于 input"
    # 历史可用额度 = input - system（实测历史+当前输入约 1162 token）
    reserve = c.input_token_budget - c.system_token_budget
    assert reserve >= 4000, (
        "system=%d 时留给历史的只剩 %d token —— 不得低于旧档的 4000"
        % (c.system_token_budget, reserve)
    )
    # 🔴 反向守卫：差额不得小于实测历史用量，否则历史会被开始裁剪
    assert reserve > 1162, "历史额度已跌破实测用量 1162，会开始丢历史"


def test_system_budget_is_capped_by_input_budget():
    """🔴 `system_token_budget` 有 `min(system, input)` 上限（`context.py:130`）。

    写这条是为了让「把 system 设成比 input 还大」不会静默通过 ——
    它会被夹到 input，配置值与实际行为不一致，属静默失效。
    """
    c = ContextComposer(input_token_budget=16000, system_token_budget=99999)
    assert c.system_token_budget == 16000, "超大 system 预算应被夹到 input"


# ── ② 收益：capabilities 片段在新预算下进得去 ─────────────────────────

def test_capabilities_fits_under_raised_budget_with_real_cost():
    """r26：用**裁剪前总量**（而不是终值）判断片段是否进得去。

    🔴 本判据替代了 r25 的同名版本。旧版本拿「终值距预算的余量」当缺口
    （`11000-10992=8`），据此断言「抬到 12000 就够」—— 生产直接证伪。
    终值是裁剪停手的位置，**恒 ≤ budget**，因此**永远算得出「只差一点点」**，
    这正是 r23/r25 两轮同因失效的地方。

    正确口径：`overflow = pre_drop_tokens - budget`。
    `capabilities` 生产实测 **281 token**。只要 overflow > 0 就必然有片段出局。
    """
    cap_cost = 281                     # 生产 venv 实测 build_fragment() 的估 token
    # 不可裁底量（人格 base + 护栏 suffix）生产实测 ≈ 5520
    required_prod = 5520
    # 生产实测：可选片段总量已远超旧闸门，导致 7d 138/138 回合丢片段
    frags_prod = 8500

    pre_drop = required_prod + frags_prod

    for budget in (11000, 12000):
        assert pre_drop - budget > cap_cost, (
            f"预算 {budget} 下 overflow={pre_drop-budget} > capabilities({cap_cost}) "
            "⇒ 必然仍丢 capabilities（r25 预测 100% 保留是错的）"
        )

    # r26 新预算：可选片段余量必须真的容得下实测片段总量
    new_budget = 16000
    assert new_budget - required_prod >= frags_prod, (
        f"新预算 {new_budget} 留给可选片段仅 {new_budget-required_prod}，"
        f"不足实测 {frags_prod} ⇒ 仍会丢"
    )


# ── ②b 观测口径：pre_drop / required 必须可证伪 ────────────────────────

def test_pre_drop_tokens_exposes_true_overflow():
    """🔴 r26 新增的观测字段必须真的落数，否则「够不够」永远只能猜。

    `pre_drop_tokens` 应等于「不可裁底量 + 全部片段」，且与终值之差
    恒等于被丢片段之和 —— 这是判断预算取值**唯一**可靠的口径。
    """
    base = "人格核心。" * 100
    suffix = "\n## 身份锁定"
    frags = [
        PromptFragment("capabilities", "能力清单。" * 100, 26),
        PromptFragment("external_exploration", "探索笔记。" * 400, 25),
        PromptFragment("vector_recall", "记忆召回。" * 10, 60),
    ]
    frag_tokens = sum(estimate_text_tokens(f.content) for f in frags)
    required = estimate_text_tokens(base + suffix)

    # 预算故意开小，制造确定的溢出
    c = ContextComposer(system_token_budget=required + 50)
    _p, st = c.compose_system(base, frags, suffix)

    assert st.required_tokens == required, "required 应等于 base+suffix 的估算"
    assert st.pre_drop_tokens == required + frag_tokens, (
        "pre_drop 应等于不可裁底量 + **全部**片段（裁剪前）"
    )
    assert st.pre_drop_tokens > st.budget, "本例应确定溢出"
    assert st.dropped_keys, "溢出时必然有片段被丢"

    # 终值恒 ≤ 预算 —— 正是它「算不出真实缺口」的原因
    assert st.estimated_tokens <= st.budget

    # 零溢出时 pre_drop 仍要落数（否则「没丢」与「没样本」无法区分）
    roomy = ContextComposer(system_token_budget=required + frag_tokens + 500)
    _p2, st2 = roomy.compose_system(base, frags, suffix)
    assert st2.dropped_keys == ()
    assert st2.pre_drop_tokens == required + frag_tokens
    assert st2.pre_drop_tokens <= st2.budget, "零溢出时 pre_drop 应 ≤ 预算"


# ── ③ 反向对照：抬预算**不得**放松人格/护栏与 protected ────────────────

def test_raising_system_budget_does_not_change_trimming_rules():
    """预算变大只应「多留片段」，**不得**改变裁剪语义。"""
    base = "人格核心。" * 200
    suffix = "\n## 身份锁定\n永远是哈基米。"
    low = ContextComposer(system_token_budget=11000)
    high = ContextComposer(system_token_budget=12000)
    frags = [
        PromptFragment("capabilities", "能力清单。" * 200, 26),
        PromptFragment("external_exploration", "探索笔记。" * 200, 25),
    ]
    _p, st_low = low.compose_system(base, frags, suffix)
    p_high, st_high = high.compose_system(base, frags, suffix)

    # 人格与护栏永远完整（两个预算下）
    for p in (_p, p_high):
        assert p.startswith(base) and p.endswith(suffix)

    # 降级顺序不变（低 priority 先出局）
    order_low = list(st_low.dropped_keys)
    order_high = list(st_high.dropped_keys)
    for keys in (order_low, order_high):
        if "capabilities" in keys and "external_exploration" in keys:
            assert keys.index("capabilities") == 0, (
                "capabilities 必须先于 external_exploration 被丢，got=%s" % keys)


def test_protected_still_survives_raised_budget():
    """protected 片段在更高预算下同样不可裁（语义不随预算变）。"""
    c = ContextComposer(system_token_budget=12000)
    prompt, stats = c.compose_system(
        "人格核心。" * 200,
        [
            PromptFragment("inner_state", "此刻状态。" * 300, 55, protected=True),
            PromptFragment("capabilities", "能力。" * 300, 26),
        ],
        "\n## 身份锁定\n永远是哈基米。",
    )
    assert "inner_state" not in stats.dropped_keys
    assert prompt.endswith("永远是哈基米。")


# ── ④ 独立裁剪（被证伪的旧假设守卫）────────────────────────────────

def test_system_and_input_budgets_are_independent_composers():
    """🔴 两个预算是**独立裁剪器**，不是共享池。

    这条判据是「抬 system 不必然抢历史」的代码级证据：
    同一 `ContextComposer` 里，抬 system 预算**不改变** input 预算本身。
    """
    c = ContextComposer(input_token_budget=20000, system_token_budget=16000)
    assert c.input_token_budget == 20000, "抬 system 意外改动了 input 预算"
    assert c.system_token_budget == 16000
    # 构造一次 compose_messages，看它用的是 input 预算
    msgs = [
        {"role": "system", "content": "系统提示。" * 100},
        {"role": "user", "content": "很长的历史。" * 2000},
    ]
    _composed, stats = c.compose_messages(msgs)
    assert stats.budget == 20000, "compose_messages 的预算应是 input，不是 system"