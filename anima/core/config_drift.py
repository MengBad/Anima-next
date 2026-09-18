"""R16 闭环4：配置 / 权限漂移检测。

生产环境中，owners / admins / 安全开关（require_approval_for_danger、
capability_layer 治理面、danger 总开关、self_code 总开关）/ 公网暴露面
（webui.host / port）属于一旦被改就会改变攻击面的敏感配置。

本模块以基线快照（baseline）为锚，周期性比对运行配置；发现漂移时
仅做只读告警（写入审计 + heartbeat 透出），绝不自动回滚——owner 经
WebUI / config change-set 的受控变更后，应显式 repin 重新校准基线。

设计原则（与 R16 其余部分一致）：
- 只读观测，不写配置、不重启、不自愈。
- 告警即证据：每次漂移都进 audit，可事后回溯何时、改了什么。
- fail-closed：基线缺失时以首次见到即视为基线处理，避免启动即误报；
  但 owner 列表为空（配置损坏）视为高危，单独告警。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.core.config_drift")

# 关注字段：权限面 + 安全开关 + 暴露面。这些值在运行中一旦变化都会改变
# 系统的可被操作范围，因此需要被持续盯防。
_SENSITIVE_FIELDS = (
    ("permissions.owners", "owners"),
    ("permissions.admins", "admins"),
    ("permissions.banned", "banned"),
    ("permissions.readonly", "readonly"),
    ("permissions.banned_groups", "banned_groups"),
    ("permissions.readonly_groups", "readonly_groups"),
    ("security.require_approval_for_danger", "require_approval_for_danger"),
    ("security.approval_ttl_sec", "approval_ttl_sec"),
    ("capability_layer.capability_mode", "capability_mode"),
    ("capability_layer.capability_gateway_mode", "capability_gateway_mode"),
    ("capability_layer.python_exec_enabled", "python_exec_enabled"),
    ("capability_layer.pilot_capability_allowlist", "pilot_capability_allowlist"),
    ("capability_layer.governance_audit_enabled", "governance_audit_enabled"),
    ("capability_layer.health_evidence_enabled", "health_evidence_enabled"),
    ("capability_layer.transition_audit_enabled", "transition_audit_enabled"),
    ("danger.enabled", "danger_enabled"),
    ("danger.core_mutation", "danger_core_mutation"),
    ("danger.core_mutation_confirm", "danger_core_mutation_confirm"),
    ("danger.memory_infection", "danger_memory_infection"),
    ("danger.stance_propagation", "danger_stance_propagation"),
    ("self_code.enabled", "self_code_enabled"),
    ("self_code.auto_install", "self_code_auto_install"),
    ("webui.enabled", "webui_enabled"),
    ("webui.host", "webui_host"),
    ("webui.port", "webui_port"),
    ("sylann.mode", "sylann_mode"),
    ("iteration.auto_repair", "iteration_auto_repair"),
)


def _get_path(cfg: Any, dotted: str) -> Any:
    """从 pydantic 配置按点路径取值；列表/集合统一为排序 tuple 稳定比较。"""
    node: Any = cfg
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    if isinstance(node, (list, set, tuple)):
        return tuple(sorted(str(x) for x in node))
    if isinstance(node, dict):
        return {str(k): _stable(v) for k, v in node.items()}
    return node


def _stable(value: Any) -> Any:
    if isinstance(value, (list, set, tuple)):
        return tuple(sorted(str(x) for x in value))
    return value


def _default_baseline_path(cfg: Any) -> Path:
    """基线文件位置：`<persistence.data_dir>/config_drift_baseline.json`。"""
    try:
        data_dir = str(getattr(getattr(cfg, "persistence", None), "data_dir", "data") or "data")
    except Exception:  # noqa: BLE001 - 取不到就退回默认目录
        data_dir = "data"
    return Path(data_dir) / "config_drift_baseline.json"


def _canon(value: Any) -> Any:
    """把 JSON 往返后的值转回 `_get_path` 的规范形式（list→排序 tuple，dict 递归）。

    不做这一步，落盘的 `("a", "b")` 读回成 `["a", "b"]`（不相等）⇒ 比对会**永远报漂移**。
    """
    if isinstance(value, list):
        return tuple(sorted(str(x) for x in value))
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items())}
    return value


class ConfigDriftMonitor:
    """敏感配置基线比对器（基线**跨重启持久化** + 周期性比对）。

    一个实例对应一套基线；owner 经 WebUI 显式 repin 时，调用方应替换
    core.config_drift 为新实例（或在实例上调用 repin）。

    持久化（2026-09-18，债 D-08）——为什么必须落盘：
        基线原先只在内存、且由**本次启动的**配置建立 ⇒ 「手工改 `config.yaml` 然后
        `systemctl restart anima`」这条**生产上最常用的变更路径**完全绕过本监控：
        新进程把新值当基线，`config_permission_drift` 永远 0 条。落盘后基线跨重启
        续用，重启期变更会立刻以 `baseline_source="persisted"` 呈现。
        首次运行（盘上无基线）仍沿用「首见即基线」的 fail-closed 语义，并把该次
        基线落盘 —— 因此**从第二次启动起**才具备跨重启可见性。
    """

    def __init__(self, cfg: Any, actor: str = "bootstrap", baseline_path: Any = None):
        self._baseline_path: Path = (
            Path(baseline_path) if baseline_path else _default_baseline_path(cfg)
        )
        loaded = self._load_baseline()
        if loaded is not None:
            self._baseline, self._baseline_at, self._baseline_actor = loaded
            self._baseline_source = "persisted"
        else:
            self._baseline = self._snapshot(cfg)
            self._baseline_at = time.time()
            self._baseline_actor = actor
            self._baseline_source = "bootstrap"
            self._persist_baseline()
        # 漂移告警去重签名保持**内存**：重启后同一漂移最多再报一次，这是刻意的
        # （重启正是「配置可能被改过」的信号，值得再提醒一次），噪音有界。
        self._last_signature: Optional[str] = None

    def _load_baseline(self) -> Optional[tuple[dict[str, Any], float, str]]:
        """读取落盘基线；不存在/损坏 → None（调用方按「首见即基线」处理）。"""
        try:
            p = self._baseline_path
            if not p.exists():
                return None
            raw = json.loads(p.read_text(encoding="utf-8"))
            baseline = raw.get("baseline")
            if not isinstance(baseline, dict) or not baseline:
                logger.warning("漂移基线文件形状异常，按「首见即基线」处理: %s", p)
                return None
            return (
                {str(k): _canon(v) for k, v in baseline.items()},
                float(raw.get("at", 0.0) or 0.0),
                str(raw.get("actor", "unknown")),
            )
        except Exception:  # noqa: BLE001 - 基线读不了不能影响启动
            logger.warning("漂移基线读取失败，按「首见即基线」处理", exc_info=True)
            return None

    def _persist_baseline(self) -> None:
        """原子写回基线；失败只降级为内存基线（不阻断启动）。"""
        try:
            p = self._baseline_path
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "baseline": self._baseline,
                "at": self._baseline_at,
                "actor": self._baseline_actor,
                "source": self._baseline_source,
            }
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
            )
            os.replace(tmp, p)
        except Exception:  # noqa: BLE001
            logger.warning("漂移基线落盘失败（本次仅内存基线）", exc_info=True)

    @staticmethod
    def _snapshot(cfg: Any) -> dict[str, Any]:
        return {field: _get_path(cfg, path) for path, field in _SENSITIVE_FIELDS}

    def check(self, cfg: Any) -> dict[str, Any]:
        """比对当前配置与基线，返回结构化漂移报告（不写审计）。"""
        current = self._snapshot(cfg)
        drifted: list[dict[str, Any]] = []
        for field, before in self._baseline.items():
            after = current.get(field)
            if before != after:
                severity = self._severity(field, before, after)
                drifted.append({
                    "field": field,
                    "before": before,
                    "after": after,
                    "severity": severity,
                })
        return {
            "drift_detected": bool(drifted),
            "baseline_at": self._baseline_at,
            "baseline_actor": self._baseline_actor,
            # "persisted" = 基线跨重启续用（因此本次重启期的变更是**可见**的）
            # "bootstrap" = 基线由本次启动建立（此时重启期变更不可见，属已知盲区）
            "baseline_source": self._baseline_source,
            "items": drifted,
            "dangerous": [d for d in drifted if d["severity"] == "danger"],
            "watched_fields": len(self._baseline),
        }

    def maybe_emit(self, cfg: Any, audit: Any) -> bool:
        """若发生新漂移则写一条 audit（去重）；返回是否真正写入。"""
        if audit is None:
            return False
        report = self.check(cfg)
        if not report["drift_detected"]:
            return False
        sig = repr(sorted((d["field"], d["before"], d["after"])
                         for d in report["items"]))
        if sig == self._last_signature:
            return False
        self._last_signature = sig
        audit.emit(
            "config_permission_drift",
            baseline_actor=self._baseline_actor,
            baseline_at=report["baseline_at"],
            items=report["items"],
            dangerous=[d["field"] for d in report["dangerous"]],
            note="敏感配置/权限较基线发生漂移；若为主动变更请经 WebUI 重新校准基线",
        )
        return True

    def repin(self, cfg: Any, actor: str) -> None:
        """在受控变更后重新校准基线（并落盘，使校准跨重启成立）。"""
        self._baseline = self._snapshot(cfg)
        self._baseline_at = time.time()
        self._baseline_actor = actor
        self._baseline_source = "repinned"
        self._last_signature = None
        self._persist_baseline()

    @staticmethod
    def _severity(field: str, before: Any, after: Any) -> str:
        danger_on_relax = {
            "require_approval_for_danger": (True, False),
            "capability_mode": (False, True),
            "capability_gateway_mode": (False, True),
            "python_exec_enabled": (False, True),
            "danger_enabled": (False, True),
            "danger_core_mutation": (False, True),
            "danger_memory_infection": (False, True),
            "danger_stance_propagation": (False, True),
            "self_code_enabled": (False, True),
            "self_code_auto_install": (False, True),
            "iteration_auto_repair": (False, True),
            "webui_enabled": (False, True),
        }
        if field in danger_on_relax and (before, after) == danger_on_relax[field]:
            return "danger"
        if field == "webui_host" and after not in (None, "127.0.0.1", "localhost"):
            return "danger"
        if field in ("owners", "admins") and len(after or ()) > len(before or ()):
            return "warning"
        if field in ("owners", "admins") and not (after or ()):
            return "danger"
        return "warning"

