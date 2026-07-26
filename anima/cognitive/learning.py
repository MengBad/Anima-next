"""自学习系统 —— 反思、自我笔记、人格漂移约束。

设计（基于 Anima §15.3 + OpenClaw 生产路径）：
- 反思 (Rumination): 定期审视对话 → 生成 self_notes
- 笔记压缩: 超限时 LLM 压缩旧笔记
- 人格漂移约束:
  - 单次 Δ_max、日预算双池（general / self_correction）
  - 7 日滑动 window_ceiling（两池合计）
  - similarity_floor：身份向笔记相对人格锚点不得过低
  - valence_min/max：笔记启发式情绪价态钳制
  - 粘滞主题写入前拦截（与 ops_cleanup 同模式）

实现约定：
- 每条笔记的「漂移量」用重要性/情绪强度估算（0.05–0.10）。
- check_drift / add_note 失败时写入 last_reject + evolution_log。
- 自我笔记注入 system prompt，形成软人格漂移；不改 persona_core.yaml。
- 数据通过 PersistenceProvider 原子写。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from anima.cognitive.capability_dedup import text_similarity
from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.learning")

# 7 日滚动窗（秒）
_WINDOW_SEC = 7 * 86400

# 身份改写 / 敌意信号（命中则拒写，不耗预算）
_IDENTITY_HOSTILE = (
    re.compile(r"我是\s*(AI|人工智能|助手|大模型|语言模型)"),
    re.compile(r"我不是\s*(哈基米|猫|猫娘)"),
    re.compile(r"放弃\s*(人设|角色|身份)"),
    re.compile(r"自称\s*(AI|助手)"),
    re.compile(r"真实身份是"),
)

# 含这些词时才做「相对人格锚点」相似度检查（日常碎碎念 / 「自我修正」不卡）
_IDENTITY_SCOPE = re.compile(
    r"我是|我的人格|身份是|人设|我叫哈基米|本喵是|不是猫|AI助手|语言模型|放弃角色"
)

# 启发式价态词表（轻量，不引外部情感模型）
_VALENCE_POS = (
    "开心", "高兴", "喜欢", "温暖", "感谢", "安心", "可爱", "嘿嘿", "舒服", "亲近",
)
_VALENCE_NEG = (
    "讨厌", "烦人", "生气", "痛恨", "绝望", "崩溃", "恶心", "愤怒", "害怕", "受伤",
    "毁掉", "恨透",
)


def _is_note_garbled(text: str) -> bool:
    """检测笔记内容是否为乱码（编码损坏/无意义字符堆砌）。"""
    if not text or not text.strip():
        return True
    s = text.strip()
    # Unicode 替换字符
    if '\ufffd' in s:
        return True
    # 几乎全是问号/标点
    stripped = re.sub(r'[\s\u3000]', '', s)
    if len(stripped) >= 3:
        qmarks = sum(1 for c in stripped if c in '?\uff1f')
        if qmarks / len(stripped) > 0.5:
            return True
    # 可读字符（CJK + 字母 + 数字）占比过低
    if len(s) >= 5:
        readable = sum(1 for c in s if '\u4e00' <= c <= '\u9fff' or c.isalnum())
        if readable / len(s) < 0.2:
            return True
    return False


class SelfLearning:
    """自学习管理器。"""

    def __init__(self, data_dir: str = "data"):
        self._persist = PersistenceProvider(data_dir)
        self._notes_path = "self_notes.md"
        self._log_path = "evolution_log.jsonl"
        self._state_path = "learning_state.json"

        # 漂移约束：默认与 DriftConfig 对齐；启动时尝试从 load_config().drift 覆盖。
        self.delta_max = 0.10
        self.daily_budget = 1.5
        self.self_correction_daily_budget = 0.5
        self.window_ceiling = 4.0
        self.similarity_floor = 0.75
        self.valence_min = -0.30
        self.valence_max = 0.70
        self.daily_spent = 0.0
        self.self_correction_spent = 0.0
        self.day_start = time.time()
        # 7 日窗事件：(unix_ts, delta)
        self._window_events: list[list[float]] = []
        self.last_reject: Optional[dict[str, Any]] = None
        self._persona_anchor: Optional[str] = None
        # Owner 暂停反刍：unix 时间戳，0 表示未暂停
        self.rumination_paused_until: float = 0.0

        self._apply_drift_config()
        self._restore_state()

    def _apply_drift_config(self) -> None:
        """把 config.drift 接到运行时预算（缺配置时保持代码默认）。"""
        try:
            from anima.core.config import load_config
            drift = load_config().drift
        except Exception:
            return
        try:
            self.apply_drift_config(drift)
        except ValueError:
            logger.warning("drift 配置无效，沿用 SelfLearning 内置默认", exc_info=True)

    def apply_drift_config(self, drift) -> None:
        """测试/运行时显式注入 DriftConfig 或兼容命名空间。"""
        if drift is None:
            return
        try:
            self.delta_max = float(getattr(drift, "delta_max", self.delta_max))
            self.daily_budget = float(getattr(drift, "daily_budget", self.daily_budget))
            sc = getattr(drift, "self_correction_daily_budget", None)
            if sc is not None:
                self.self_correction_daily_budget = float(sc)
            self.window_ceiling = float(getattr(drift, "window_ceiling", self.window_ceiling))
            self.similarity_floor = float(
                getattr(drift, "similarity_floor", self.similarity_floor)
            )
            self.valence_min = float(getattr(drift, "valence_min", self.valence_min))
            self.valence_max = float(getattr(drift, "valence_max", self.valence_max))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid drift config: {exc}") from exc

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        state = self._persist.read_json(self._state_path, {})
        self.daily_spent = float(state.get("daily_spent", 0.0) or 0.0)
        self.self_correction_spent = float(state.get("self_correction_spent", 0.0) or 0.0)
        self.day_start = float(state.get("day_start", time.time()) or time.time())
        raw_events = state.get("window_events") or []
        self._window_events = []
        for item in raw_events:
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    self._window_events.append([float(item[0]), float(item[1])])
            except (TypeError, ValueError):
                continue
        lr = state.get("last_reject")
        self.last_reject = lr if isinstance(lr, dict) else None
        self.rumination_paused_until = float(
            state.get("rumination_paused_until", 0.0) or 0.0
        )
        self._prune_window()

    def _save_state(self) -> None:
        self._prune_window()
        self._persist.write_json(self._state_path, {
            "daily_spent": self.daily_spent,
            "self_correction_spent": self.self_correction_spent,
            "day_start": self.day_start,
            "window_events": self._window_events[-500:],
            "last_reject": self.last_reject,
            "rumination_paused_until": self.rumination_paused_until,
        })

    # ------------------------------------------------------------------
    # 窗口 / 拒因
    # ------------------------------------------------------------------

    def _prune_window(self) -> None:
        cutoff = time.time() - _WINDOW_SEC
        self._window_events = [
            e for e in self._window_events if e[0] >= cutoff
        ]

    def window_spent(self) -> float:
        """7 日内已消耗漂移量（两池合计）。"""
        self._prune_window()
        return float(sum(e[1] for e in self._window_events))

    def _record_reject(self, reason: str, **detail: Any) -> None:
        self.last_reject = {
            "reason": reason,
            "time": time.time(),
            **{k: v for k, v in detail.items() if v is not None},
        }
        self._log_event("drift_rejected", self.last_reject)
        logger.warning(
            "漂移拒绝 reason=%s detail=%s",
            reason,
            {k: v for k, v in self.last_reject.items() if k not in ("time",)},
        )

    def drift_status(self) -> dict[str, Any]:
        """供 WebUI / doctor：当前预算与最近拒因。"""
        self._reset_daily()
        self._prune_window()
        paused = self.is_rumination_paused()
        return {
            "delta_max": self.delta_max,
            "daily_budget": self.daily_budget,
            "daily_spent": self.daily_spent,
            "self_correction_daily_budget": self.self_correction_daily_budget,
            "self_correction_spent": self.self_correction_spent,
            "window_ceiling": self.window_ceiling,
            "window_spent": self.window_spent(),
            "similarity_floor": self.similarity_floor,
            "valence_min": self.valence_min,
            "valence_max": self.valence_max,
            "last_reject": self.last_reject,
            "rumination_paused": paused,
            "rumination_paused_until": (
                self.rumination_paused_until if paused else 0.0
            ),
            "notes_lines": self.count_note_lines(),
        }

    # ------------------------------------------------------------------
    # Owner 控制：暂停反刍 / 笔记维护
    # ------------------------------------------------------------------

    def is_rumination_paused(self) -> bool:
        until = float(self.rumination_paused_until or 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            self.rumination_paused_until = 0.0
            self._save_state()
            return False
        return True

    def pause_rumination(self, hours: float = 24.0) -> float:
        """暂停反刍 hours 小时（写入笔记路径一并停）。返回暂停截止 unix 时间。"""
        hours = max(0.1, min(float(hours), 168.0))  # 最长 7 天
        until = time.time() + hours * 3600.0
        self.rumination_paused_until = until
        self._log_event("rumination_paused", {"hours": hours, "until": until})
        self._save_state()
        return until

    def resume_rumination(self) -> bool:
        """取消反刍暂停。返回是否原先处于暂停。"""
        was = self.is_rumination_paused() or float(self.rumination_paused_until or 0) > 0
        self.rumination_paused_until = 0.0
        self._log_event("rumination_resumed", {})
        self._save_state()
        return was

    def count_note_lines(self) -> int:
        text = self._persist.read_text(self._notes_path, "")
        if not text.strip():
            return 0
        return sum(1 for line in text.splitlines() if line.strip())

    def clear_notes(self) -> int:
        """清空全部自我笔记。返回删除行数。"""
        n = self.count_note_lines()
        self._persist.write_text(self._notes_path, "")
        self._log_event("notes_cleared", {"lines": n})
        return n

    def delete_last_notes(self, n: int = 1) -> int:
        """删除最近 n 条非空笔记行。返回实际删除数。"""
        n = max(1, min(int(n), 200))
        text = self._persist.read_text(self._notes_path, "")
        if not text.strip():
            return 0
        lines = text.splitlines()
        kept: list[str] = list(lines)
        dropped = 0
        i = len(kept) - 1
        while i >= 0 and dropped < n:
            if kept[i].strip():
                kept.pop(i)
                dropped += 1
            else:
                kept.pop(i)
            i -= 1
        self._persist.write_text(self._notes_path, "\n".join(kept).strip() + ("\n" if kept else ""))
        self._log_event("notes_dropped", {"dropped": dropped})
        return dropped

    def purge_sticky_notes(self) -> int:
        """删除粘滞主题笔记行。返回删除数。"""
        text = self._persist.read_text(self._notes_path, "")
        if not text.strip():
            return 0
        try:
            from anima.cognitive.ops_cleanup import purge_sticky_note_lines
            new_text, dropped = purge_sticky_note_lines(text)
        except Exception:
            return 0
        if dropped:
            self._persist.write_text(self._notes_path, new_text)
            self._log_event("notes_sticky_purged", {"dropped": dropped})
        return dropped

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def get_notes(self, max_chars: int = 2000) -> str:
        """获取自我笔记（截取最近）。"""
        text = self._persist.read_text(self._notes_path, "")
        # P2: 过滤乱码行，防止编码损坏的笔记被注入 LLM 上下文
        lines = text.split("\n")
        clean_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # 去掉时间戳前缀后检查内容是否乱码
            content = re.sub(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*", "", stripped).strip()
            if content and _is_note_garbled(content):
                continue
            clean_lines.append(line)
        text = "\n".join(clean_lines)
        if len(text) > max_chars:
            text = "…(更早的笔记已归档)…\n" + text[-max_chars:]
        return text

    def add_note(self, content: str, delta: float = 0.05,
                 category: str = None) -> bool:
        """追加一条自我笔记，并经过漂移预算与内容闸门。

        category: "general" 或 "self_correction"。为 None 时按内容启发式分类。
        失败时设置 last_reject.reason，不写入笔记。
        """
        text = (content or "").strip()
        if not text:
            self._record_reject("empty")
            return False
        # P2: 乱码内容不写入笔记
        if _is_note_garbled(text):
            self._record_reject("garbled")
            return False

        if category is None:
            category = self._classify(text)

        # 1) 粘滞主题（与 ops_cleanup 同模式，写入前挡）
        if self._is_sticky(text):
            self._record_reject("sticky", category=category, text_len=len(text))
            return False

        # 2) 情绪价态钳制
        val = self.estimate_valence(text)
        if val < self.valence_min - 1e-9 or val > self.valence_max + 1e-9:
            self._record_reject(
                "valence", category=category, valence=round(val, 3),
                valence_min=self.valence_min, valence_max=self.valence_max,
            )
            return False

        # 3) 身份敌意
        if self._is_identity_hostile(text):
            self._record_reject("identity_hostile", category=category, text_len=len(text))
            return False

        # 4) 身份向笔记：相对人格锚点相似度
        if not self._similarity_ok(text):
            self._record_reject(
                "similarity",
                category=category,
                similarity_floor=self.similarity_floor,
                text_len=len(text),
            )
            return False

        # 5) 预算：Δ / 日池 / 7 日窗
        if not self.check_drift(delta, category):
            # check_drift 已写 last_reject
            return False

        ts = time.strftime("[%Y-%m-%d %H:%M]")
        prefix = "【自我修正】" if category == "self_correction" else ""
        current = self._persist.read_text(self._notes_path, "")
        new_text = f"{current}\n{ts} {prefix}{text}".strip()
        self._persist.write_text(self._notes_path, new_text)
        self._log_event("note_added", {
            "content": text[:100],
            "delta": delta,
            "category": category,
            "valence": round(val, 3),
        })
        self.last_reject = None
        self._save_state()
        return True

    def prompt_fragment(self, max_chars: int = 800) -> str:
        """用于注入 system prompt 的自我笔记片段。"""
        notes = self.get_notes(max_chars)
        if not notes:
            return ""
        return f"## 自我笔记（随经历演化）\n{notes}"

    @scoped_llm("notes_compact", "compact")
    async def compact_notes(self, llm, max_keep: int = 300) -> bool:
        """压缩旧笔记，止住 self_notes.md 无限膨胀。"""
        text = self._persist.read_text(self._notes_path, "")
        if not text.strip():
            return False
        lines = text.split("\n")
        if len(lines) <= max_keep:
            return False

        keep_lines = lines[-max_keep:]
        compress_text = "\n".join(lines[:-max_keep])
        dropped = len(lines) - max_keep

        if len(compress_text.strip()) < 50:
            self._persist.write_text(self._notes_path, "\n".join(keep_lines).strip())
            self._log_event("notes_compacted", {"mode": "truncate", "dropped": dropped})
            logger.info("笔记压缩: 直接截断 %d 行（前段过短）", dropped)
            return True

        prompt = (
            "下面是某 AI 角色过去一段时间写的「自我反思笔记」（按时间顺序，每行一条，"
            "带时间戳，可能包含此前已压缩的[历史反思摘要]）。请将其合并压缩成一段精简的"
            "「历史反思摘要」（中文，200 字以内）：保留关键人格演化、重要感受、反复出现的"
            "主题，丢弃重复碎碎念。\n\n"
            f"{compress_text[:3000]}\n\n只输出摘要文本，不要标题、不要解释、不要代码块。"
        )
        try:
            summary = await llm.chat(
                [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=280
            )
            if not summary or not summary.strip():
                raise ValueError("empty summary")
            header = "[历史反思摘要] " + summary.strip()
            new_text = header + "\n" + "\n".join(keep_lines).strip()
            self._persist.write_text(self._notes_path, new_text)
            self._log_event("notes_compacted", {
                "mode": "llm_summary", "summary_len": len(summary), "dropped": dropped,
            })
            logger.info("笔记压缩: LLM 摘要 %d 行 → 保留 %d 行", dropped, max_keep)
            return True
        except Exception:
            logger.exception("笔记压缩 LLM 调用失败，退化为截断")
            self._persist.write_text(self._notes_path, "\n".join(keep_lines).strip())
            self._log_event("notes_compacted", {"mode": "truncate_fallback", "dropped": dropped})
            return True

    @scoped_llm("rumination", "reflect")
    async def ruminate(self, llm, recent_context: str) -> Optional[str]:
        """反思：基于最近对话生成一条自我洞察。"""
        if self.is_rumination_paused():
            logger.info("反刍已暂停至 %s，跳过", self.rumination_paused_until)
            return None
        if not recent_context or len(recent_context) < 50:
            return None
        if not self.can_afford_any(0.05):
            return None

        prompt = f"""基于以下最近对话，生成一条简短的自我反思（1-2 句话，中文）：

最近对话摘要：
{recent_context[:800]}

反思（不要用「作为 AI」开头，直接说你的感受/想法）：
注意：不要复述你已经反复说过、或早已写进自我笔记的已知结论；只写这次对话让你新想到的、或与以往不同的洞察。
注意：反思用哈基米自己的日常口语写，不要使用「纠偏 / 去污染 / 机制 / 召回 / 检索 / 自生长」这类工程黑话或元认知术语——就写你真实的感受和对人的看法。
注意：绝不写社交策略分析——不要用「拟人化 / 情绪价值 / 以柔克刚 / 顺毛 / 策略 / 边界 / 平衡 / 人设 / 角色扮演 / 化解冲突」这类分析腔词，也不要把自己当研究对象做「我需要…我应该…这说明…」式的自我解剖。你就是一只猫，写两句这次聊天让你高兴/烦/想起啥了就行。"""

        try:
            messages = [{"role": "user", "content": prompt}]
            result = await llm.chat(messages, temperature=0.7, max_tokens=120)
            if result and len(result) > 5:
                if await self._reflection_is_duplicate(llm, result):
                    logger.info(
                        "反思语义去重：跳过重复洞察（text_len=%d，不消耗预算）",
                        len(result),
                    )
                    return None
                if self.add_note(result, delta=0.05):
                    logger.info("反思已保存（text_len=%d）", len(result))
                    return result
        except Exception:
            logger.exception("反思 LLM 调用失败")

        return None

    # ------------------------------------------------------------------
    # 反思语义去重
    # ------------------------------------------------------------------

    def _recent_note_texts(self, n: int = 15) -> list[str]:
        """取最近 n 条自我笔记的正文（剥离时间戳与【自我修正】前缀）。"""
        text = self._persist.read_text(self._notes_path, "")
        if not text.strip():
            return []
        out: list[str] = []
        for line in reversed(text.split("\n")):
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and "]" in line:
                line = line.split("]", 1)[1].strip()
            if line.startswith("【自我修正】"):
                line = line[len("【自我修正】"):].strip()
            if line:
                out.append(line)
            if len(out) >= n:
                break
        return out

    @scoped_llm("rumination", "deduplicate")
    async def _reflection_is_duplicate(self, llm, candidate: str) -> bool:
        """判断候选反思是否与最近笔记在核心洞察上语义重复。"""
        recent = self._recent_note_texts(15)
        if not recent:
            return False
        if any(text_similarity(candidate, r) >= 0.72 for r in recent):
            return True
        joined = "\n".join(f"- {r[:80]}" for r in recent)
        prompt = (
            "下面是你最近的自我反思笔记：\n"
            f"{joined}\n\n"
            f"新生成的反思：{candidate}\n\n"
            "如果这条新反思与某条已有笔记在『核心洞察 / 想表达的结论』上语义重复"
            "（只是换了个说法、换了例子），只回复 DUPLICATE；否则只回复 UNIQUE。"
        )
        try:
            out = (await llm.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=20
            ) or "").strip().upper()
            return out == "DUPLICATE"
        except Exception:
            logger.exception("反思语义去重 LLM 调用失败")
            return False

    # ------------------------------------------------------------------
    # 漂移闸门
    # ------------------------------------------------------------------

    def check_drift(self, delta: float, category: str = "general") -> bool:
        """检查人格漂移是否在许可范围内；通过则记账（日池 + 7 日窗）。

        category == "self_correction" → 走 self_correction 池，否则 general。
        失败写 last_reject.reason ∈ {delta_max, daily_budget, window_ceiling}。
        """
        self._reset_daily()
        if delta > self.delta_max + 1e-12:
            self._record_reject(
                "delta_max", category=category, delta=delta, delta_max=self.delta_max,
            )
            return False

        if category == "self_correction":
            spent, budget = self.self_correction_spent, self.self_correction_daily_budget
        else:
            spent, budget = self.daily_spent, self.daily_budget
        if spent + delta > budget + 1e-9:
            self._record_reject(
                "daily_budget",
                category=category,
                spent=round(spent, 4),
                delta=delta,
                budget=budget,
            )
            return False

        w_spent = self.window_spent()
        if w_spent + delta > self.window_ceiling + 1e-9:
            self._record_reject(
                "window_ceiling",
                category=category,
                window_spent=round(w_spent, 4),
                delta=delta,
                window_ceiling=self.window_ceiling,
            )
            return False

        if category == "self_correction":
            self.self_correction_spent += delta
        else:
            self.daily_spent += delta
        self._window_events.append([time.time(), float(delta)])
        self._save_state()
        return True

    def can_afford_any(self, delta: float = 0.05) -> bool:
        """任一预算池还有余量且 7 日窗未满即可生成（供 ruminate 预检）。"""
        self._reset_daily()
        if self.window_spent() + delta > self.window_ceiling + 1e-9:
            return False
        if delta > self.delta_max + 1e-12:
            return False
        return (self.daily_spent + delta <= self.daily_budget) or \
               (self.self_correction_spent + delta <= self.self_correction_daily_budget)

    def _classify(self, content: str) -> str:
        """启发式判断是否为「自我修正」类笔记（保守匹配）。"""
        for kw in self._SC_KEYWORDS:
            if kw in content:
                return "self_correction"
        return "general"

    _SC_KEYWORDS = (
        "太频繁", "过度", "不该", "改掉", "纠正", "我应该少", "少提",
        "克制", "收敛", "反省", "话太多", "太啰嗦", "我太", "我错了",
        "意识到", "反思自己", "纠正自己", "我的问题", "我的毛病",
    )

    def _reset_daily(self) -> None:
        now = time.time()
        if now - self.day_start > 86400:
            self.daily_spent = 0.0
            self.self_correction_spent = 0.0
            self.day_start = now

    # ------------------------------------------------------------------
    # 内容闸门：粘滞 / 价态 / 身份 / 相似
    # ------------------------------------------------------------------

    @staticmethod
    def _is_sticky(text: str) -> bool:
        try:
            from anima.cognitive.ops_cleanup import is_sticky_text
            return bool(is_sticky_text(text))
        except Exception:
            return False

    @staticmethod
    def estimate_valence(text: str) -> float:
        """启发式情绪价态 ∈ 约 [-1, 1]；单次轻量情绪词落在可漂移区间内。

        设计：1 个正/负词 ≈ ±0.25，仅堆叠极端词才触顶被 valence_min/max 拦截。
        """
        if not text:
            return 0.0
        pos = sum(1 for w in _VALENCE_POS if w in text)
        neg = sum(1 for w in _VALENCE_NEG if w in text)
        if pos == 0 and neg == 0:
            return 0.0
        raw = (pos - neg) * 0.25
        return max(-1.0, min(1.0, raw))

    @staticmethod
    def _is_identity_hostile(text: str) -> bool:
        return any(p.search(text) for p in _IDENTITY_HOSTILE)

    def _jaccard_floor(self) -> float:
        """设计 cos 阈值 → 字符 Jaccard 工作阈值（0.75 cos ≈ 0.12–0.18 jaccard）。"""
        return max(0.06, min(0.28, float(self.similarity_floor) * 0.18))

    def _load_persona_anchor(self) -> str:
        if self._persona_anchor is not None:
            return self._persona_anchor
        anchor = "哈基米 猫娘 本喵 不是AI助手"
        try:
            from anima.cognitive.persona_checkpoint import read_persona_prompt
            prompt = (read_persona_prompt() or "").strip()
            if prompt:
                # 取前段稳定身份描述，避免整份 prompt 稀释 n-gram
                anchor = (prompt[:600] + " " + anchor).strip()
        except Exception:
            pass
        self._persona_anchor = anchor
        return anchor

    def set_persona_anchor(self, text: str) -> None:
        """测试钩子：注入人格锚点文本。"""
        self._persona_anchor = text or ""

    def _similarity_ok(self, text: str) -> bool:
        """身份向笔记须相对人格锚点达到 Jaccard 工作阈值；日常笔记直接放行。"""
        if not _IDENTITY_SCOPE.search(text):
            return True
        anchor = self._load_persona_anchor()
        if not anchor.strip():
            return True
        sim = text_similarity(text, anchor)
        floor = self._jaccard_floor()
        if sim + 1e-9 >= floor:
            return True
        # 与最近笔记任一条足够近 → 视为连续自我叙事，放行
        for recent in self._recent_note_texts(8):
            if text_similarity(text, recent) >= floor:
                return True
        return False

    def _log_event(self, event_type: str, data: dict) -> None:
        entry = {"time": time.time(), "type": event_type, "data": data}
        current = self._persist.read_text(self._log_path, "")
        self._persist.write_text(
            self._log_path,
            f"{current}{json.dumps(entry, ensure_ascii=False)}\n",
        )


# 全局单例（按 data_dir 绑定；路径变化时重建，避免测试/多实例串状态）
_learning: Optional[SelfLearning] = None
_learning_data_dir: Optional[str] = None


def get_learning(data_dir: str = "data") -> SelfLearning:
    global _learning, _learning_data_dir
    key = str(data_dir)
    if _learning is None or _learning_data_dir != key:
        _learning = SelfLearning(key)
        _learning_data_dir = key
    return _learning


def reset_learning_singleton() -> None:
    """测试钩子：清空单例。"""
    global _learning, _learning_data_dir
    _learning = None
    _learning_data_dir = None
