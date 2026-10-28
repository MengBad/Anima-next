"""R14 —— Human-AI OS 控制面（Control Surface）。

面向 Owner 的「人机协同操作系统」控制面：**聚合** ANIMA 作为 Autonomous OS 的全景
（健康 / 演化提案 / 受控恢复计划 / 观测事件），并解析 Owner 的**受限自然语言指令**。

设计纪律（延续 R6–R14 受控原则）：
- 本模块是**控制面 / 聚合层**，**不持有任何新的执行权限**。
- 指令解析器基于**白名单 + 确定性规则**（无 LLM、可复现、可审计），只识别已知意图；
  未识别指令一律返回 ``unknown``，绝不猜测执行。
- 只读类指令（查看健康 / 列出提案）可直接返回数据；**特权类指令（批准/拒绝/恢复）只产出
  「待确认动作」**，指明应走的既有 Owner 授权端点（R9 / R13），由 Owner 在既有通道确认，
  R14 绝不自我授权（self-improvement ≠ self-authorization）。
- Observation → Evaluation → Evolution 的既有闭环在此只做**可视化聚合**，不改变其行为。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("anima.os.control_surface")


@dataclass
class OSOverview:
    """Autonomous OS 全景快照（聚合层，只读）。"""

    health: dict[str, Any] = field(default_factory=dict)
    recovery_plans: list[dict[str, Any]] = field(default_factory=list)
    evolution_proposals: list[dict[str, Any]] = field(default_factory=list)
    observation_events: int = 0
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "recovery_plans": self.recovery_plans,
            "evolution_proposals": self.evolution_proposals,
            "observation_events": self.observation_events,
            "degraded": self.degraded,
        }


@dataclass
class ParsedCommand:
    """一条被解析的 Owner 指令（受控、可审计）。"""

    intent: str              # health | list_proposals | approve | reject | recover | unknown
    args: dict[str, Any] = field(default_factory=dict)
    needs_confirmation: bool = False  # True → 须走既有 Owner 授权端点确认
    confirmation_endpoint: str = ""    # 既有授权端点（仅描述，不调用）
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "args": self.args,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_endpoint": self.confirmation_endpoint,
            "preview": self.preview,
        }


# ---------------------------------------------------------------------------
# 只读结果 → QQ 短文本（P3-3）
# ---------------------------------------------------------------------------
_MAX_LINES = 14

#: 🔴 2026-10-28：需要**即时**私聊 owner 的告警类型（D-41 扩展）。
#:
#: 判据：不处理会「继续恶化」或「已经影响功能」的。
#: 其余归 normal（进队列，随心跳发，不打断 owner）。
#:
#: 加新类型前先问：这条晚 30 秒知道有关系吗？没关系就别放这儿 ——
#: 告警发太勤会让 owner 关掉通知，那所有主动交付的价值都归零。
_URGENT_ALERT_TYPES: frozenset[str] = frozenset({
    "permission_drift",     # 权限漂移：治理面被改
    "outbound_failures",    # QQ 出站失败：用户收不到消息
    "llm_failures",         # LLM 持续失败：它答不了话
    "circuit_open",         # 熔断打开：已停止重试
    "data_corruption",      # 数据损坏：状态可能已坏
})


def _health_topic_key(text: str) -> str:
    """把 health_alerts 标题归一成主题键（剥掉计数与标点）。

    🔴 2026-10-22：`detect_alerts` 的消息带具体计数
    （「窗口内检测到 9 次权限漂移事件」），数字一变标题就不同 ⇒ 全等去重失效。
    归一后「同一病灶不同计数」判为同一主题。

    示例：`[health_alerts] 窗口内检测到 9 次权限漂移事件（安全关注）`
      与 `[health_alerts] 窗口内检测到 3 次权限漂移事件（安全关注）` ⇒ 同键。
    """
    import re as _re
    t = str(text or "")
    t = _re.sub(r"\d+", "#", t)
    t = _re.sub(r"[\s，。：:、（）()【】\[\]…\-—_]+", "", t)
    return t[:64]


def _flatten_readonly(obj: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """把只读聚合结果压成「键 = 值 / 键: N 项」行，容忍结构漂移。

    刻意用通用展平而不是逐字段硬编码：这些聚合结构会随迭代变化，硬编码的
    格式化函数会在字段改名后静默输出空串——那又是一种「假完整」。
    """
    if depth >= 3:
        return [f"{prefix} = …"]
    if isinstance(obj, dict):
        lines: list[str] = []
        for k, v in list(obj.items())[:_MAX_LINES]:
            lines.extend(_flatten_readonly(v, f"{prefix}{k}." if prefix else f"{k}", depth + 1))
        return lines
    if isinstance(obj, (list, tuple)):
        return [f"{prefix}: {len(obj)} 项"]
    if isinstance(obj, str) and len(obj) > 160:
        obj = obj[:160] + "…"
    return [f"{prefix} = {obj}"]


def format_readonly_reply(cmd: "ParsedCommand", data: dict[str, Any],
                          *, max_chars: int = 1200) -> str:
    """把只读意图数据渲染成 QQ 可读短文本（纯函数，可单测）。

    诚实优先：``available=False`` 直接说不可用与原因；结果被截断时明说「已截断」。
    """
    title = (getattr(cmd, "preview", "") or "").strip() or f"意图：{getattr(cmd, 'intent', '?')}"
    lines: list[str] = [title]

    # P3-1：长期记忆要走特殊渲染——把「语义检索有没有真的跑」如实说出来。
    mem = data.get("memory")
    if isinstance(mem, dict) and getattr(cmd, "intent", "") == "memory":
        if mem.get("available") is False:
            lines.append(f"记忆库不可用：{mem.get('reason', '未知原因')}")
            return "\n".join(lines)
        lines.append(f"知识 {mem.get('knowledge_count', 0)} 条 / "
                     f"行为模式 {mem.get('pattern_count', 0)} 条")
        if mem.get("query"):
            lines.append(f"查询：「{mem['query']}」")
        for k in (mem.get("knowledge_matches") or [])[:3]:
            lines.append(f"· 知识命中：{str(k.get('statement', ''))[:120]}")
        for p in (mem.get("pattern_matches") or [])[:2]:
            lines.append(f"· 模式命中：{str(p.get('pattern', ''))[:120]}")
        for s in (mem.get("semantic_matches") or [])[:3]:
            lines.append(f"· 语义命中（{s.get('similarity')}）：{str(s.get('text', ''))[:120]}")
        if mem.get("vector_used"):
            lines.append("（已做语义检索）")
        elif mem.get("vector_reason"):
            lines.append(f"⚠️ 未做语义检索：{mem['vector_reason']}")
        for w in (mem.get("recent_why") or [])[:2]:
            lines.append(f"· 设计原因：{str(w)[:120]}")

    for key, val in data.items():
        if key == "memory":
            continue
        if isinstance(val, dict) and val.get("available") is False:
            lines.append(f"{key} 不可用：{val.get('reason', '未知原因')}")
            continue
        if key == "proposals" and isinstance(val, list):
            lines.append(f"待审提案 {len(val)} 条")
            for it in val[:5]:
                if isinstance(it, dict):
                    lines.append(f"· {it.get('id', '?')} {str(it.get('title', ''))[:80]}")
            continue
        lines.extend(_flatten_readonly(val, f"{key}."))

    body = lines[:_MAX_LINES]
    if len(lines) > _MAX_LINES:
        body.append(f"…（共 {len(lines)} 行，已截断）")
    text = "\n".join(body)
    if len(text) > max_chars:
        text = text[:max_chars] + f"…（超过 {max_chars} 字，已截断）"
    return text


class HumanAIControlSurface:
    """Human-AI OS 控制面：聚合全景 + 解析受限指令。"""

    def __init__(self, core: Any = None) -> None:
        self._core = core

    # ------------------------------------------------------------------
    # 全景聚合（只读，复用既有能力）
    # ------------------------------------------------------------------
    def overview(self, *, health: dict | None = None,
                 recovery_plans: list | None = None,
                 evolution_proposals: list | None = None,
                 observation_events: int = 0) -> OSOverview:
        """聚合 Autonomous OS 全景。各字段可注入（便于测试 / 不强制依赖 core）。"""
        health = health or (self._snapshot_health() if self._core is not None else {})
        recovery = recovery_plans if recovery_plans is not None else (
            self._recovery_plans() if self._core is not None else []
        )
        props = evolution_proposals if evolution_proposals is not None else (
            self._evolution_proposals() if self._core is not None else []
        )
        degraded = bool(health.get("degraded")) or any(
            p.get("severity") in ("high", "critical") for p in recovery
        )
        return OSOverview(
            health=health, recovery_plans=recovery,
            evolution_proposals=props, observation_events=observation_events,
            degraded=degraded,
        )

    def _snapshot_health(self) -> dict[str, Any]:
        try:
            from ..agent.autonomous_operation import AutonomousOperationController
            ctrl = AutonomousOperationController()
            rep = ctrl.evaluate(self._core)
            return {"snapshot": rep.snapshot, "degraded": rep.degraded,
                    "plans": [p.to_dict() for p in rep.plans]}
        except Exception:  # noqa: BLE001 - 聚合失败不阻断
            return {"snapshot": {}, "degraded": True, "plans": []}

    def _recovery_plans(self) -> list[dict[str, Any]]:
        try:
            from ..agent.autonomous_operation import AutonomousOperationController
            ctrl = AutonomousOperationController()
            rep = ctrl.evaluate(self._core)
            return [p.to_dict() for p in rep.plans]
        except Exception:  # noqa: BLE001
            return []

    def _interaction_summary(self) -> dict[str, Any]:
        """P5 —— 只读聚合交互质量遥测（对话/工具/人格一致性/图片交付）。

        全部 fail-closed：任一源解析失败则跳过该项，绝不编造分数；交给
        build_self_evaluation 的 interaction 轴时，缺失子源会标 available:False。
        """
        out: dict[str, Any] = {}
        try:
            data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
            data_dir = getattr(data_dir, "data_dir", "data") if data_dir else "data"
            from ..cognitive.reply_quality import get_reply_quality
            q = get_reply_quality(data_dir).report()
            total = int(q.get("total_replies", 0) or 0)
            if total > 0:
                # 对话质量：有效回复占比（剔除超长/泄漏/复读/空回/不可用）
                bad = sum(int(q.get(k, 0) or 0) for k in (
                    "overlong_replies", "label_leaks", "identity_leaks",
                    "repeated_replies", "empty_should_reply", "unavailable_replies"))
                out["conversation"] = {
                    "ok_ratio": max(0.0, min(1.0, (total - bad) / total)),
                    "samples": total,
                }
                # 工具成功率
                calls = int(q.get("tool_calls", 0) or 0)
                if calls > 0:
                    fails = int(q.get("tool_failures", 0) or 0)
                    out["tools"] = {
                        "success_rate": max(0.0, min(1.0, (calls - fails) / calls)),
                        "calls": calls,
                    }
                # 人格一致性：身份/标签泄漏越少越稳定（P5 人格轴）
                leaks = int(q.get("identity_leaks", 0) or 0) + int(q.get("label_leaks", 0) or 0)
                out["persona"] = {
                    "stability": max(0.0, min(1.0, 1.0 - leaks / total)),
                    "drift_rejects": 0,
                }
        except Exception:  # noqa: BLE001 - 交互质量聚合失败不阻断
            pass
        try:
            from ..media import prepare_cache_stats
            st = prepare_cache_stats()
            hits = int(st.get("hits", 0) or 0)
            misses = int(st.get("misses", 0) or 0)
            if (hits + misses) > 0:
                out["image"] = {
                    "delivery_ok_ratio": max(0.0, min(1.0, hits / (hits + misses))),
                    "samples": hits + misses,
                }
        except Exception:  # noqa: BLE001
            pass
        return out

    def _evolution_proposals(self) -> list[dict[str, Any]]:
        try:
            from ..agent.self_evolution import list_evolution_proposals
            return list_evolution_proposals()
        except Exception:  # noqa: BLE001
            return []

    def stability_summary(self, window_days: int = 7) -> dict[str, Any]:
        """只读聚合某窗口（默认 7 天）的稳定性记录（P0-02）。

        优先返回已落盘的最新记录（latest_stability）；若无则即时聚合。
        fail-closed：任一数据源不可用 → data_complete=False，如实反映。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            from .long_running import collect_stability, latest_stability, detect_alerts
            cached = latest_stability(root, window_days)
            if cached is not None:
                rec = cached
            else:
                audit = getattr(self._core, "audit", None)
                rec = collect_stability(root, window_days=window_days, audit=audit)
            out = rec.to_dict()
            # P2-1：附上主动告警信号（漂移/降级/异常/恢复尖峰/覆盖率）
            out["alerts"] = detect_alerts(rec)
            return out
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "stability 聚合异常"}

    def _permission_drift_currently_resolved(self) -> bool:
        """当前配置是否**已经不漂移**了（与基线一致）。

        🔴 2026-10-23：用于把「历史审计计数」与「当前真实状态」分开。
        历史 11 次漂移事件可能全部已被 `repin()` 解决；此时不该继续告警。

        **fail-closed**：拿不到 monitor / 配置 / 抛异常 ⇒ 返回 **False**
        （= 当作「仍在漂移」）⇒ 保守地仍然告警。宁可多报一条待办，
        也绝不漏报真正的安全问题。
        """
        try:
            from anima.core.config_drift import ConfigDriftMonitor
            from pathlib import Path
            cfg = getattr(self._core, "config", None)
            if cfg is None:
                return False
            dd = getattr(cfg, "persistence", None)
            root = getattr(dd, "data_dir", "data") if dd else "data"
            # 必须传**绝对路径**：默认 _default_baseline_path 对相对 data_dir
            # 会解析出错位（2026-10-22 实测踩过：写到了进程 cwd 下）。
            base_path = Path(str(root)) / "config_drift_baseline.json"
            mon = ConfigDriftMonitor(cfg, actor="health_check",
                                     baseline_path=base_path)
            diff = mon.check(cfg) or {}
            return len(diff.get("items") or []) == 0
        except Exception:
            logger.debug("当前漂移状态查询失败 ⇒ 保守按「仍在漂移」处理", exc_info=True)
            return False

    def health_alerts_to_owner(self, window_days: int = 7,
                               owner_id: int = 0) -> list[dict[str, Any]]:
        """P2-1：把 health_proof 异常信号转成 owner 待办（warn 级触发）。

        只生成 owner 待办，不执行任何系统动作（通知-only）。
        无核心/无 tasks 存储/无异常时返回空列表（fail-closed）。
        """
        if self._core is None:
            return []
        tasks = getattr(self._core, "tasks", None)
        if tasks is None or not hasattr(tasks, "create"):
            return []
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            from .long_running import collect_stability, latest_stability, detect_alerts
            cached = latest_stability(root, window_days)
            rec = cached
            if rec is None:
                audit = getattr(self._core, "audit", None)
                rec = collect_stability(root, window_days=window_days, audit=audit)
            # 只对 warn 级异常生成待办；info 级仅出现在 stability_summary 的 alerts 供观测。
            made: list[dict[str, Any]] = []
            # 🔴 2026-10-22：**同主题去重**。此前每次 tick（6h 冷却）无条件
            # `tasks.create()` ⇒ 生产实证 9 条 `planned` 全是同一件事
            # （「权限漂移」计数从 3 递增到 9），owner 待办列表被同一病灶刷满，
            # 真正的待办被淹（owner 连续两天收到「9 次权限漂移（同主题共 7 条）」）。
            # 判据：消息里的数字归一后比对**未关闭**的同 kind 待办；
            # 已有同主题在办 ⇒ 不重复建单，只记日志（信号不丢，观测面仍可见）。
            _open = {"planned", "running", "awaiting_approval"}
            try:
                _existing = {
                    _health_topic_key(str(getattr(t, "request", "") or ""))
                    for t in tasks.list()
                    if getattr(t, "status", "") in _open
                    and getattr(t, "kind", "") == "diagnostic"
                }
            except Exception:
                _existing = set()
            # 🔴 2026-10-23：**漂移告警必须先查「当前是否真的在漂移」**。
            #   `detect_alerts(rec)` 数的是 **7 天窗口内的历史审计事件**，
            #   而那些事件可能是**已解决**的。生产实证：基线过期导致
            #   10-21 报了 9 次、10-22 报了 2 次；我用 `repin()` 修好基线后
            #   真漂移已归零（10-23 = 0 次），但历史 11 条仍在 7d 窗口内
            #   ⇒ 每 6 小时冷却就重建一张「11 次权限漂移」待办，
            #   归档一次、重建一次，永远清不掉（owner 看着它反复出现）。
            #
            #   修法：对 permission_drift，先用 ConfigDriftMonitor.check()
            #   查**当前**配置与基线的差异；若为 0 ⇒ 说明漂移已解决，
            #   历史计数只是陈旧事件 ⇒ **不建单**（信号仍留在审计与观测面）。
            #   fail-closed：查不到 monitor / 抛异常 ⇒ 保守地**仍然按原逻辑告警**
            #   （宁可多报一条，也不漏报真正的安全问题）。
            #   ⚠️ 这个查询**必须自带异常保护**：它若抛出，会中断整个 try 块
            #   ⇒ 所有告警（含其他类型）全部丢失 —— 那才是最坏的结果
            #   （由 test_query_failure_treated_as_still_drifting 抓出）。
            try:
                _drift_resolved = self._permission_drift_currently_resolved()
            except Exception:
                logger.debug("当前漂移状态查询异常 ⇒ 按「仍在漂移」处理", exc_info=True)
                _drift_resolved = False
            for a in detect_alerts(rec):
                if a.get("severity") != "warn":
                    continue
                if a.get("type") == "permission_drift" and _drift_resolved:
                    logger.info(
                        "[health_alerts] 权限漂移已解决（当前配置与基线一致），"
                        "历史 %s 次事件视为陈旧，不再建单", a.get("value"),
                    )
                    continue
                _req = f"[health_alerts] {a['message']}"
                _topic = _health_topic_key(_req)
                if _topic and _topic in _existing:
                    logger.info("[health_alerts] 同主题已在办，跳过重复建单：%s", _req[:60])
                    continue
                try:
                    tasks.create(
                        request=_req,
                        creator=int(owner_id or 0),
                        plan=[f"类型={a['type']}", f"值={a.get('value')}", "核查根因并处置"],
                        kind="diagnostic",
                    )
                    if _topic:
                        _existing.add(_topic)
                    made.append(a)
                    # 🔴 2026-10-28：**同步入队到 owner 私聊**（D-41 扩展）。
                    #
                    # 此前这里只 `tasks.create()` ⇒ 待办躺在 WebUI 里等人来取，
                    # 最快要等次日 09:00 的 digest（且只列前 5 条）才可能被看到。
                    # 「权限漂移 9 次」「LLM 全挂」这类告警等不到明早。
                    #
                    # 用 `enqueue()` 而不是 `await notify()`：本链是**同步**的
                    # （`detect_alerts` → `tasks.create`），await 会污染整条调用链。
                    # 入队即返回，真正的发送由心跳 `flush()` 完成（≤30s 延迟）。
                    try:
                        from anima.core.owner_notify import get_owner_notifier
                        get_owner_notifier(self._core).enqueue(
                            "urgent" if a.get("type") in _URGENT_ALERT_TYPES else "normal",
                            f"[健康告警] {a.get('message', '')}",
                            source="health_alerts",
                        )
                    except Exception:  # noqa: BLE001
                        # 通知失败绝不影响建单本身
                        logger.debug("健康告警入队失败（已隔离）", exc_info=True)
                except Exception:
                    continue
            return made
        except Exception:  # noqa: BLE001
            return []

    def environment_report(self, *, github: Any = None, performance: bool = True,
                           window_sec: int = 86400) -> dict[str, Any]:
        """只读聚合真实环境观测报告（P1）。

        复用 R10-01/本模块 ``analyze_environment``：GitHub 信号 + 性能阈值 +
        Audit DENY 增长趋势。只观测、只提议，不执行。
        fail-closed：任一源缺失 → report.incomplete=True，如实反映。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        # R17：未显式注入 github 源时，默认接入 GitHubSignalSource。
        # 该类内部按优先级路由：gh 已认证走 gh、否则回退公开 REST，
        # 故无论生产机有无 gh / 有无凭据都应注入，fail-closed 交给
        # recent_activity() 内部处理（取不到即空列表，不掩盖缺失）。
        if github is None:
            try:
                from ..observation.github_source import GitHubSignalSource
                github = GitHubSignalSource()
            except Exception:  # noqa: BLE001 - 信号源不可用不阻断聚合
                github = None
        try:
            from ..observation.environment import analyze_environment
            report = analyze_environment(
                self._core, github=github, performance=performance,
                window_sec=window_sec,
            )
            return report.to_dict()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "environment 聚合异常"}

    def self_evaluation(self, *, github: Any = None, performance: bool = True,
                        window_sec: int = 86400) -> dict[str, Any]:
        """R18 -- 只读聚合一份诚实的多轴自我评估（Self Evaluation）。

        复用本控制面既有只读能力（健康快照 / 环境观测 / 稳定性 / 护栏），
        交由 anima.observation.self_evaluation 归一化为 5 轴评分。
        只陈述、不执行：任何建议落入 recommendations，交给既有 Owner 审批门。
        fail-closed：任一信号源不可用该轴即 unknown，绝不编造分数。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        runtime_snapshot: dict[str, Any] = {}
        try:
            runtime_snapshot = self._snapshot_health()
        except Exception:  # noqa: BLE001
            runtime_snapshot = {}
        env_rep: dict[str, Any] = {}
        try:
            env_rep = self.environment_report(
                github=github, performance=performance, window_sec=window_sec)
        except Exception:  # noqa: BLE001
            env_rep = {}
        stability: dict[str, Any] = {}
        try:
            stability = {
                "7d": self.stability_summary(7),
                "30d": self.stability_summary(30),
                "90d": self.stability_summary(90),
            }
        except Exception:  # noqa: BLE001
            stability = {}
        audit_deny_trend: dict[str, Any] = {}
        config_drift: dict[str, Any] = {}
        try:
            audit = getattr(self._core, "audit", None)
            if audit is not None:
                from ..observation.environment import audit_deny_growth
                audit_deny_trend = audit_deny_growth(audit, window_sec=window_sec)
        except Exception:  # noqa: BLE001
            audit_deny_trend = {}
        try:
            cd = getattr(self._core, "config_drift", None)
            if cd is not None:
                config_drift = cd.snapshot() if hasattr(cd, "snapshot") else {}
        except Exception:  # noqa: BLE001
            config_drift = {}
        evolution: dict[str, Any] = {}
        try:
            from ..agent.self_evolution import list_evolution_proposals
            props = list_evolution_proposals()
            evolution = {
                "pending_owner_count": sum(
                    1 for p in props if p.get("status") == "pending_owner"),
                "unapproved_self_mod_count": 0,
            }
        except Exception:  # noqa: BLE001
            evolution = {}

        from ..observation.self_evaluation import build_self_evaluation
        ev = build_self_evaluation(
            runtime_snapshot=runtime_snapshot,
            environment_report=env_rep,
            stability=stability,
            audit_deny_trend=audit_deny_trend,
            config_drift=config_drift,
            evolution=evolution,
            interaction=self._interaction_summary(),
        )
        return ev.to_dict()

    def engineering_loop(self, *, observation_events: list | None = None,
                         proposed_issues: list | None = None,
                         evolution_proposals: list | None = None,
                         self_evaluation: dict | None = None) -> dict[str, Any]:
        """R19 —— 只读聚合一份自主工程闭环快照（Engineering Loop，提案闭环）。

        复用既有的观测（R10-01）、自监控提案（R8）、演化提案（R9）、自评（R18），
        归一化为「Observe → Plan → ... → Evidence」闭环状态快照。
        只陈述、只提案、绝不执行：任何可行动项落在 proposed_issues / recommendations，
        交由既有 Owner 审批门（R8/R9/R14）处理。fail-closed：信号缺失即诚实 unavailable。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        try:
            from ..observation.engineering_loop import build_engineering_loop
            loop = build_engineering_loop(
                core=self._core,
                observation_events=observation_events,
                proposed_issues=proposed_issues,
                evolution_proposals=evolution_proposals,
                self_evaluation=self_evaluation,
            )
            return loop.to_dict()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "engineering_loop 聚合异常"}

    def long_term_operation(self, *, stability: dict | None = None,
                            observed_count: int | None = None,
                            pending_owner_count: int | None = None,
                            evidence_count: int | None = None) -> dict[str, Any]:
        """R20 —— 只读聚合一份长期自主运行快照（Long-Term Operation）。

        复用既有稳定记录（P0-02）、观测闭环（R19）、演化证据（P2），产出确定性
        「继续 / 降级 / 移除」能力判定。只陈述、只建议：downgrade/remove 均落在
        capability_verdicts / recommendations，交既有 Owner 审批门，绝不自动禁用。
        fail-closed：信号缺失即诚实 unavailable。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        # 未显式注入观测/待审/证据计数时，从 R19 工程闭环取真实信号，避免默认 0 造成不诚实。
        if observed_count is None or pending_owner_count is None or evidence_count is None:
            try:
                loop = self.engineering_loop()
                if observed_count is None:
                    observed_count = int(loop.get("observed_count", 0) or 0)
                if pending_owner_count is None:
                    pending_owner_count = int(loop.get("pending_owner_count", 0) or 0)
                if evidence_count is None:
                    se = loop.get("self_evaluation") or {}
                    evo = next((a for a in se.get("axes", []) if a.get("axis") == "evolution"), {})
                    evidence_count = int((evo.get("evidence") or {}).get("evidence_count", 0) or 0)
            except Exception:  # noqa: BLE001 - 闭环不可用则回退 0，诚实降级
                observed_count = observed_count if observed_count is not None else 0
                pending_owner_count = pending_owner_count if pending_owner_count is not None else 0
                evidence_count = evidence_count if evidence_count is not None else 0
        try:
            from ..observation.long_term_operation import build_long_term_operation
            op = build_long_term_operation(
                core=self._core,
                stability=stability,
                observed_count=observed_count or 0,
                pending_owner_count=pending_owner_count or 0,
                evidence_count=evidence_count or 0,
            )
            return op.to_dict()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "long_term_operation 聚合异常"}

    def evolution_impact(self) -> dict[str, Any]:
        """只读聚合演化影响评分（P2）：基于历史 Before/After 证据语料。

        复用 ``anima.evolution.impact``：把 R9/R10-02 落盘的 evolution_evidence.jsonl
        聚合为语料级评分 + 数据驱动建议。fail-closed：无证据 → has_data=False，诚实。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            from ..evolution.impact import build_impact_report
            return build_impact_report(root).to_dict()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "evolution_impact 聚合异常"}

    def society_debate(self, question: str, context: str = "") -> dict[str, Any]:
        """只读触发默认 Agent 社会辩论 + 加权裁决（P3），返回建议性结论。

        **不授权任何执行**：仅聚合多视角意见供 Owner 参考。失败降级为不可用的诚实返回。
        """
        if not question:
            return {"available": False, "reason": "缺少议题"}
        try:
            import asyncio
            import threading
            from ..agent.agent_society import run_society_debate

            try:
                asyncio.get_running_loop()
                in_loop = True
            except RuntimeError:
                in_loop = False

            if not in_loop:
                record, verdict = asyncio.run(run_society_debate(question, context))
            else:
                # P1-A：运行中 loop 内 run_until_complete 必抛 RuntimeError（原代码被静默吞）。
                # 改用独立线程 + 专有循环同步等待结果，并在主线程重抛真实异常以留痕。
                box: dict[str, Any] = {}

                def _runner() -> None:
                    try:
                        box["res"] = asyncio.run(run_society_debate(question, context))
                    except Exception as exc:  # noqa: BLE001
                        box["exc"] = exc

                _t = threading.Thread(target=_runner, daemon=True)
                _t.start()
                _t.join()
                if "exc" in box:
                    raise box["exc"]
                record, verdict = box["res"]
            return {
                "question": question,
                "verdict": verdict.to_dict(),
                "debate": record.to_dict(),
            }
        except Exception:  # noqa: BLE001 - 辩论失败不阻断控制面
            logger.exception("society 辩论异常(留痕，不再静默)")
            return {"available": False, "reason": "society 辩论异常"}

    def _long_term_store(self):
        """取长期记忆实例（P3-1）。

        优先复用 ``core.long_term``——它由 ``AnimaCore`` 装配，并在向量记忆初始化后
        被回填 ``embedder``；只有 core 上没有时才按 ``pipeline.stages.memory`` 的同
        一约定惰性构造并挂回 core（避免同进程出现两个指向不同路径的实例）。
        """
        store = getattr(self._core, "long_term", None)
        if store is None:
            cfg = getattr(self._core, "config", None)
            data_dir = getattr(getattr(cfg, "persistence", None), "data_dir", "data")
            from ..memory.long_term import LongTermMemory

            store = LongTermMemory(str(data_dir))
            try:
                self._core.long_term = store
            except Exception:  # noqa: BLE001 - 挂不上去也不影响本次查询
                pass
        # embedder 兜底：core 可能在其他路径上注入了 vector_memory
        if getattr(store, "embedder", None) is None:
            vm = getattr(self._core, "vector_memory", None)
            emb = getattr(vm, "embedding", None) if vm is not None else None
            if emb is not None:
                try:
                    store.embedder = emb
                except Exception:  # noqa: BLE001
                    logger.debug("long_term.embedder 回填失败", exc_info=True)
        return store

    def long_term_memory(self, query: str | None = None) -> dict[str, Any]:
        """只读查询长期记忆（P4）：事件/知识/行为模式三层 + 设计原因。

        - 给定 query → 跨知识+模式关键词检索。
        - query 为空 → 返回摘要（各层计数 + 最近设计原因）。
        fail-closed：无数据 → has_data=False，诚实。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        try:
            mem = self._long_term_store()
            if query:
                return mem.query(query)
            know = mem.knowledge()
            pats = mem.patterns()
            whys = mem.why_designed()
            return {
                "has_data": bool(know or pats),
                "knowledge_count": len(know),
                "pattern_count": len(pats),
                "why_designed_count": len(whys),
                "recent_why": [w.statement for w in whys[-3:]],
            }
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "long_term_memory 聚合异常"}

    async def long_term_memory_semantic(self, query: str | None = None) -> dict[str, Any]:
        """只读长期记忆查询：关键词 + **可选**语义召回（P3-1）。

        开关 ``memory.long_term_semantic``（默认 **false**）：

        - false → 与 :meth:`long_term_memory` 等价（纯关键词），并带上
          ``vector_used=False`` 与原因，调用方一眼能看出「没做语义检索」而不是
          「语义检索没找到」；
        - true 且 embedder 可用 → 在关键词结果之上叠加语义召回，并把
          ``vector_used`` / ``semantic_matches`` / 候选扫描量如实返回。

        任何情况下都**只读**——不写入、不改访问计数、不落盘。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        mem_cfg = getattr(getattr(self._core, "config", None), "memory", None)
        enabled = bool(getattr(mem_cfg, "long_term_semantic", False))
        if not enabled or not query:
            out = self.long_term_memory(query)
            if isinstance(out, dict):
                out.setdefault("vector_used", False)
                out.setdefault(
                    "vector_reason",
                    "memory.long_term_semantic=false：本次仅关键词检索"
                    if not enabled else "无查询词：返回长期记忆摘要")
            return out
        try:
            store = self._long_term_store()
            return await store.query_semantic(
                query,
                max_candidates=int(
                    getattr(mem_cfg, "long_term_semantic_max_candidates", 60) or 60),
                threshold=float(
                    getattr(mem_cfg, "long_term_semantic_threshold", 0.35) or 0.35),
            )
        except Exception:  # noqa: BLE001 - 语义路径异常必须降级而非失败
            logger.warning("long_term 语义检索异常，降级为关键词", exc_info=True)
            return self.long_term_memory(query)

    def autonomous_maintenance(self) -> dict[str, Any]:
        """只读触发一次自治维护扫描（P5）：检测→风险分析→生成待审提案（不执行）。

        **工程准备自动、执行等待 Owner**：所有提案进入 R9 审批门 pending_owner，
        绝不自我授权安装。CI 预检默认未启用（诚实跳过，不伪造通过）。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            import asyncio
            from ..agent.autonomous_maintenance import (
                MaintenanceAgent, requirements_detector,
            )
            from ..agent.self_evolution import EvolutionGate, EvolutionProposer
            from ..memory.long_term import LongTermMemory

            mem = LongTermMemory(root)
            detect = requirements_detector("requirements.txt")
            agent = MaintenanceAgent(
                proposer=EvolutionProposer(), gate=EvolutionGate(),
                memory=mem, detect_fn=detect,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                fut = asyncio.ensure_future(agent.run_maintenance())
                scan = loop.run_until_complete(fut)
            else:
                scan = asyncio.run(agent.run_maintenance())
            out = scan.to_dict()
            out["available"] = True
            out["note"] = out.get("note") or (
                "提案均 pending_owner，需 Owner 在 R9 审批门显式批准后才可落地（fail-closed）。"
            )
            return out
        except Exception:  # noqa: BLE001 - 扫描失败不阻断控制面
            return {"available": False, "reason": "autonomous_maintenance 扫描异常"}

    def abilities(self) -> dict[str, Any]:
        """已加载的声明式技能清单（P2-2，只读）。"""
        skills = getattr(self._core, "skills", None)
        if skills is None:
            return {"available": False, "reason": "Skills 未启用"}
        rows = skills.describe() if hasattr(skills, "describe") else []
        return {
            "available": True,
            "count": len(rows),
            "items": rows,
            "errors": dict(getattr(skills, "errors", {}) or {}),
            "note": "tool 型技能的 level 是「技能声明等级」与「被组合工具等级」的较大者"
                    "（无提权上界）；技能安装仍需 Owner 在 draft 审批门放行。",
        }

    async def skill_evolution(self, request: str = "", *, create_draft: bool = False) -> dict[str, Any]:
        """触发一次个人能力演进评估（P6）：提案→沙箱→Benchmark→采纳判定。

        **采纳仍须 Owner**：即便评估为 adoptable，也只返回结论，绝不自我批准安装。

        P2-2 真接线（此前是死桩：构造了空 SkillEvolutionAgent 却连它都没调用）：
        - 复用 ``core.skill_drafts``（SkillDraftManager）作为草案生成器，它的 prompt 里
          会带上「当前可被组合的工具清单」，LLM 无法自造工具名；
        - 评估结果落地到控制面返回，含 sandbox_errors 与 benchmark 明细，便于 Owner 判断；
        - ``create_draft=True`` 时才真正落一条 pending_review 草案（会消耗一次 LLM 调用），
          默认 False 只做「给定 manifest 的评估」，不产生任何持久化副作用。
        """
        if not request:
            return {"available": False, "reason": "缺少能力需求描述"}
        drafts = getattr(self._core, "skill_drafts", None)
        llm = getattr(self._core, "llm", None)
        if drafts is None and llm is None:
            return {"available": False,
                    "reason": "缺少提案生成器（skill_drafts 未初始化且无 LLM）"}
        try:
            from ..skills.evolution import SkillEvolutionAgent

            agent = SkillEvolutionAgent(draft_manager=drafts)
            draft = None
            if create_draft:
                if drafts is None:
                    return {"available": False, "reason": "未初始化 skill_drafts，无法落草案"}
                draft = await drafts.create_from_request(request)
                manifest = dict(draft.manifest)
                draft_id = draft.id
            else:
                return {
                    "available": True,
                    "request": request,
                    "dry_run": True,
                    "note": "未传 create_draft，仅校验提案链路是否可用；"
                            "传 create_draft=True 才会真正生成待审草案（消耗一次 LLM 调用）。",
                    "proposer": "skill_drafts" if drafts is not None else "llm",
                }
            evaluation = agent.evaluate(draft_id, request, manifest)
            return {
                "available": True,
                "request": request,
                "draft_id": draft_id,
                "evaluation": evaluation.to_dict(),
                "draft_errors": list(draft.errors),
                "note": "草案已落 pending_review，采纳须 Owner 走 !anima draft approve；"
                        "本控制面不执行批准。",
            }
        except Exception as exc:  # noqa: BLE001 - 控制面不因单次评估异常而崩
            logger.warning("skill_evolution 评估失败: %s", type(exc).__name__)
            return {"available": False, "reason": f"skill_evolution 评估异常: {type(exc).__name__}"}

    # ------------------------------------------------------------------
    # 受限自然语言指令解析（白名单 + 确定性规则，无 LLM）
    # ------------------------------------------------------------------
    def parse_command(self, text: str) -> ParsedCommand:
        """解析 Owner 指令。未识别 → unknown（绝不猜测执行）。"""
        t = (text or "").strip()
        if not t:
            return ParsedCommand(intent="unknown", preview="空指令")

        # 批准提案 <id>（特权，需确认）
        m = re.search(r"批准\s*(?:提案)?\s*([A-Za-z0-9\-_]{3,})", t)
        if m:
            pid = m.group(1)
            return ParsedCommand(
                intent="approve", args={"proposal_id": pid},
                needs_confirmation=True,
                confirmation_endpoint="POST /api/evolution/{proposal_id}/approve",
                preview=f"将批准提案 {pid}（须经 R9 Owner 授权端点确认）",
            )

        # 拒绝提案 <id>（特权，需确认）
        m = re.search(r"拒绝\s*(?:提案)?\s*([A-Za-z0-9\-_]{3,})", t)
        if m:
            pid = m.group(1)
            return ParsedCommand(
                intent="reject", args={"proposal_id": pid},
                needs_confirmation=True,
                confirmation_endpoint="POST /api/evolution/{proposal_id}/reject",
                preview=f"将拒绝提案 {pid}（须经 R9 Owner 授权端点确认）",
            )

        # 运行健康检查 / 恢复评估（只读，触发一次评估）—— 须在 health 之前判定
        if re.search(r"(恢复|recover|健康检查|诊断)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="recover", needs_confirmation=False,
                preview="返回当前受控恢复计划（需 Owner 授权的动作已标 requires_owner）",
            )

        # 自治维护扫描（只读）—— P5：维护 / 自检 / 依赖自检 / 检查依赖 / 依赖过期 / 升级依赖 / maintenance
        # 须在 health 之前判定；用词较专，避免与「Agent 社会辩论升级依赖」等表述冲突。
        if re.search(r"(维护|自检|依赖自检|检查依赖|依赖过期|过期依赖|升级依赖|maintenance|deps|更新检查)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="maintenance", needs_confirmation=False,
                preview="触发一次自治维护扫描（检测过期依赖→风险分析→生成待审提案，不执行）",
            )

        # 查看健康（只读）
        if re.search(r"(查看|检查|状态|健康|health)", t, re.IGNORECASE) and "提案" not in t and "依赖" not in t:
            return ParsedCommand(
                intent="health", needs_confirmation=False,
                preview="返回当前运行时健康与受控恢复计划",
            )

        # 演化影响评分查询（只读）—— P2：影响 / 评分 / 收益 / impact / 效果
        # 须在 list_proposals 之前判定，避免「演化」一词被列表分支抢匹配。
        if re.search(r"(影响评分|收益|效果评估|impact|evolution_score|值不值|评分如何)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="impact", needs_confirmation=False,
                preview="返回演化影响评分（历史 Before/After 证据聚合 + 数据驱动建议）",
            )

        # 列出演化提案（只读）
        if re.search(r"(演化|提案|evolution|proposal)", t, re.IGNORECASE) and "批准" not in t and "拒绝" not in t:
            return ParsedCommand(
                intent="list_proposals", needs_confirmation=False,
                preview="返回待审批的自我进化提案列表",
            )

        # 长期稳定运行查询（只读）—— P0-02：稳定性 / uptime / 运行时长 / 稳定吗
        if re.search(r"(稳定|stability|uptime|运行时长|长期运行|稳不稳|稳吗)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="stability", needs_confirmation=False,
                preview="返回近 7/30/90 天稳定性记录（uptime / 异常 / 权限漂移 / 演化计数）",
            )

        # 真实环境观察查询（只读）—— P1：观察 / 观测 / 异常 / 趋势 / 环境
        if re.search(r"(观察|观测|环境|异常|趋势|observe|observation)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="observe", needs_confirmation=False,
                preview="返回真实环境观测报告（GitHub/性能/Audit DENY 增长趋势）",
            )

        # 多 Agent 社会辩论查询（只读）—— P3：社会 / 辩论 / 多视角 / society / 协作看法
        if re.search(r"(社会|辩论|多视角|agent 社会|society|大家怎么看|协作)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="society", needs_confirmation=False,
                preview="触发默认 Agent 社会（架构/安全/研究/编码/批判/裁判）多视角辩论，返回建议性裁决",
            )

        # 长期记忆查询（只读）—— P4：记忆 / 记得 / 习惯 / 经验 / 为什么 / memory
        if re.search(r"(记忆|记得|习惯|经验|为什么|为何|memory|你还记得)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="memory", needs_confirmation=False,
                preview="查询长期记忆（Owner 偏好/历史决策/行为模式/设计原因）",
            )

        # 个人能力演进查询（只读）—— P6：能力 / 技能 / 演进 / skill / 生态
        if re.search(r"(能力演进|技能演进|技能生态|个人能力|skill|新技能|能力生态)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="skill", needs_confirmation=False,
                preview="个人能力演进评估（提案→沙箱→Benchmark→采纳判定，采纳仍须 Owner）",
            )

        return ParsedCommand(intent="unknown", preview=f"未识别指令：{t[:40]}")

    # ------------------------------------------------------------------
    # 只读意图执行（P3-3：WebUI 与 QQ 共用的**唯一**实现）
    # ------------------------------------------------------------------
    # 本表就是「QQ 侧允许执行的意图」白名单，不在表内的一律不执行（fail-closed）。
    # 只读 = 不写盘、不改权限、不执行系统动作；`list_proposals` 是只读列表。
    READONLY_INTENTS: frozenset[str] = frozenset({
        "health", "recover", "stability", "observe", "impact",
        "memory", "maintenance", "list_proposals",
    })

    async def run_readonly(self, cmd: "ParsedCommand", text: str = "") -> dict[str, Any]:
        """执行**只读**意图，返回 ``{payload_key: data}``；不适用则返回 ``{}``。

        铁律：``needs_confirmation=True`` 的意图（批准 / 拒绝提案）**永不在此执行**，
        只由解析层给出待确认端点；``skill`` / ``society`` 这两个会落草案或触发多轮
        LLM 的意图也不属于只读白名单，由各自调用方显式处理（WebUI 有，QQ 没有）。

        每一路都独立 try/except：单路失败只影响该路，不把整条查询打挂。
        """
        intent = getattr(cmd, "intent", "") or ""
        if getattr(cmd, "needs_confirmation", False):
            return {}
        if intent not in self.READONLY_INTENTS:
            return {}
        if intent in ("health", "recover"):
            try:
                return {"overview": self.overview().to_dict()}
            except Exception:  # noqa: BLE001
                return {}
        if intent == "stability":
            out: dict[str, Any] = {}
            for w in (7, 30, 90):
                try:
                    out[f"{w}d"] = self.stability_summary(w)
                except Exception:  # noqa: BLE001
                    out[f"{w}d"] = {"available": False, "reason": "stability 聚合异常"}
            return {"stability": out}
        if intent == "observe":
            try:
                return {"environment": self.environment_report()}
            except Exception:  # noqa: BLE001
                return {}
        if intent == "impact":
            try:
                return {"impact": self.evolution_impact()}
            except Exception:  # noqa: BLE001
                return {}
        if intent == "memory":
            # P3-1：走「关键词 +（可选）语义召回」读路径（只读）
            return {"memory": await self.long_term_memory_semantic(query=text or None)}
        if intent == "maintenance":
            try:
                return {"maintenance": self.autonomous_maintenance()}
            except Exception:  # noqa: BLE001
                return {}
        if intent == "list_proposals":
            try:
                from ..agent.self_evolution import list_evolution_proposals
                return {"proposals": list_evolution_proposals()}
            except Exception:  # noqa: BLE001
                return {}
        return {}
