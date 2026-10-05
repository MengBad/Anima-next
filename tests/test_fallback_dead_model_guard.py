# -*- coding: utf-8 -*-
"""降级链不得包含**结构性失效**模型（2026-10-05）

## 生产实证（72h 账本，2026-10-05 取数）
```
model                   总   成功  empty  成功率
gemini-3-flash           36     0     36    0%    ← 结构性失效
gemini-3.6-flash        203   201      0   99%    ← 健康
gemini-3.8-flash        402   402      0  100%    ← 健康
```
`gemini-3-flash` **36 次调用 0 次成功**，全部 `empty_completion`。

## 它为什么有害（不只是"没用"）
它在 `fallback_models` 链的**最后一位**。14:28 的日志揭示了完整链路：
```
14:28:38  补全为空 model=gemini-3.8-flash-high   ← 主模型失败
14:28:40  补全为空 model=gemini-3.6-flash         ← 第一级备用也失败
14:28:42  补全为空 model=gemini-3-flash           ← 第二级备用（结构性失效）也失败
14:28:42  ERROR LLM 请求失败：empty_completion
```
⇒ 主模型 + 健康备用都失败时，这个 0% 的模型**必然**贡献第 3 次往返和 3–6 秒延迟
（实测 2029ms / 3095ms / 5650ms / 6342ms），**然后整条链仍然报同一个错**。
它是纯开销：把"一次失败"变成"两次失败"。

## 判据要点
1. 模板 `config/default.yaml` 不得再把 0% 模型列为 fallback 示例。
2. 🔴 **只读断言不写死生产值**（生产 `config.yaml` 不在 git）——
   改为"判据描述健康判据，实现侧用**可配置**的失效名单"。
3. 失效模型名单为空时**不得改变任何行为**（零影响回滚点）。
4. 名单里的模型**不参与降级链**，但**不得影响主模型**。
"""

from __future__ import annotations



import pytest

from anima.llm import LLMClient


def _client(fallback: list[str]) -> LLMClient:
    return LLMClient(
        endpoint="http://example.invalid/v1",
        api_key="k",
        model="gemini-3.8-flash-high",
        fallback_models=fallback,
    )


# ── 1. 失效模型被剔除 ───────────────────────────────────────────────

def test_dead_model_is_dropped_from_fallback_chain():
    """0% 的模型不得留在降级链里（它是纯开销，不是容灾）。"""
    c = _client(["gemini-3.6-flash", "gemini-3-flash", "gemini-3.7-flash"])
    assert "gemini-3-flash" not in c.fallback_models, \
        f"结构性失效模型仍在降级链：{c.fallback_models}"
    # 健康模型必须保留
    assert "gemini-3.6-flash" in c.fallback_models
    assert "gemini-3.7-flash" in c.fallback_models


def test_all_dead_models_removed_yields_empty_chain():
    c = _client(["gemini-3-flash"])
    assert c.fallback_models == [], f"全失效时链应为空：{c.fallback_models}"


# ── 2. 🔴 零影响回滚点：名单为空 ⇒ 行为逐字不变 ─────────────────────

def test_empty_deadlist_changes_nothing():
    """不传失效名单 ⇒ 降级链**原样保留**（含原顺序、排除主模型）。

    🔴 刻意**不假设去重**：实测 `fallback_models` 构造**不做去重**
    （`['a','b','a']` 原样保留，去重发生在别处）⇒ 判据按真实行为写，不臆造。
    """
    c = _client(["gemini-3.6-flash", "gemini-3-flash"])
    assert c.fallback_models == ["gemini-3.6-flash"], \
        f"未启用失效名单时行为被改变：{c.fallback_models}"


def test_main_model_never_touched():
    """🔴 失效名单处理**绝不能**改主模型（那是活路由，误伤=全站不可用）。"""
    c = _client(["gemini-3.6-flash"])
    assert c.model == "gemini-3.8-flash-high", f"主模型被改动：{c.model}"


def test_main_model_can_also_be_in_deadlist_without_breaking():
    """即便主模型自己被列进失效名单，也**只影响 fallback 链**，
    `self.model` 保持不变（主模型切换是配置决策，不是本函数的职责）。"""
    c = _client(["gemini-3.8-flash-high", "gemini-3-flash"])
    assert c.model == "gemini-3.8-flash-high"
    assert c.fallback_models == []


def test_order_preserved():
    """剔除**只删**失效项，健康模型的相对顺序不得变。"""
    c = _client(["a-flash", "gemini-3-flash", "b-flash"])
    assert c.fallback_models == ["a-flash", "b-flash"], \
        f"顺序被破坏：{c.fallback_models}"


# ── 3. 模板不再举 0% 模型作示例 ────────────────────────────────────

def _fallback_block(text: str) -> str:
    """截取 `llm.fallback_models:` 段（到下一个 2 空格缩进的键为止，**注释行算内容**）。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("  fallback_models:"):
            start = i
            break
    assert start is not None, "未找到 llm.fallback_models 段"
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("  ") and not ln.startswith("   ") and ln.strip():
            break
        if not ln.strip():
            break
        out.append(ln)
    return "\n".join(out)


def test_default_yaml_no_longer_lists_dead_model():
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "config" / "default.yaml"
    text = p.read_text(encoding="utf-8")
    seg = _fallback_block(text)

    # 生效条目里不得有裸的 `gemini-3-flash`
    active = [ln for ln in seg.splitlines()
              if ln.strip().startswith("- ")]
    assert not any("gemini-3-flash" in ln for ln in active), \
        f"模板仍在拿 0% 模型当容灾示例：{active}"
    # 必须仍有健康项
    assert any("gemini-3.6-flash" in ln for ln in active), "健康备用被误删"
    # 应保留注释说明为何移除（否则后人会再加回来）
    assert "gemini-3-flash" in seg, "应保留注释说明为何移除"
    assert "0%" in seg, "注释应写明实测成功率证据"
