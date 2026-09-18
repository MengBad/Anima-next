"""`config/default.yaml` 段级完整性守卫（D-06，2026-09-18）。

问题
----
`default.yaml` 曾缺 **10 个段**（`capability_layer` / `danger` / `x_browser` / `tavily` /
`contradiction` / `iteration` / `repo_read` / `rumination` / `scrapling` / `social`）。
代码里有默认值，所以运行时无碍；但缺段让「仓库默认」的键集无法作为参考基准 ——
`deploy/check_config_drift.py` 靠它 ∪ `production.example.yaml` 去比对生产实配。

本组测试钉住三件事：
    1. 段级完整：AnimaConfig 的每个顶层段都在，且 default.yaml 不多出无效段；
    2. 可解析：整份 yaml 能直接喂给 `AnimaConfig`（含 5 个 SecretStr 字段）；
    3. 那次补齐**只补形状、不改值** —— 这 10 段必须逐字等于代码默认。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import SecretStr

from anima.core.config import AnimaConfig

_ROOT = Path(__file__).resolve().parents[1]
_CFG = _ROOT / "config" / "default.yaml"

# 2026-09-18 用「代码默认值」补齐的段（D-06）。它们的值应与代码默认逐字一致；
# 若将来**有意**调整其中某段，请同步更新本白名单并在 ENGINEERING_DEBT_LEDGER.md 登记。
_BACKFILLED_BY_D06 = (
    "capability_layer", "contradiction", "danger", "iteration", "repo_read",
    "rumination", "scrapling", "social", "tavily", "x_browser",
)


def _raw() -> dict:
    return yaml.safe_load(_CFG.read_text(encoding="utf-8")) or {}


def _norm(value):
    """把 pydantic 的默认值归一化成 yaml 可表达的形态（SecretStr / tuple）。"""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_norm(v) for v in value]
    return value


def test_every_config_section_is_present():
    """段级完整：AnimaConfig 的顶层字段不得在 default.yaml 里缺席。"""
    raw = _raw()
    missing = sorted(set(AnimaConfig.model_fields) - set(raw))
    assert missing == [], f"default.yaml 缺段: {missing}"


def test_no_orphan_sections_in_default_yaml():
    """反向：default.yaml 不得有 AnimaConfig 不认的段（会被 extra 校验拒绝）。"""
    raw = _raw()
    extra = sorted(set(raw) - set(AnimaConfig.model_fields))
    assert extra == [], f"default.yaml 有多余段: {extra}"


def test_default_yaml_parses_into_config():
    """整份 yaml 必须能被 AnimaConfig 直接解析（回归：SecretStr 写成 null 会 ValidationError）。"""
    cfg = AnimaConfig(**_raw())
    assert cfg is not None
    assert isinstance(cfg.llm.api_key, SecretStr)


def test_no_required_field_depends_on_absence():
    """若某字段是必填，它必须在 default.yaml 里有显式值（否则仓库默认不可用）。"""
    raw = _raw()
    required = [name for name, f in AnimaConfig.model_fields.items() if f.is_required()]
    missing = [name for name in required if name not in raw]
    assert missing == [], f"必填段缺席: {missing}"


def test_d06_backfilled_sections_equal_code_defaults():
    """D-06 补齐的 10 段必须逐字等于代码默认（证明「只补形状、不改值」）。"""
    raw = _raw()
    defaults = AnimaConfig().model_dump()
    drifted = {}
    for seg in _BACKFILLED_BY_D06:
        want = _norm(defaults.get(seg))
        got = raw.get(seg)
        if _norm(got) != want:
            drifted[seg] = (got, want)
    assert not drifted, f"补齐的段被改成了非默认值: {sorted(drifted)}"
