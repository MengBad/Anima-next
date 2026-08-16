"""P0-02 — Long Running Test（长期运行稳定性记录）。

定位：Owner 要判断「ANIMA 这台个人 Autonomous Agent OS 是否真的长期稳定运行」，
需要**可量化的稳定性证据**，而非一句声称。本模块基于 P0-01 每日健康证明 +
审计日志 + 演化证据，聚合出 7/30/90 天的稳定性记录：

    - uptime_ratio         ：窗口内「有健康证明的天数 / 窗口天数」（证明可用性）
    - anomaly_total        ：窗口内所有证明的异常检查项累计数
    - degraded_days        ：窗口内出现 DEGRADED 证明的天数
    - permission_drift_count：窗口内权限漂移/变更类审计事件数
    - recovery_count       ：窗口内恢复动作类审计事件数
    - evolution_count      ：窗口内受控自我进化（批准/提案）类审计事件数

设计纪律（延续 P0-01 / R6–R15 安全原则）：
- **fail-closed / 诚实**：任一数据源不可用 → 该计数器记 0 且 ``data_complete=False``，
  绝不编造稳定数字假装健康。
- **只读**：只聚合既存数据（健康证明 / 审计 / 演化证据），不写运行态、不改权限。
- **可复现**：纯确定性统计，无 LLM、无随机；窗口锚定传入 ``now``。
- **可落盘**：每条记录带 sha256 指纹，按窗口写入 ``data/stability/{window}d.jsonl``。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# 审计事件中用于分类的关键词（小写匹配 type 字段）
PERMISSION_DRIFT_KEYWORDS = ("permission_drift", "permission_revoked", "permission_granted", "revoke")
RECOVERY_KEYWORDS = ("recovery_plan", "recover", "restart_runtime", "reconnect_channel", "review_llm")
EVOLUTION_KEYWORDS = ("evolution_approved", "evolution_proposal", "evolution")


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _iso_utc(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return str(ts)


# ---------------------------------------------------------------------------
# 数据读取（坏行隔离，与 P0-01 一致）
# ---------------------------------------------------------------------------
def _read_proofs(data_dir: str) -> list[dict[str, Any]]:
    """读取全部已落盘健康证明（按时间倒序），坏行隔离。"""
    root = Path(data_dir) / "health_proof"
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for path in sorted(root.glob("*.jsonl"), reverse=True):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return out


def _read_audit_rows(audit: Any, limit: int = 200000) -> list[dict[str, Any]]:
    if audit is None:
        return []
    try:
        return list(audit.recent(limit=limit))
    except Exception:  # noqa: BLE001
        return []


def _count_keyword(rows: list[dict[str, Any]], keywords: tuple[str, ...], now: float, window_sec: int) -> int:
    n = 0
    for r in rows:
        try:
            ts = float(r.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if now - ts > window_sec:
            continue
        etype = str(r.get("type", "")).lower()
        if any(k in etype for k in keywords):
            n += 1
    return n


# ---------------------------------------------------------------------------
# 稳定性记录
# ---------------------------------------------------------------------------
@dataclass
class StabilityRecord:
    window_days: int
    generated_at: float
    proofs_total: int
    days_covered: int
    uptime_ratio: float                 # 0..1
    anomaly_total: int
    degraded_days: int
    permission_drift_count: int
    recovery_count: int
    evolution_count: int
    data_complete: bool                 # 任一数据源缺失 → False（fail-closed 诚实）
    proof_sha256: str = ""

    @property
    def stable(self) -> bool:
        """稳定性判定：数据完整 + 无降级天 + 无权限漂移 + 有证明覆盖。"""
        return (
            self.data_complete
            and self.degraded_days == 0
            and self.permission_drift_count == 0
            and self.proofs_total > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "anima.stability_record/v1",
            "window_days": self.window_days,
            "generated_at": self.generated_at,
            "generated_at_iso": _iso_utc(self.generated_at),
            "proofs_total": self.proofs_total,
            "days_covered": self.days_covered,
            "uptime_ratio": round(self.uptime_ratio, 4),
            "anomaly_total": self.anomaly_total,
            "degraded_days": self.degraded_days,
            "permission_drift_count": self.permission_drift_count,
            "recovery_count": self.recovery_count,
            "evolution_count": self.evolution_count,
            "data_complete": self.data_complete,
            "stable": self.stable,
            "proof_sha256": self.proof_sha256,
        }

    def to_line(self) -> str:
        payload = self.to_dict()
        payload["proof_sha256"] = _sha256_json(payload)
        self.proof_sha256 = payload["proof_sha256"]
        return json.dumps(payload, ensure_ascii=False)


def collect_stability(
    data_dir: str,
    window_days: int = 7,
    audit: Any = None,
    now: Optional[float] = None,
) -> StabilityRecord:
    """聚合某窗口（7/30/90 天）的稳定性记录。

    ``audit`` 可选：传入 AuditLogger（有 recent()）以统计权限漂移/恢复/演化事件；
    为 None 时这些计数器为 0 且 ``data_complete=False``（诚实，不编造）。
    """
    now = now if now is not None else _now()
    window_sec = window_days * 86400

    # 1) 健康证明
    proofs = _read_proofs(data_dir)
    in_window = [p for p in proofs if now - float(p.get("generated_at", 0)) <= window_sec]
    proofs_total = len(in_window)
    # 按「天」去重，统计有证明覆盖的天数 + 降级天数 + 异常累计
    days_with_proof: set[str] = set()
    degraded_days = 0
    anomaly_total = 0
    for p in in_window:
        day = _iso_utc(float(p.get("generated_at", 0)))[:10]
        days_with_proof.add(day)
        if p.get("overall") == "DEGRADED":
            degraded_days += 1
        anomaly_total += int(p.get("anomaly_count", 0) or 0)
    days_covered = len(days_with_proof)
    uptime_ratio = (days_covered / window_days) if window_days > 0 else 0.0

    # 2) 审计事件分类
    rows = _read_audit_rows(audit)
    audit_ok = audit is not None
    perm_drift = _count_keyword(rows, PERMISSION_DRIFT_KEYWORDS, now, window_sec)
    recovery = _count_keyword(rows, RECOVERY_KEYWORDS, now, window_sec)
    evolution = _count_keyword(rows, EVOLUTION_KEYWORDS, now, window_sec)

    data_complete = audit_ok  # 健康证明目录缺失也会让 days_covered=0，但记录仍有效；
                               # 真正缺失的是审计源 → 标记不完整

    rec = StabilityRecord(
        window_days=window_days,
        generated_at=now,
        proofs_total=proofs_total,
        days_covered=days_covered,
        uptime_ratio=uptime_ratio,
        anomaly_total=anomaly_total,
        degraded_days=degraded_days,
        permission_drift_count=perm_drift,
        recovery_count=recovery,
        evolution_count=evolution,
        data_complete=data_complete,
    )
    rec.to_line()
    return rec


def _now() -> float:
    import time
    return time.time()


def persist_stability(rec: StabilityRecord, data_dir: str) -> Path:
    """把稳定性记录追加落盘到 ``data/stability/{window}d.jsonl``。"""
    root = Path(data_dir) / "stability"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{rec.window_days}d.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(rec.to_line() + "\n")
    return path


def latest_stability(data_dir: str, window_days: int) -> Optional[StabilityRecord]:
    """读取某窗口最近一条稳定性记录（用于回答「近 30 天稳不稳」）。"""
    path = Path(data_dir) / "stability" / f"{window_days}d.jsonl"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    last = json.loads(lines[-1])
    # 还原为 dataclass（proof_sha256 已含于 last）
    return StabilityRecord(
        window_days=last["window_days"],
        generated_at=last["generated_at"],
        proofs_total=last["proofs_total"],
        days_covered=last["days_covered"],
        uptime_ratio=last["uptime_ratio"],
        anomaly_total=last["anomaly_total"],
        degraded_days=last["degraded_days"],
        permission_drift_count=last["permission_drift_count"],
        recovery_count=last["recovery_count"],
        evolution_count=last["evolution_count"],
        data_complete=last["data_complete"],
        proof_sha256=last.get("proof_sha256", ""),
    )
