"""配置漂移基线的**跨重启可见性**守卫（D-08，2026-09-18）。

背景（这条债为什么重要）：
    基线原先只在内存，且由**本次启动的**配置建立 ⇒ 「手工改 `config.yaml` 然后
    `systemctl restart anima`」——生产上最常用的变更路径——完全绕过监控：
    新进程把新值当基线，`config_permission_drift` 审计历史上 0 条。
    生产实测该项敏感面里含 `capability_mode` / `python_exec_enabled` 等治理开关，
    而 2026-09-05 前后的四项翻转正是走的"改配置+重启"这条路，**没有任何台账**。

本组测试钉住的契约：
    1. 重启期变更**必须可见**（新实例读盘上基线，而不是拿当前配置当基线）；
    2. 报告里能区分 `baseline_source`：persisted（可见）vs bootstrap（盲区）；
    3. JSON 往返不得产生**假漂移**（list/tuple 归一化）；
    4. 基线损坏 → 退回「首见即基线」且不留静默；
    5. repin 后漂移消失且跨重启成立。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace as NS

from anima.core.config_drift import ConfigDriftMonitor


def _cfg(tmp_path, **over) -> NS:
    """最小 config 形状，只含 `_SENSITIVE_FIELDS` 覆盖到的路径。"""
    sections = {
        "persistence": NS(data_dir=str(tmp_path)),
        "permissions": NS(owners=("1",), admins=(), banned=(), readonly=(),
                          banned_groups=(), readonly_groups=()),
        "security": NS(require_approval_for_danger=True, approval_ttl_sec=300),
        "capability_layer": NS(capability_mode=False, capability_gateway_mode=False,
                               python_exec_enabled=False, pilot_capability_allowlist=(),
                               governance_audit_enabled=False, health_evidence_enabled=False,
                               transition_audit_enabled=False),
        "danger": NS(enabled=False, core_mutation=False, core_mutation_confirm=False,
                     memory_infection=False, stance_propagation=False),
        "self_code": NS(enabled=False, auto_install=False),
        "webui": NS(enabled=True, host="127.0.0.1", port=8443),
        "sylann": NS(mode="off"),
        "iteration": NS(auto_repair=False),
    }
    for section, values in over.items():
        for key, val in values.items():
            setattr(sections[section], key, val)
    return NS(**sections)


def _baseline_file(tmp_path):
    return tmp_path / "config_drift_baseline.json"


# --------------------------------------------------------------------------
# 1) 核心：重启期变更可见
# --------------------------------------------------------------------------
def test_restart_time_change_is_visible_after_fix(tmp_path):
    """修复的核心目标：改配置 → 重启（新实例）→ 必须报漂移。"""
    m1 = ConfigDriftMonitor(_cfg(tmp_path), actor="bootstrap")
    assert m1.check(_cfg(tmp_path))["drift_detected"] is False
    assert m1._baseline_source == "bootstrap", "首次运行应为 bootstrap 并落盘"
    assert _baseline_file(tmp_path).exists(), "首见即基线必须落盘"

    # 模拟「改 config.yaml 后 systemctl restart」：新进程 + 新配置
    cfg2 = _cfg(tmp_path, capability_layer={"capability_mode": True})
    m2 = ConfigDriftMonitor(cfg2, actor="bootstrap")
    rep = m2.check(cfg2)

    assert rep["baseline_source"] == "persisted", "重启后基线应来自磁盘"
    assert rep["drift_detected"] is True, "重启期变更被漏检 —— 这正是 D-08 的盲区"
    fields = {i["field"] for i in rep["items"]}
    assert "capability_mode" in fields
    assert [d["field"] for d in rep["dangerous"]] == ["capability_mode"], \
        "False→True 翻转应判 danger"


def test_restart_without_change_reports_nothing(tmp_path):
    """反向守卫：没改配置的重启不应产生假漂移（防噪音）。"""
    ConfigDriftMonitor(_cfg(tmp_path))
    m2 = ConfigDriftMonitor(_cfg(tmp_path))
    rep = m2.check(_cfg(tmp_path))
    assert rep["drift_detected"] is False, f"假漂移: {rep['items']}"


# --------------------------------------------------------------------------
# 2) JSON 往返不得产生假漂移
# --------------------------------------------------------------------------
def test_list_fields_roundtrip_without_false_drift(tmp_path):
    """list/tuple 归一化：不修 `_canon`，落盘的 tuple 读回成 list 会**永久**报漂移。"""
    cfg = _cfg(tmp_path, permissions={"owners": ("9", "8"), "admins": ("5",)})
    ConfigDriftMonitor(cfg)
    saved = json.loads(_baseline_file(tmp_path).read_text(encoding="utf-8"))
    assert saved["baseline"]["owners"] == ["8", "9"], "落盘应为排序后的 list"

    m2 = ConfigDriftMonitor(_cfg(tmp_path, permissions={"owners": ("9", "8"), "admins": ("5",)}))
    assert m2.check(_cfg(tmp_path, permissions={"owners": ("9", "8"), "admins": ("5",)}))["drift_detected"] is False


def test_owner_removal_is_still_danger(tmp_path):
    """权限面语义不受持久化影响：owners 被清空仍判 danger。"""
    ConfigDriftMonitor(_cfg(tmp_path, permissions={"owners": ("1",)}))
    cfg2 = _cfg(tmp_path, permissions={"owners": ()})
    rep = ConfigDriftMonitor(cfg2).check(cfg2)
    assert rep["drift_detected"] and [d["field"] for d in rep["dangerous"]] == ["owners"]


# --------------------------------------------------------------------------
# 3) 损坏容错不留静默
# --------------------------------------------------------------------------
def test_corrupt_baseline_falls_back_to_bootstrap_with_warning(tmp_path, caplog):
    _baseline_file(tmp_path).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="anima.core.config_drift"):
        m = ConfigDriftMonitor(_cfg(tmp_path))
    assert m._baseline_source == "bootstrap"
    assert m.check(_cfg(tmp_path))["drift_detected"] is False
    assert any("漂移基线读取失败" in r.getMessage() for r in caplog.records), \
        "损坏时应留 warning（不得静默）"


def test_wrong_shape_baseline_is_rejected(tmp_path, caplog):
    _baseline_file(tmp_path).write_text(json.dumps({"baseline": {}}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="anima.core.config_drift"):
        m = ConfigDriftMonitor(_cfg(tmp_path))
    assert m._baseline_source == "bootstrap"
    assert any("形状异常" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# 4) repin 的持久性
# --------------------------------------------------------------------------
def test_repin_persists_across_restart(tmp_path):
    """owner repin 之后重启，不应把旧基线又捡回来。"""
    ConfigDriftMonitor(_cfg(tmp_path))
    cfg2 = _cfg(tmp_path, danger={"enabled": True})
    m2 = ConfigDriftMonitor(cfg2)
    assert m2.check(cfg2)["drift_detected"] is True

    m2.repin(cfg2, actor="owner:1")
    assert m2._baseline_source == "repinned"
    assert m2.check(cfg2)["drift_detected"] is False

    m3 = ConfigDriftMonitor(cfg2)          # 再来一次"重启"
    assert m3._baseline_source == "persisted"
    assert m3.check(cfg2)["drift_detected"] is False, "repin 未落盘 → 旧基线被捡回"
