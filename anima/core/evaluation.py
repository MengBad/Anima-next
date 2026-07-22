"""只读运行验收：用可验证的运行事实代替不透明的“健康分”。"""

from __future__ import annotations

import time
from typing import Any


class RuntimeEvaluator:
    """Build a compact, content-free acceptance report for the Owner console."""

    def __init__(self, core: Any):
        self.core = core

    @staticmethod
    def _check(check_id: str, title: str, status: str, detail: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return {"id": check_id, "title": title, "status": status, "detail": detail, "evidence": evidence}

    def evaluate(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        running = bool(getattr(self.core, "_running", False))
        checks.append(self._check(
            "runtime", "运行时", "ready" if running else "attention",
            "主进程正在运行" if running else "主进程尚未进入运行状态",
            {"running": running},
        ))

        channel = getattr(getattr(self.core, "gateway", None), "channel", None)
        try:
            channel_health = channel.health() if channel and hasattr(channel, "health") else {}
        except Exception:
            channel_health = {}
        connected = bool(channel_health.get("connected"))
        checks.append(self._check(
            "qq_channel", "QQ 通道", "ready" if connected else "attention",
            "NapCat / OneBot 连接正常" if connected else "未检测到活跃 QQ 通道",
            {"bound": channel is not None, "connected": connected},
        ))

        llm = getattr(self.core, "llm", None)
        try:
            llm_health = llm.health() if llm and hasattr(llm, "health") else {}
        except Exception:
            llm_health = {}
        llm_error = str(llm_health.get("last_error") or "")
        checks.append(self._check(
            "llm", "模型链路", "attention" if llm_error else ("ready" if llm else "unknown"),
            "最近一次模型调用出现错误：" + llm_error[:120] if llm_error else
            ("模型客户端已就绪" if llm else "尚无模型健康样本"),
            {"configured": llm is not None, "requests": int(llm_health.get("request_count", 0) or 0),
             "failures": int(llm_health.get("failure_count", 0) or 0)},
        ))

        workflows = getattr(self.core, "workflows", None)
        summary = workflows.summary() if workflows and hasattr(workflows, "summary") else {}
        active = int(summary.get("active", 0) or 0)
        checks.append(self._check(
            "automation", "自动化交付", "ready" if active == 0 else "attention",
            "没有等待送达的自动化" if active == 0 else f"有 {active} 项自动化正在等待送达",
            summary,
        ))

        backups = getattr(self.core, "backups", None)
        snapshots = backups.list(1) if backups and hasattr(backups, "list") else []
        latest = snapshots[0] if snapshots else None
        checks.append(self._check(
            "backup", "运行状态备份", "ready" if latest else "unknown",
            "已有可恢复的状态快照" if latest else "尚未创建状态快照",
            {"latest_at": latest.get("created_at") if latest else None, "count": len(backups.list()) if backups else 0},
        ))

        history = getattr(self.core, "health_history", None)
        meta = history.health() if history and hasattr(history, "health") else {}
        checks.append(self._check(
            "observability", "健康趋势", "ready" if meta.get("samples") else "unknown",
            f"已记录 {int(meta.get('samples', 0) or 0)} 个匿名健康样本" if meta.get("samples") else "尚未积累健康趋势样本",
            meta,
        ))
        counts = {state: sum(1 for item in checks if item["status"] == state) for state in ("ready", "attention", "unknown")}
        return {"generated_at": time.time(), "summary": counts, "checks": checks}
