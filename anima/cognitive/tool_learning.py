"""工具调用自学习 —— 记录 ReAct 工具调用轨迹，归纳使用心得。

设计（对齐 Anima 认知引擎约定）：
- 调用轨迹来自 ReActLoop.tool_trace（每次回复采集，不进 LLM 上下文，零开销）。
- records 环形上限防无界增长；preferences 累计成功/失败计数 + 归纳规律。
- rules_fragment 注入认知 system（严守单一 system 消息契约，插在身份锁定之前）。
- 规律归纳走 rumination 心跳（单独存储 tool_learning.json），不挤占人格漂移预算。
- 数据通过 PersistenceProvider 原子写，与 chat_memory / crossgroup 互补零迁移。
"""

from __future__ import annotations
import json
import logging
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.tool_learning")


class ToolLearning:
    """工具调用自学习管理器。"""

    def __init__(self, data_dir: str = "data",
                 records_max: int = 200, inject_top_k: int = 4,
                 summarize_interval: int = 10):
        self._persist = PersistenceProvider(data_dir)
        self._path = "tool_learning.json"
        self.records_max = records_max
        self.inject_top_k = inject_top_k
        self.summarize_interval = summarize_interval
        self._data = self._load()

    def _load(self) -> dict:
        return self._persist.read_json(self._path, {"records": [], "preferences": {}, "summarized": {}})

    def _save(self) -> None:
        self._persist.write_json(self._path, self._data)

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    def record(self, permission_role: str, trace: list[dict], context_text: str = "") -> None:
        """记录一次回复里 bot 的工具调用轨迹。

        Args:
            permission_role: 调用者角色（owner/admin/user...），仅作留痕
            trace: ReActLoop.tool_trace 列表（每项含 name/args/result/ok/round）
            context_text: 触发本次回复的用户消息（用于归纳时的背景）
        """
        if not trace:
            return
        ctx = (context_text or "")[:200]
        for t in trace:
            self._data["records"].append({
                "tool": t.get("name", "unknown"),
                "context": ctx,
                "success": bool(t.get("ok", False)),
                "role": permission_role or "user",
                "ts": time.time(),
            })
        # 环形上限，防无界增长
        rmax = self.records_max
        if len(self._data["records"]) > rmax:
            self._data["records"] = self._data["records"][-rmax:]
        # 偏好计数
        for t in trace:
            name = t.get("name", "unknown")
            pref = self._data["preferences"].setdefault(name, {
                "success_count": 0, "fail_count": 0, "rules": [],
            })
            if t.get("ok"):
                pref["success_count"] += 1
            else:
                pref["fail_count"] += 1
        self._save()

    def _success_count(self, tool: str) -> int:
        return sum(1 for r in self._data["records"] if r["tool"] == tool and r["success"])

    def get_records(self, tool: str, limit: int = 50) -> list[dict]:
        return [r for r in self._data["records"] if r["tool"] == tool][-limit:]

    def summarize_due_tools(self) -> list[str]:
        """返回「成功次数≥3 且尚未在当前成功次数上归纳过」的工具名列表。"""
        due = []
        for tool in self._data["preferences"]:
            cnt = self._success_count(tool)
            if cnt < 3:
                continue
            if self._data.get("summarized", {}).get(tool) == cnt:
                continue
            due.append(tool)
        return due

    # ------------------------------------------------------------------
    # 规律归纳（LLM）
    # ------------------------------------------------------------------

    @scoped_llm("tool_learning", "summarize")
    async def summarize_rules(self, llm, tool: str) -> Optional[str]:
        """用 LLM 归纳某工具的使用规律，回写 preferences[tool].rules。

        返回归纳到的 rule 文本，或 None（记录不足/解析失败）。
        """
        recs = self.get_records(tool, limit=50)
        if self._success_count(tool) < 3:
            return None
        recs_text = "\n".join(
            f"- 背景：{r['context'][:80]}，结果：{'成功' if r['success'] else '失败'}"
            for r in recs
        )
        prompt = (
            f"以下是使用工具 {tool} 的历史记录：\n{recs_text}\n\n"
            "请总结一条简短的「什么时候用这个工具效果好」的使用心得（中文，一句话）。\n"
            '只输出 JSON：{"rule": "..."}'
        )
        try:
            text = await llm.chat([{"role": "user", "content": prompt}], temperature=0.4, max_tokens=120)
            if not text:
                return None
            text = text.strip()
            text = text.lstrip("`").replace("```json", "").replace("```", "").strip("`").strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # 上游偶尔无视 JSON 约束，直接返回一句话心得。这里做容错：
                # 优先从正文中摘出 {"rule": "..."}，再退化为把整句当作 rule，
                # 避免对 generate_image 这类纯文本工具结果制造无意义的 WARNING 噪声。
                import re as _re
                m = _re.search(r'\{[^{}]*"rule"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
                if m:
                    data = {"rule": m.group(1)}
                else:
                    cleaned = text.strip().strip('"').strip()
                    if not cleaned:
                        logger.warning("工具规律总结无可用内容，已跳过 tool=%s", tool)
                        return None
                    data = {"rule": cleaned}
            rule = (data.get("rule") or "").strip()
            if rule:
                pref = self._data["preferences"].setdefault(
                    tool, {"success_count": 0, "fail_count": 0, "rules": []}
                )
                pref["rules"].append(rule)
                self._data.setdefault("summarized", {})[tool] = self._success_count(tool)
                self._save()
                logger.info("工具规律总结已更新 tool=%s rule_len=%d", tool, len(rule))
                return rule
        except Exception:
            logger.exception("工具规律总结失败")
        return None

    # ------------------------------------------------------------------
    # 注入片段
    # ------------------------------------------------------------------

    def rules_fragment(self, top_k: Optional[int] = None) -> str:
        """生成注入 system 的「我的工具使用心得」片段（仅列成功规律）。"""
        top_k = top_k or self.inject_top_k
        prefs = self._data.get("preferences", {})
        lines = []
        for name, p in prefs.items():
            rules = p.get("rules") or []
            if not rules:
                continue
            lines.append(f"· {name}：{rules[-1]}")
        if not lines:
            return ""
        return "## 我的工具使用心得（从经历里学来的）\n" + "\n".join(lines[:top_k])


# 全局单例
_learning: Optional["ToolLearning"] = None


def get_tool_learning(data_dir: str = "data", **kwargs) -> "ToolLearning":
    global _learning
    if _learning is None:
        _learning = ToolLearning(data_dir, **kwargs)
    return _learning


def close_tool_learning() -> None:
    global _learning
    _learning = None
