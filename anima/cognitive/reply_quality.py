"""Reply-quality telemetry without retaining chat text.

The report is intentionally aggregate-only: repetition checks use a one-way
fingerprint, never the message body.  This keeps operational diagnostics from
becoming another source of conversational memory.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from anima.core.persistence import PersistenceProvider

_BJ = timezone(timedelta(hours=8))
_FILE = "reply_quality.json"
_LABEL_RE = re.compile(r"^\s*(?:\[?哈基米\]?\s*[:：])")
_IDENTITY_RE = re.compile(r"(?:antigravity|openai|chatgpt|系统提示|编程助手)", re.I)


def _day() -> str:
    return datetime.now(_BJ).date().isoformat()


def _empty() -> dict[str, int]:
    return {
        "total_replies": 0, "unavailable_replies": 0, "overlong_replies": 0,
        "label_leaks": 0, "identity_leaks": 0, "repeated_replies": 0,
        "tool_calls": 0, "tool_failures": 0, "outbound_failures": 0,
        "image_outbounds": 0, "sticker_outbounds": 0,
        # L0 interaction telemetry（不存正文）
        "empty_should_reply": 0,   # 应回复但最终无可用正文/静默
        "tool_spins": 0,           # 同工具同参连续重复（ReAct 死循环守卫触发次数）
        "space_link_ok": 0,        # B 站主页链接被处理且有信息性结果
        "space_link_fail": 0,      # 主页链接路径失败/空结果
        "react_max_rounds": 0,     # ReAct 触顶轮次次数
        # 闸门跳过（不存正文，只计原因桶）
        "gate_skip_other_bot": 0,
        "gate_skip_idle": 0,
        "gate_skip_mention_only": 0,
        "gate_skip_unfamiliar": 0,
        "gate_skip_end_signal": 0,
        "gate_skip_passive_cooldown": 0,
        "gate_skip_other": 0,
        "guard_fixation_rewrite": 0,
        # 上下文预算（只记 token 估算与裁剪计数，不保存任何正文）
        "context_requests": 0,
        "context_system_tokens_total": 0,
        "context_input_tokens_total": 0,
        "context_fragments_dropped": 0,
        "context_history_dropped": 0,
        "context_protected_over_budget": 0,
        # 兜底 / 降级出站（口径：这一条出站**不是**模型正常生成的正文，
        # 而是护栏改写或降级话术替身）。按原因分桶，未知一律进 fallback_other。
        # 2026-09-19：此前兜底出口只 `return` 文本、不留结构化痕迹，
        # 话术改成多变体后连 `grep 文案` 也不再可靠 ⇒ 补可观测面。
        "fallback_no_tool": 0,        # react：无工具痕迹却没生成出正文
        "fallback_tool_failed": 0,    # react：有工具失败痕迹且没生成出正文
        "fallback_tool_ok": 0,        # react：工具都成功但模型仍空回复
        # react：**真实病因**桶 —— 本轮发生过「上游 HTTP 200 + 空补全」且穷尽候选后仍空。
        # 2026-09-22：此前这一情形被 `tool_trace` 分流洗成 `fallback_no_tool`，
        # 于是"上游静默降级"被读成"模型自己无话可说"，故障不可观测（归因错位）。
        "fallback_upstream_empty": 0,
        # react：上游静默的**第二种**病因 —— 传输故障（候选穷尽后仍失败）或熔断器打开。
        # 2026-09-23：熔断时 `_post` 早退、不增 `empty_completion_count`，只看 200-空
        # 会把纯传输故障洗成 `fallback_no_tool`（"模型自己无话可说"，与事实相反）。
        "fallback_upstream_error": 0,
        "fallback_breaker": 0,        # generation：LLM 熔断器打开
        "fallback_react_error": 0,    # generation：ReAct 抛异常
        "fallback_empty_reply": 0,    # generation：LLM 返回空/占位串
        "fallback_mechanical": 0,     # guardrail：机械短语命中被改写
        "fallback_leak_blocked": 0,   # guardrail：危险内容被拦截
        "fallback_analysis_essay": 0, # guardrail：破角色分析体被改写
        "fallback_political": 0,      # guardrail：政治敏感话题回避
        "fallback_cold_bullying": 0,  # generation：被霸凌时的简短冷回（COLD_RESPONSES）
        "fallback_cold_avoidance": 0, # generation：回避期（scar）的极短冷回
        "fallback_tired": 0,          # handler：社交疲劳 gate 的罐头回复（第 14 出口）
        "fallback_other": 0,          # 不在白名单的 kind（防拼错导致键无界增长）
    }


class ReplyQuality:
    """Daily reply-quality counters, persisted atomically through core storage."""

    def __init__(self, data_dir: str):
        self.persistence = PersistenceProvider(data_dir)
        raw = self.persistence.read_json(_FILE, {})
        self.data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        self.data.setdefault("days", {})
        self.data.setdefault("recent", [])
        self.data.setdefault("version", 1)

    def _metrics(self, day: str | None = None) -> dict[str, int]:
        key = day or _day()
        days = self.data["days"]
        current = days.setdefault(key, _empty())
        for name, value in _empty().items():
            current.setdefault(name, value)
        return current

    def _save(self) -> None:
        # Keep at most seven days of fingerprints; counters themselves are tiny
        # and deliberately retained for historical trend inspection.
        cutoff = (_day())
        self.data["recent"] = [r for r in self.data.get("recent", [])
                               if r.get("day", cutoff) >= cutoff][-80:]
        self.persistence.write_json(_FILE, self.data)

    @staticmethod
    def _fingerprint(text: str) -> str:
        normalized = re.sub(r"\s+", "", text).lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]

    def record_reply(self, text: str, *, detail_type: str, target_id: Any,
                     tool_trace: list[dict] | None = None) -> None:
        metrics = self._metrics()
        metrics["total_replies"] += 1
        if len(text) > 500:
            metrics["overlong_replies"] += 1
        if _LABEL_RE.search(text):
            metrics["label_leaks"] += 1
        if _IDENTITY_RE.search(text):
            metrics["identity_leaks"] += 1
        scope = f"{detail_type}:{target_id}"
        fp = self._fingerprint(text)
        recent = self.data["recent"]
        if len(text) >= 12 and any(r.get("scope") == scope and r.get("fp") == fp for r in recent):
            metrics["repeated_replies"] += 1
        recent.append({"day": _day(), "scope": scope, "fp": fp})
        if tool_trace:
            metrics["tool_calls"] += len(tool_trace)
            metrics["tool_failures"] += sum(not bool(item.get("ok")) for item in tool_trace)
        self._save()

    def record_unavailable(self) -> None:
        self._metrics()["unavailable_replies"] += 1
        self._save()

    def record_empty_should_reply(self) -> None:
        """应进入回复路径但最终没有可用正文（静默/空串）。"""
        self._metrics()["empty_should_reply"] += 1
        self._save()

    def record_tool_spin(self, count: int = 1) -> None:
        self._metrics()["tool_spins"] += max(0, int(count))
        self._save()

    def record_react_max_rounds(self) -> None:
        self._metrics()["react_max_rounds"] += 1
        self._save()

    def record_space_link(self, *, ok: bool) -> None:
        metrics = self._metrics()
        if ok:
            metrics["space_link_ok"] += 1
        else:
            metrics["space_link_fail"] += 1
        self._save()

    def record_gate_skip(self, reason: str) -> None:
        """记录群回复闸门跳过原因（other_bot / idle / mention_only / unfamiliar / end_signal / other）。"""
        key = {
            "other_bot": "gate_skip_other_bot",
            "idle": "gate_skip_idle",
            "mention_only": "gate_skip_mention_only",
            "unfamiliar": "gate_skip_unfamiliar",
            "end_signal": "gate_skip_end_signal",
            "passive_cooldown": "gate_skip_passive_cooldown",
            "stale_dropped": "gate_skip_stale_dropped",
        }.get(str(reason or ""), "gate_skip_other")
        metrics = self._metrics()
        metrics[key] = int(metrics.get(key, 0) or 0) + 1
        self._save()

    def record_fallback(self, kind: str) -> None:
        """记录一次「兜底出站」。

        口径：这一条最终出站**不是模型正常生成的正文**，而是护栏改写或降级话术替身。
        用途是让「机器话术出站占比」可被观测 —— 此前兜底出口只 `return` 文本、
        不留任何结构化痕迹，话术改为多变体后连 `grep 文案` 也不再可靠。

        kind 走白名单映射（与 `record_gate_skip` 同口径）：**未知一律进
        `fallback_other`**，避免调用方拼错键名导致计数器无界增长。
        """
        key = {
            "no_tool": "fallback_no_tool",
            "tool_failed": "fallback_tool_failed",
            "tool_ok": "fallback_tool_ok",
            "upstream_empty": "fallback_upstream_empty",
            "upstream_error": "fallback_upstream_error",
            "breaker": "fallback_breaker",
            "react_error": "fallback_react_error",
            "empty_reply": "fallback_empty_reply",
            "mechanical": "fallback_mechanical",
            "leak_blocked": "fallback_leak_blocked",
            "analysis_essay": "fallback_analysis_essay",
            "political": "fallback_political",
            "cold_bullying": "fallback_cold_bullying",
            "cold_avoidance": "fallback_cold_avoidance",
            "tired": "fallback_tired",
        }.get(str(kind or ""), "fallback_other")
        metrics = self._metrics()
        metrics[key] = int(metrics.get(key, 0) or 0) + 1
        self._save()

    def record_guard_fixation(self) -> None:
        metrics = self._metrics()
        metrics["guard_fixation_rewrite"] = int(metrics.get("guard_fixation_rewrite", 0) or 0) + 1
        self._save()

    def record_context(self, system_stats: dict[str, Any] | None,
                       message_stats: dict[str, Any] | None) -> None:
        """记录上下文预算结果；统计键名和数量，不保存片段/消息正文。"""
        system_stats = system_stats if isinstance(system_stats, dict) else {}
        message_stats = message_stats if isinstance(message_stats, dict) else {}
        metrics = self._metrics()
        metrics["context_requests"] += 1
        metrics["context_system_tokens_total"] += int(system_stats.get("estimated_tokens", 0) or 0)
        metrics["context_input_tokens_total"] += int(message_stats.get("estimated_tokens", 0) or 0)
        metrics["context_fragments_dropped"] += len(system_stats.get("dropped_keys") or [])
        metrics["context_history_dropped"] += len(message_stats.get("dropped_keys") or [])
        metrics["context_protected_over_budget"] += int(bool(
            system_stats.get("protected_over_budget") or message_stats.get("protected_over_budget")
        ))
        self._save()

    def record_outbound(self, message: str | list[dict[str, Any]], ok: bool) -> None:
        metrics = self._metrics()
        if not ok:
            metrics["outbound_failures"] += 1
        if isinstance(message, list):
            images = [s for s in message if isinstance(s, dict) and s.get("type") == "image"]
            metrics["image_outbounds"] += len(images)
            # Stickers use local paths under the sticker data directory. Generated
            # images remain visible in the aggregate image total but not this count.
            metrics["sticker_outbounds"] += sum("stickers" in str(s.get("data", {}).get("file", "")) for s in images)
        self._save()

    def report(self, day: str | None = None) -> dict[str, Any]:
        key = day or _day()
        result: dict[str, Any] = {"date": key, **dict(self._metrics(key))}
        requests = int(result.get("context_requests", 0) or 0)
        result["context_avg_system_tokens"] = (
            int(result.get("context_system_tokens_total", 0) or 0) / requests if requests else 0.0
        )
        result["context_avg_input_tokens"] = (
            int(result.get("context_input_tokens_total", 0) or 0) / requests if requests else 0.0
        )
        result["context_trim_rate"] = (
            sum((int(result.get("context_fragments_dropped", 0) or 0),
                 int(result.get("context_history_dropped", 0) or 0))) / requests if requests else 0.0
        )
        # 兜底出站合计：按 **前缀** 汇总 —— 将来新增 kind 自动纳入，无需改这里。
        result["fallback_total"] = sum(
            int(v or 0) for k, v in result.items() if str(k).startswith("fallback_")
        )
        return result

    def format_report(self, day: str | None = None) -> str:
        report = self.report(day)
        return (
            f"回复质量日报 {report['date']}：回复 {report['total_replies']}，"
            f"不可用 {report['unavailable_replies']}，应回却空 {report.get('empty_should_reply', 0)}，"
            f"兜底出站 {report.get('fallback_total', 0)}"
            f"（其中上游静默返空 {report.get('fallback_upstream_empty', 0)}、"
            f"传输故障/熔断 {report.get('fallback_upstream_error', 0)}），"
            f"超长 {report['overlong_replies']}，"
            f"标签泄漏 {report['label_leaks']}，身份泄漏 {report['identity_leaks']}，"
            f"复读 {report['repeated_replies']}；工具失败 {report['tool_failures']}/{report['tool_calls']}，"
            f"工具空转 {report.get('tool_spins', 0)}，ReAct 触顶 {report.get('react_max_rounds', 0)}，"
            f"主页链接 ok/fail {report.get('space_link_ok', 0)}/{report.get('space_link_fail', 0)}，"
            f"出站失败 {report['outbound_failures']}，图片 {report['image_outbounds']}（表情 {report['sticker_outbounds']}）。"
        )

    def close(self) -> None:
        self._save()


_quality: ReplyQuality | None = None
_quality_dir: str | None = None


def get_reply_quality(data_dir: str = "data") -> ReplyQuality:
    """Return the process singleton, rebinding when data_dir changes.

    Tests and multi-instance boots use different data directories; reusing an
    older singleton would leak counters across isolation boundaries.
    """
    global _quality, _quality_dir
    if _quality is None or _quality_dir != data_dir:
        if _quality is not None:
            try:
                _quality.close()
            except Exception:
                pass
        _quality = ReplyQuality(data_dir)
        _quality_dir = data_dir
    return _quality


def close_reply_quality() -> None:
    global _quality, _quality_dir
    if _quality is not None:
        _quality.close()
    _quality = None
    _quality_dir = None
