"""`reasoning_effort` 的**配置入口**（2026-10-03 补）。

背景：2026-10-02 给 `LLMClient` 加了 `module_reasoning_effort` 参数与默认表
（`{"sticker_label": "minimal", "vision": "minimal"}`），但**没接配置** ——
`LLMConfig` 没有该字段、`cli.py` 构造时也没传。后果是：生产确实生效（走代码默认表），
但**没有配置回滚点**（想关掉只能改代码）。本项目要求「一键回滚」，故补齐。

语义（三层，与 `LLMClient.__init__` 一致，不得含糊）：
- 配置缺省 / `None` ⇒ 用**代码默认表**（sticker_label / vision = minimal）
- 显式 `{}`        ⇒ **全关**（不下发任何 reasoning_effort）
- 显式非空 dict    ⇒ **整体替换**默认表
"""

from __future__ import annotations

from pathlib import Path

from anima.core.config import LLMConfig
from anima.llm import LLMClient, _DEFAULT_REASONING_EFFORT

_ROOT = Path(__file__).resolve().parents[1]


def _client(**kw) -> LLMClient:
    return LLMClient("http://127.0.0.1:1/v1", "k", retries=0, retry_backoff_sec=0, **kw)


def test_llm_config_exposes_reasoning_effort_field():
    """配置类必须有该字段，否则生产无法覆盖（这正是本次要补的缺口）。"""
    cfg = LLMConfig()
    assert hasattr(cfg, "module_reasoning_effort")
    assert cfg.module_reasoning_effort is None, "缺省必须是 None（= 用代码默认表），不是 {}"


def test_cli_passes_reasoning_effort_to_client():
    """`cli.py` 必须把配置传进 `LLMClient`，否则字段形同虚设（静默失效）。"""
    src = (_ROOT / "anima" / "cli.py").read_text(encoding="utf-8-sig")
    assert "module_reasoning_effort=" in src


def test_none_uses_code_default_table():
    c = _client()
    assert c.module_reasoning_effort == _DEFAULT_REASONING_EFFORT
    assert c.reasoning_effort_for("sticker_label") == "minimal"
    assert c.reasoning_effort_for("vision") == "minimal"
    assert c.reasoning_effort_for("chat") is None


def test_explicit_empty_dict_disables_all():
    """一键回滚：显式空表 ⇒ 对任何模块都不下发。"""
    c = _client(module_reasoning_effort={})
    assert c.module_reasoning_effort == {}
    assert c.reasoning_effort_for("sticker_label") is None
    assert c.reasoning_effort_for("vision") is None


def test_explicit_custom_table_replaces_default():
    c = _client(module_reasoning_effort={"chat": "low"})
    assert c.reasoning_effort_for("chat") == "low"
    assert c.reasoning_effort_for("sticker_label") is None, "替换语义：默认表不再叠加"
