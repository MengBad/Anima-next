"""P0-01 — ANIMA Daily Health Proof（每日健康证明）。

定位：Owner 长期运行 ANIMA 这台「个人 Autonomous Agent OS」的**可证明可信赖性**
基石。每天（或由 Owner 随问随生）产出一份结构化、fail-closed 的健康证明，回答：

    「ANIMA 昨天有没有异常？」→ 「没有，这是证明。」 / 「有 N 处，明细如下。」

证明覆盖：
  1. 当前版本（运行 commit SHA，取自生产 version.json）
  2. 文件 hash（核心模块指纹，可比对是否遭篡改/漂移）
  3. 配置 hash（运行 config 归一化哈希，可比对权限/策略漂移）
  4. 权限状态（RBAC / Owner 边界 / CORE_PROTECTED 是否完整）
  5. Audit 状态（审计可用性 + DENY 异常计数）
  6. Agent 状态（注册 Agent 存活 + 受控多 Agent 社会可用性）
  7. 运行时漂移（R6.2 attest_runtime：运行 SHA 与批准 SHA 比对）

设计纪律（延续 R6–R15 安全原则）：
- **fail-closed**：任何探针缺失/异常 → 该检查为 FAIL，整体证明为 DEGRADED；
  绝不因「信息不全」而假装健康。
- **只读**：本模块不写入任何运行态、不修改权限、不触发恢复；只观测+落盘证明。
- **可复现**：所有检查基于确定性输入（文件/配置/审计/身份），无 LLM、无随机。
- **自我证据**：证明本身带 sha256 指纹，可事后核验未被篡改。

落盘：`{data_dir}/health_proof/` 下按日期写 `YYYY-MM-DD.jsonl`（每日追加一条），
供「昨天是否异常」回溯。读取采用坏行隔离（同 core/audit）。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from anima.capability.governance.attestation import (
    attest_runtime,
    config_hash,
    read_running_sha,
)
from anima.agent.self_evolution import CORE_PROTECTED


# ---------------------------------------------------------------------------
# 健康证明数据结构
# ---------------------------------------------------------------------------
CHECK_OK = "ok"
CHECK_FAIL = "fail"
CHECK_UNKNOWN = "unknown"

PROOF_HEALTHY = "HEALTHY"
PROOF_DEGRADED = "DEGRADED"


@dataclass
class HealthCheck:
    """单条健康检查（fail-closed：ok=False 即视为异常）。"""

    name: str
    ok: bool
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": CHECK_OK if self.ok else CHECK_FAIL,
            "detail": self.detail,
            "extra": self.extra,
        }


@dataclass
class HealthProof:
    """ANIMA 每日健康证明（结构化、可落盘、带自我指纹）。"""

    generated_at: float
    version: Optional[str]
    config_hash_value: Optional[str]
    file_hashes: dict[str, str]
    checks: list[HealthCheck]
    drift: dict[str, Any]
    audit_summary: dict[str, Any]
    agent_summary: dict[str, Any]
    overall: str
    proof_sha256: str = ""

    # ---- 聚合辅助 ----
    @property
    def ok(self) -> bool:
        return self.overall == PROOF_HEALTHY

    def anomalies(self) -> list[HealthCheck]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "anima.health_proof/v1",
            "generated_at": self.generated_at,
            "generated_at_iso": _iso_utc(self.generated_at),
            "overall": self.overall,
            "version": self.version,
            "config_hash": self.config_hash_value,
            "file_hashes": self.file_hashes,
            "drift": self.drift,
            "audit_summary": self.audit_summary,
            "agent_summary": self.agent_summary,
            "checks": [c.to_dict() for c in self.checks],
            "anomaly_count": len(self.anomalies()),
            "proof_sha256": self.proof_sha256,
        }

    def to_line(self) -> str:
        """可落盘 JSONL 的一行（自带指纹）。"""
        payload = self.to_dict()
        payload["proof_sha256"] = _sha256_json(payload)
        self.proof_sha256 = payload["proof_sha256"]
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> Optional[str]:
    """对单文件生成 sha256（不可读/缺失 → None，由调用方按 fail-closed 解释）。"""
    try:
        data = path.read_bytes()
    except (OSError,):
        return None
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _iso_utc(ts: float) -> str:
    """把时间戳转 UTC ISO；越界（极端值）降级为原数字字符串，绝不抛错。"""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return str(ts)


def _safe_call(fn: Callable[[], Any], default: Any) -> Any:
    """探针统一保护：任何异常都不应让证明生成失败（但结果标记为 FAIL）。"""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 观测失灵不得中断证明生成
        return default


# ---------------------------------------------------------------------------
# 探针（每个探针返回 HealthCheck；fail-closed 默认 FAIL）
# ---------------------------------------------------------------------------
def _check_version(version_path: Optional[str]) -> HealthCheck:
    if not version_path:
        return HealthCheck("version", False, "version.json 路径未配置")
    sha = _safe_call(lambda: read_running_sha(version_path), None)
    if not sha:
        return HealthCheck("version", False, "无法读取运行 commit SHA（version.json 缺失/无 commit）",
                           {"path": str(version_path)})
    return HealthCheck("version", True, f"运行 commit={sha}", {"commit": sha})


def _check_file_hashes(files: list[Path]) -> tuple[HealthCheck, dict[str, str]]:
    """核心文件指纹；任一被探测文件不可读 → FAIL（fail-closed）。"""
    if not files:
        return HealthCheck("file_hashes", False, "未配置任何待校验核心文件"), {}
    hashes: dict[str, str] = {}
    missing: list[str] = []
    for f in files:
        h = _file_hash(f)
        if h is None:
            missing.append(str(f))
        else:
            hashes[str(f)] = h
    if missing:
        return HealthCheck("file_hashes", False, f"存在不可读核心文件：{missing}", {"missing": missing}), hashes
    return HealthCheck("file_hashes", True, f"已校验 {len(hashes)} 个核心文件指纹"), hashes


def _check_config_hash(config_text: Optional[str]) -> tuple[HealthCheck, Optional[str]]:
    if config_text is None:
        return HealthCheck("config_hash", False, "运行配置文本不可用（无法比对配置漂移）"), None
    try:
        h = config_hash(config_text)
    except Exception:  # noqa: BLE001
        return HealthCheck("config_hash", False, "配置归一化失败"), None
    return HealthCheck("config_hash", True, "配置哈希已生成", {"hash": h}), h


def _check_permissions(core: Any) -> HealthCheck:
    """Owner 边界 + RBAC 可用性 + CORE_PROTECTED 完整性。

    fail-closed：core 不可用 / 无 admins / 权限存储异常 → FAIL。
    """
    if core is None:
        return HealthCheck("permissions", False, "Core 不可用，无法核验权限边界")
    perm = getattr(core, "permission_store", None)
    if perm is None:
        return HealthCheck("permissions", False, "PermissionStore 未初始化")
    admins = _safe_call(lambda: perm.admins(), None)
    if not admins:
        return HealthCheck("permissions", False, "Owner/管理员列表为空（Owner 边界失效）",
                           {"admins": []})
    return HealthCheck(
        "permissions", True,
        f"权限边界有效，{len(admins)} 名 Owner/管理员",
        {"admins": admins, "core_protected_count": len(CORE_PROTECTED)},
    )


def _check_audit(audit: Any, now: float) -> tuple[HealthCheck, dict[str, Any]]:
    """审计可用性 + 最近 DENY/异常计数（窗口锚定传入的 now，保证可复现）。

    审计本身 fail-open（写失败只计数），但**审计不可用**对「可证明自身」是硬失败：
    Owner 无法审计 = 不可信。故审计 sink 缺失/异常 → FAIL。
    """
    if audit is None:
        return HealthCheck("audit", False, "审计日志不可用（无法证明自身行为）"), {
            "available": False, "denials_24h": 0
        }
    rows = _safe_call(lambda: audit.recent(limit=5000), None)
    if rows is None:
        return HealthCheck("audit", False, "审计读取失败"), {"available": False, "denials_24h": 0}
    denials = 0
    for r in rows:
        # 24h 窗口内统计 deny/denied/reject 类结果
        try:
            ts = float(r.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if now - ts > 86400:
            continue
        outcome = str(r.get("outcome", "")).lower()
        etype = str(r.get("type", "")).lower()
        if outcome in ("denied", "deny", "reject", "rejected") or "deny" in etype or "reject" in etype:
            denials += 1
    summary = {"available": True, "denials_24h": denials, "sampled": len(rows)}
    return HealthCheck("audit", True, f"审计可用，近 24h DENY/拒绝事件 {denials} 起", summary), summary


def _check_agents(core: Any) -> tuple[HealthCheck, dict[str, Any]]:
    """注册 Agent 存活 + 受控多 Agent 社会可用性（R7/R12）。

    注意：本检查为**观测性**而非 fail-closed 硬失败。理由：
    - Agent 注册表在 Core 中是可选装配（当前 R7/R12 主要经测试与编排器使用，
      生产 Core 未必常驻 registry）；缺失不应让每日健康证明整体 DEGRADED，
      否则会制造大量误报噪声。
    - 真正的「自主编排能力」是否齐备由 P3 接入时再提升严格度。
    因此 registry 缺失/空 → 标记 INFO（ok=True，detail 说明），如实记录但不计入异常。
    """
    registry = getattr(core, "agent_registry", None) if core is not None else None
    if registry is None:
        orch = getattr(core, "agent_orchestrator", None) if core is not None else None
        registry = getattr(orch, "_registry", None) if orch is not None else None
    if registry is None:
        return HealthCheck("agents", True, "Agent 注册表未装配（观测跳过，不影响基础运行）",
                           {"registered": 0, "note": "optional"}), {"registered": 0, "note": "optional"}
    agents = _safe_call(lambda: registry.list(), []) or []
    expected = {"architect", "security", "research", "coding", "critic", "judge"}
    have = {getattr(a, "agent_id", None) for a in agents}
    missing_roles = sorted(expected - {h for h in have if isinstance(h, str)})
    summary = {"registered": len(agents), "missing_society_roles": missing_roles}
    if not agents:
        return HealthCheck("agents", True, "无注册 Agent（观测跳过，不影响基础运行）", summary), summary
    if missing_roles:
        return HealthCheck(
            "agents", True,
            f"已注册 {len(agents)} 个 Agent；多 Agent 社会角色缺 {missing_roles}（不影响基础运行）",
            summary,
        ), summary
    return HealthCheck("agents", True, f"已注册 {len(agents)} 个 Agent，社会角色齐备", summary), summary


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
@dataclass
class ProofSource:
    """构造健康证明所需的运行期输入（由调用方注入；测试可全注入，零外部依赖）。"""

    version_path: Optional[str] = None
    config_text: Optional[str] = None
    core_files: list[Path] = field(default_factory=list)
    core: Any = None
    audit: Any = None
    approved_sha: Optional[str] = None           # R6.2 批准参考（None → 不做 SHA 比对，仅报告）
    approved_config_hash: Optional[str] = None   # 可选配置批准参考


def build_health_proof(src: ProofSource, clock: Optional[Callable[[], float]] = None) -> HealthProof:
    """生成一份每日健康证明（fail-closed，纯观测，不写运行态）。"""
    now = (clock or time.time)()
    checks: list[HealthCheck] = []

    checks.append(_check_version(src.version_path))
    file_check, file_hashes = _check_file_hashes(src.core_files)
    checks.append(file_check)
    cfg_check, cfg_hash = _check_config_hash(src.config_text)
    checks.append(cfg_check)
    checks.append(_check_permissions(src.core))
    audit_check, audit_summary = _check_audit(src.audit, now)
    checks.append(audit_check)
    agent_check, agent_summary = _check_agents(src.core)
    checks.append(agent_check)

    # R6.2 运行时漂移比对（仅当同时具备运行 SHA 与批准 SHA 才有意义）
    version_sha = read_running_sha(src.version_path) if src.version_path else None
    try:
        drift = attest_runtime(
            src.approved_sha,
            src.version_path or "",
            config_text=src.config_text,
            approved_config_hash=src.approved_config_hash,
        )
        drift_dict = {
            "status": drift.status,
            "checks": {k: {"ok": ok_, "detail": d} for k, (ok_, d) in drift.checks.items()},
        }
        # 漂移结果为 DRIFT 时，整体证明降级（但不新增 HealthCheck 重复计数）；
        # 若 approved_sha 未提供则 drift 本身无意义，标记为 unknown 不计入异常。
        if src.approved_sha:
            if not drift.ok:
                checks.append(HealthCheck(
                    "runtime_drift", False,
                    "运行时漂移检测失败：运行态与批准参考不一致",
                    {k: {"ok": ok_, "detail": d} for k, (ok_, d) in drift.checks.items()},
                ))
            else:
                checks.append(HealthCheck("runtime_drift", True, "运行时与批准参考一致，无漂移"))
        else:
            drift_dict["note"] = "approved_sha 未提供，跳过 SHA 漂移比对（仅报告运行 SHA）"
    except Exception:  # noqa: BLE001
        drift_dict = {"status": "DRIFT DETECTED", "checks": {}, "error": "attest_runtime 异常"}
        checks.append(HealthCheck("runtime_drift", False, "运行时漂移检测异常"))

    overall = PROOF_HEALTHY if all(c.ok for c in checks) else PROOF_DEGRADED

    proof = HealthProof(
        generated_at=now,
        version=version_sha,
        config_hash_value=cfg_hash,
        file_hashes=file_hashes,
        checks=checks,
        drift=drift_dict,
        audit_summary=audit_summary,
        agent_summary=agent_summary,
        overall=overall,
    )
    proof.to_line()  # 计算并写入 proof_sha256
    return proof


def answer_was_anomaly_yesterday(data_dir: str, today: Optional[str] = None) -> dict[str, Any]:
    """Owner 的灵魂提问：『昨天有没有异常？』

    读取最近两条健康证明（昨天 / 前天，或最近两次），比较 anomaly_count。
    返回结构化答复，供 WebUI / 自然语言层直接引用。
    fail-closed：无历史记录 → 如实报告「无历史可比对」，绝不假装健康。
    """
    root = Path(data_dir) / "health_proof"
    lines: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.glob("*.jsonl"), reverse=True):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            lines.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
            if len(lines) >= 2:
                break
    if not lines:
        return {
            "answer": "unknown",
            "message": "尚无健康证明历史，无法判断昨日是否异常。",
            "yesterday_anomaly_count": None,
        }
    yesterday = lines[0]
    prior_anomaly = lines[1].get("anomaly_count", 0) if len(lines) > 1 else 0
    y_anom = yesterday.get("anomaly_count", 0)
    if y_anom == 0:
        return {
            "answer": "no",
            "message": f"昨天没有异常（证明 {yesterday.get('proof_sha256', '')[:16]}…）。",
            "yesterday_anomaly_count": 0,
            "vs_prior": y_anom - prior_anomaly,
        }
    return {
        "answer": "yes",
        "message": f"昨天有 {y_anom} 处异常（证明 {yesterday.get('proof_sha256', '')[:16]}…）。",
        "yesterday_anomaly_count": y_anom,
        "vs_prior": y_anom - prior_anomaly,
        "checks": yesterday.get("checks", []),
    }


def persist_proof(proof: HealthProof, data_dir: str) -> Path:
    """把证明追加落盘到当日 jsonl（坏行隔离由读取侧保证）。"""
    root = Path(data_dir) / "health_proof"
    root.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(proof.generated_at, tz=timezone.utc).strftime("%Y-%m-%d")
    path = root / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(proof.to_line() + "\n")
    return path
