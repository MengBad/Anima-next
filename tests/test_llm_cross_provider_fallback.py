"""🔴 降级链必须能跨 provider 兜底（2026-10-06 生产数据驱动）。

## 生产事实（72h 账本，真实数字）

| actual_model | 总 | 成功 | 成功率 |
|---|---:|---:|---:|
| `gemini-3.8-flash-n`（= `req gemini-3.8-flash-high` 规范化） | 1952 | 1952 | **100%** |
| `gemini-3.8-flash` | 377 | 377 | **100%** |
| `gemini-3.6-flash`（降级一级） | 201 | 194 | 96.5% |
| 🔴 `gemini-3-flash`（降级二级） | 32 | 0 | **0%** |
| 🔴 `gemini-3.8-flash-high`（未规范化直落） | 8 | 0 | **0%** |

⇒ 主模型**从不出问题**；一旦需要降级，**两级都是同一个 provider（google）**
⇒ 同 provider 返空往往是**上游整体抖动**，换同族模型**救不回来**
⇒ 00:25 那条用户面事故正是如此：`3.8-flash-high` 返空 → `3.6-flash` 返空 → 兜底话术。

## CPA 实测有 73 个可用模型（`/v1/models`）
`claude-opus-5-5-high` / `claude-sonnet-5-5-high` / `grok-4.7` / `kimi-k3` /
`deepseek-v4-pro` / `glm-5.3` …

⇒ **加一个异族兜底，是唯一能治「同族全灭」的结构性修法**。

## 契约
- 异族兜底**只加一档**，且**排在同族之后**（同族优先，异族保底）
- 异族**不得**是主模型或已有 fallback（去重）
- 配置驱动、可空（空 ⇒ 行为**逐字不变**，是零影响回滚点）
- 🔴 **不改**主模型、**不改**同族顺序、**不改**返空判定（`continue` 语义）
"""
import pytest

from anima.llm import LLMClient


def _client(fallback, extra=None, model="gemini-3.8-flash-high"):
    kw = {"extra_fallback_models": extra} if extra is not None else {}
    return LLMClient(
        endpoint="http://x.invalid/v1", api_key="k", model=model,
        fallback_models=fallback, **kw)


# ── 1. 异族兜底确实进链，且排在同族之后 ──────────────────────────

def test_cross_provider_appended_after_same_family():
    c = _client(["gemini-3.6-flash"], ["claude-sonnet-5-5-high"])
    assert c.fallback_models == ["gemini-3.6-flash", "claude-sonnet-5-5-high"], \
        f"异族未追加到链尾：{c.fallback_models}"


def test_empty_extra_changes_nothing():
    """🔴 零影响回滚点：不配置 ⇒ 行为**逐字**不变。"""
    c = _client(["gemini-3.6-flash"], [])
    assert c.fallback_models == ["gemini-3.6-flash"], \
        f"空异族列表改变了行为：{c.fallback_models}"
    c2 = _client(["gemini-3.6-flash"])
    assert c2.fallback_models == ["gemini-3.6-flash"]


def test_none_extra_changes_nothing():
    c = _client(["gemini-3.6-flash"])
    assert c.fallback_models == ["gemini-3.6-flash"], \
        f"未传该参数时行为被改变：{c.fallback_models}"


# ── 2. 🔴 去重与「绝不误伤主模型」 ───────────────────────────────

def test_main_model_never_in_extra():
    """异族里写了主模型 ⇒ 必须剔除（误伤主模型 = 全站不可用）。"""
    c = _client(["gemini-3.6-flash"],
                ["gemini-3.8-flash-high", "claude-sonnet-5-5-high"])
    assert c.fallback_models == ["gemini-3.6-flash", "claude-sonnet-5-5-high"], \
        f"主模型未被剔除：{c.fallback_models}"


def test_extra_deduped_against_fallback():
    c = _client(["gemini-3.6-flash", "kimi-k3"], ["kimi-k3", "grok-4.7"])
    assert c.fallback_models == ["gemini-3.6-flash", "kimi-k3", "grok-4.7"], \
        f"异族与同族未去重：{c.fallback_models}"


def test_blank_entries_dropped():
    c = _client(["gemini-3.6-flash"], ["", "  ", "grok-4.7", "   "])
    assert c.fallback_models == ["gemini-3.6-flash", "grok-4.7"], \
        f"空条目未被剔除：{c.fallback_models}"


# ── 3. 🔴 同族顺序绝不可被改动（判别顺序降级 vs 追加）───────────

def test_same_family_order_preserved():
    c = _client(["a-flash", "b-flash", "c-flash"], ["x-pro", "y-pro"])
    assert c.fallback_models[:3] == ["a-flash", "b-flash", "c-flash"], \
        f"同族相对顺序被破坏：{c.fallback_models}"


def test_extra_order_preserved():
    c = _client(["a-flash"], ["z-pro", "y-pro", "x-pro"])
    assert c.fallback_models == ["a-flash", "z-pro", "y-pro", "x-pro"], \
        f"异族相对顺序被破坏：{c.fallback_models}"


# ── 4. 链长上限（不能无限试模型）────────────────────────────────

def test_chain_is_bounded():
    """🔴 链长必须有上限，**且**不得因追加异族而失控。

    设计决策（不是绕过上限）：上限 **4 → 5**，语义是
    「同族最多 4 档 + 异族保底 1 档」。

    🔴 为何必须多一档：原 `[:4]` 是在 `fallback_models` **之后**切的，
    追加异族时若不一起切，`a1..a4 + z1` 会绕过约束。
    形态上仍是有界常量（**不是无限链**），且实测最坏多一次 HTTP 往返。
    """
    c = _client(["a1", "a2", "a3", "a4", "a5"], ["z1", "z2", "z3"])
    assert len(c.fallback_models) == 5, \
        f"链长应为 5（同族4+异族1）：{c.fallback_models}"
    # 🔴 异族只保底**一档** —— 再多会把最坏延迟拖长（每档一次往返）
    assert c.fallback_models[-1] == "z1", \
        f"异族应只保留 1 档：{c.fallback_models}"
    # 同族占满前 4 档且**相对顺序不变**
    assert c.fallback_models[:4] == ["a1", "a2", "a3", "a4"], \
        f"同族档位被挤：{c.fallback_models}"


# ── 5. 🔴 既有行为不受影响（0% 模型硬拦仍生效）─────────────────

def test_dead_model_still_blocked_in_both_lists():
    """🔴 r19 的 `_DEAD_FALLBACK_MODELS` 对**两个列表**都生效
    ——否则有人把 0% 模型塞进异族列表就绕过了修复。"""
    c = _client(["gemini-3.6-flash"], ["gemini-3-flash", "grok-4.7"])
    assert "gemini-3-flash" not in c.fallback_models, \
        f"0% 模型从异族列表绕过了硬拦：{c.fallback_models}"
    assert "grok-4.7" in c.fallback_models


# ── 6. 🔴 真实事故形态回放：两级同族返空 ⇒ 异族被试 ───────────────

@pytest.mark.asyncio
async def test_real_incident_shape_reaches_cross_provider(monkeypatch):
    """🔴 复现 00:25 那条真实事故的形状。

    主模型返空 → 同族降级返空 → **异族必须被试**（而不是直接放弃出兜底话术）。
    """
    tried = []

    async def fake_post(self, payload, headers):
        tried.append(payload.get("model"))
        return None            # 一律返空，逼它走完整条链

    monkeypatch.setattr(LLMClient, "_try_post", fake_post)
    c = _client(["gemini-3.6-flash"], ["claude-sonnet-5-5-high"])
    c._empty_completion = True
    await c._post({"model": c.model, "messages": []}, {})

    assert tried == ["gemini-3.8-flash-high", "gemini-3.6-flash",
                     "claude-sonnet-5-5-high"], \
        f"异族未被尝试（用户就会看到兜底话术）：{tried}"


@pytest.mark.asyncio
async def test_cross_provider_can_actually_succeed(monkeypatch):
    """异族成功时必须正常返回，且记一次 failover。"""
    calls = {"n": 0}

    async def fake_post(self, payload, headers):
        calls["n"] += 1
        if calls["n"] <= 2:                     # 主模型 + 同族都返空
            self._empty_completion = True
            self.last_status = 200
            return None
        return {"choices": [{"message": {"content": "来自异族的回复"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr(LLMClient, "_try_post", fake_post)
    c = _client(["gemini-3.6-flash"], ["claude-sonnet-5-5-high"])
    data = await c._post({"model": c.model, "messages": []}, {})

    assert data is not None, "异族成功却仍返回 None"
    assert data["choices"][0]["message"]["content"] == "来自异族的回复"
