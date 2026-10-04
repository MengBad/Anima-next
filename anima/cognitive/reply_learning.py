"""被动回复效果自学习 —— 记录「开口之后有没有人接」，归纳成经验事实。

## 为什么需要它

`core/autonomy.py` 的 `_active_feedback_pending` 已经给**主动发言**建了完整回路：
挂 pending → 窗口内有人接记 engaged / 无人理记 ignored → Beta 平滑 → 降频/冷落。
但**被动回复**（被叫到、熟人插话、唤醒窗内接话）**零采集点**——闸门
（`llm/group_gate.py`）只读**静态** `familiarity` 阈值，开口效果永不回流到
「该不该回」的判定。这个模块补的就是这条断线。

严格分两步：

* **B1（本次）＝ 只采集**：挂 pending、记 engaged/ignored、落盘、归纳、注入。
  **不碰闸门、不改任何开口行为** ⇒ 零行为风险。先跑一周拿真数字。
* **B2（未做）**：等数字出来，再决定 `familiar_min` 要不要从静态值改成
  「基线 ± 群冷场惩罚」。没有真数字就调阈值 = 拍脑袋。

## 设计对齐既有约定

形状照抄已验证在生产跑的 `cognitive/tool_learning.py`（同款三点闭环）：

1. **采集** —— `llm/handler.py` 群聊出站成功后 `mark_reply()`（成功才记，失败不算样本）
2. **观察** —— `core/autonomy.py:count_message`（memory 层群消息回调，**天然在回复闸门
   之前**）⇒ 与主动发言共用同一个观察点，不新增回调
3. **归纳** —— `cognitive/rumination.py:_self_growth_dispatch` 现成心跳（每 1800s 节流）
4. **注入** —— `pipeline/stages/cognition.py` 的 `add_system_fragment`（tool_learning 隔壁）

## 🔴 口径（必须知道，否则会误读数字）

`engaged` = **窗口内群里又出现了人类消息**，**不等于**「有人回应它」。热闹的群
哪怕那条消息与它无关，也会记 engaged。这是刻意选的最粗口径 —— 零成本、零误判
风险，且足以区分「这个群开口时有人在 vs 冷清」。

要精确到「有人回应它」需要语义判定（@/引用检测或相似度），**留给 B2**，别在这里
偷偷加。三处已核实的事实（`memory/chat_memory.py`）：

* 回调只在**入站人类消息**触发（`:273` 有 `role == "user"` 守卫）；
* bot 自己的出站走 `observe_group_bot_reply`，**不触发回调** ⇒ 不会「自己接自己」；
* 回调在**回复闸门之前**（`:353` 注释原文：「保证被闸门跳过的消息仍保留在对话记忆中」）
  ⇒ 被拦下的消息同样算「群里有人说话」，这与它自己有没有被允许回复无关。

## 一条硬约束：写「经验事实」，不写「社交策略」

`cognitive/learning.py` 的 `ruminate()` prompt 里硬编码着「**绝不写社交策略分析**」
（防「分析腔」露馅，与 09-28 人视角审计的露馅清单同源）。那条禁令**是对的**——
所以这里**绕开它、不推翻它**：

* ✅ 允许：「被叫到时开口，12 次里 9 次有人接着聊」
* ❌ 禁止：「我应该更克制，收敛插话频率」

`fragment()` 内置 fail-closed 过滤：命中策略腔的笔记**整条不注入**。
"""

from __future__ import annotations
import json
import logging
import re
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.reply_learning")

#: 触发场景白名单 —— 只分「被叫到」与「其他」，防任意字符串撑爆磁盘。
_TRIGGER_AT = "at"
_TRIGGER_OTHER = "other"
_VALID_TRIGGERS = frozenset({_TRIGGER_AT, _TRIGGER_OTHER})

#: 🔴 策略腔 / 自我解剖黑名单。命中即整条丢弃（fail-closed）。
#: 这些词正是 `learning.ruminate()` 明令禁止的「分析腔」——
#: 上游模型万一漏出来，绝不让它进 system。
_STRATEGY_RE = re.compile(
    r"我应该|我需要|我要更|我必须|得学会|要克制|该收敛|收敛点|策略|人设|"
    r"情绪价值|以柔克刚|顺毛|这说明|自我解剖|边界感|平衡感"
)


class ReplyLearning:
    """被动回复效果自学习管理器（B1：只采集，不改行为）。"""

    def __init__(self, data_dir: str = "data", window_sec: int = 300,
                 records_max: int = 200, inject_top_k: int = 3,
                 min_samples: int = 5):
        self._persist = PersistenceProvider(data_dir)
        self._path = "reply_learning.json"
        self.window_sec = max(30, int(window_sec))
        self.records_max = max(1, int(records_max))
        self.inject_top_k = max(1, int(inject_top_k))
        self.min_samples = max(1, int(min_samples))
        self._data = self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        data = self._persist.read_json(self._path, {})
        if not isinstance(data, dict):
            data = {}
        for key, default in (("pending", {}), ("stats", {}), ("notes", {}),
                             ("summarized", {})):
            if not isinstance(data.get(key), dict):
                data[key] = default
        return data

    def _save(self) -> None:
        self._persist.write_json(self._path, self._data)

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------

    def mark_reply(self, group_id: int, trigger: str = "", message_id: str = "",
                   now: Optional[float] = None) -> None:
        """被动回复出站成功时调用：挂一个「等待被接话」的窗口。

        若同群已有未结算的旧 pending，先把它结算为 engaged —— 窗口内既然又来了
        消息（就是这次触发回复的那条），说明上一次开口确实被接住了。
        """
        now = time.time() if now is None else float(now)
        gid = str(int(group_id))
        self.observe(int(group_id), now=now)      # 结算旧 pending（若有）
        self._data["pending"][gid] = {
            "sent_at": now,
            "trigger": self._norm_trigger(trigger),
            "message_id": str(message_id or ""),
        }
        self._save()
        logger.debug("被动回复采集已挂载 group=%s trigger=%s", gid,
                     self._data["pending"][gid]["trigger"])

    def observe(self, group_id: int, now: Optional[float] = None) -> None:
        """窗口内群里有新消息 ⇒ 上一次开口被接话。

        🔴 由 `core/autonomy.py:count_message` 调用 —— 那是 memory 层的群消息回调，
        **在回复闸门之前**，所以「被拦下的消息」同样算数（对 bot 而言群里确实有人
        说话了，与它自己有没有被允许回复无关）。
        """
        now = time.time() if now is None else float(now)
        gid = str(int(group_id))
        pending = self._data["pending"].get(gid)
        if not pending:
            return
        sent_at = float(pending.get("sent_at", 0) or 0)
        if sent_at <= 0 or now - sent_at > self.window_sec:
            return                                # 过期了，交给 expire() 结算
        self._bump(gid, str(pending.get("trigger") or _TRIGGER_OTHER), "engaged")
        self._data["pending"].pop(gid, None)
        self._save()

    def expire(self, now: Optional[float] = None) -> None:
        """窗口过了仍无人理 ⇒ ignored。由心跳（`autonomy._tick`）驱动。"""
        now = time.time() if now is None else float(now)
        expired = [gid for gid, p in self._data["pending"].items()
                   if float(p.get("sent_at", 0) or 0) > 0
                   and now - float(p.get("sent_at", 0) or 0) >= self.window_sec]
        if not expired:
            return
        for gid in expired:
            pending = self._data["pending"].pop(gid)
            self._bump(gid, str(pending.get("trigger") or _TRIGGER_OTHER), "ignored")
        self._save()

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_trigger(trigger: str) -> str:
        t = str(trigger or "").strip().lower()
        return t if t in _VALID_TRIGGERS else _TRIGGER_OTHER

    def _bucket(self, gid: str) -> dict:
        return self._data["stats"].setdefault(gid, {
            "engaged": 0, "ignored": 0, "by_trigger": {},
        })

    def _bump(self, gid: str, trigger: str, outcome: str) -> None:
        st = self._bucket(gid)
        st[outcome] = int(st.get(outcome, 0) or 0) + 1
        bt = st.setdefault("by_trigger", {}).setdefault(
            self._norm_trigger(trigger), {"engaged": 0, "ignored": 0})
        bt[outcome] = int(bt.get(outcome, 0) or 0) + 1
        audit = getattr(self, "_audit_hook", None)
        if audit is not None:
            try:
                audit(gid, trigger, outcome)
            except Exception:
                logger.debug("被动回复效果审计失败", exc_info=True)

    def stats(self, group_id: int) -> dict:
        st = self._data["stats"].get(str(int(group_id)))
        if not st:
            return {"engaged": 0, "ignored": 0, "by_trigger": {}}
        return {
            "engaged": int(st.get("engaged", 0) or 0),
            "ignored": int(st.get("ignored", 0) or 0),
            "by_trigger": {k: dict(v) for k, v in (st.get("by_trigger") or {}).items()},
        }

    def _samples(self, group_id: int) -> int:
        st = self.stats(group_id)
        return st["engaged"] + st["ignored"]

    def pending_count(self) -> int:
        return len(self._data["pending"])

    def snapshot(self) -> dict:
        """供 WebUI / 审计读取的只读快照。"""
        return {
            "groups": {gid: self.stats(int(gid)) for gid in self._data["stats"]},
            "pending": len(self._data["pending"]),
            "notes": sum(len(v) for v in self._data["notes"].values()),
        }

    def notes(self, group_id: int) -> list[dict]:
        return list(self._data["notes"].get(str(int(group_id))) or [])

    # ------------------------------------------------------------------
    # 归纳（LLM）—— 走 rumination 心跳
    # ------------------------------------------------------------------

    def summarize_due_groups(self) -> list[int]:
        """样本够（≥ min_samples）且这批样本还没归纳过 ⇒ 待归纳的群。"""
        due = []
        for gid in self._data["stats"]:
            samples = self._samples(int(gid))
            if samples < self.min_samples:
                continue
            if self._data.get("summarized", {}).get(str(gid)) == samples:
                continue
            due.append(int(gid))
        return due

    def _facts_text(self, group_id: int) -> str:
        """把计数渲染成给 LLM 看的「事实」列表（不含任何评价）。"""
        st = self.stats(group_id)
        lines = []
        for trig, label in ((_TRIGGER_AT, "被叫到时开口"),
                            (_TRIGGER_OTHER, "其他时候开口")):
            b = st["by_trigger"].get(trig)
            if not b:
                continue
            total = b["engaged"] + b["ignored"]
            if total <= 0:
                continue
            lines.append(f"- {label}：{total} 次，其中 {b['engaged']} 次有人接着说")
        if not lines:
            return ""
        return "\n".join(lines)

    @scoped_llm("reply_learning", "summarize")
    async def summarize(self, llm, group_id: int) -> Optional[str]:
        """把「开口 → 有没有人接」的计数归纳成一条经验事实。

        返回写入的笔记文本，或 None（样本不足 / 上游空 / 解析失败）。
        """
        if self._samples(group_id) < self.min_samples:
            return None
        facts = self._facts_text(group_id)
        if not facts:
            return None
        prompt = (
            "下面是一只猫娘机器人在群里说话之后的记录（它自己看的事实，不是任务）：\n"
            f"{facts}\n\n"
            "请用一句话总结「它在什么情况下开口，群里更容易有人接着说」。\n"
            "🔴 只准描述观察到的事实，例如「被叫到时开口大多有人接」。\n"
            "🔴 禁止写任何建议或自我要求：不许出现「我应该」「我需要」「要克制」"
            "「收敛」「策略」「人设」这类词，也不要分析自己的性格或心理。\n"
            '只输出 JSON：{"note": "..."}'
        )
        try:
            text = await llm.chat([{"role": "user", "content": prompt}],
                                  temperature=0.3, max_tokens=120)
            if not text:
                return None
            text = text.strip()
            text = text.lstrip("`").replace("```json", "").replace("```", "").strip("`").strip()
            note = ""
            try:
                note = str((json.loads(text) or {}).get("note") or "").strip()
            except json.JSONDecodeError:
                # 上游偶尔无视 JSON 约束直接给一句话（tool_learning 同款容错）
                m = re.search(r'\{[^{}]*"note"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
                note = (m.group(1) if m else text.strip().strip('"').strip()).strip()
            if not note:
                logger.warning("被动回复心得无可用内容，已跳过 group=%s", group_id)
                return None
            if _STRATEGY_RE.search(note):
                # 不丢样本、不写坏笔记：记日志便于观察上游违规频率，但不入库。
                logger.warning("被动回复心得含策略腔，已丢弃 group=%s note=%r",
                               group_id, note[:80])
                self._data.setdefault("summarized", {})[str(int(group_id))] = \
                    self._samples(group_id)
                self._save()
                return None
            notes = self._data["notes"].setdefault(str(int(group_id)), [])
            notes.append({"text": note, "ts": time.time(),
                          "samples": self._samples(group_id)})
            del notes[:-self.records_max]
            self._data.setdefault("summarized", {})[str(int(group_id))] = \
                self._samples(group_id)
            self._save()
            logger.info("被动回复心得已更新 group=%s note_len=%d", group_id, len(note))
            return note
        except Exception:
            logger.exception("被动回复心得归纳失败")
            return None

    # ------------------------------------------------------------------
    # 注入片段
    # ------------------------------------------------------------------

    def fragment(self, group_id: Optional[int] = None) -> str:
        """生成注入 system 的「开口之后」片段。

        传 group_id ⇒ 只报该群；不传 ⇒ 报所有群里最新的几条。

        🔴 fail-closed：任何一条笔记命中策略腔黑名单就整条丢弃 —— 宁可少注入，
        绝不让「我应该更克制」这类分析腔进 system（那正是露馅清单里的病根）。
        """
        picked: list[str] = []
        if group_id is not None:
            groups = [str(int(group_id))]
        else:
            groups = list(self._data["notes"].keys())
        for gid in groups:
            for n in reversed(self._data["notes"].get(gid) or []):
                text = str(n.get("text") or "").strip()
                if not text or _STRATEGY_RE.search(text):
                    continue
                picked.append(text)
                break                          # 每群只取最新一条
        picked = picked[: self.inject_top_k]
        if not picked:
            return ""
        return ("## 本喵开口之后（自己记下来的）\n"
                + "\n".join(f"· {t}" for t in picked))


# ------------------------------------------------------------------
# 全局单例
# ------------------------------------------------------------------

_learning: Optional["ReplyLearning"] = None


def get_reply_learning(data_dir: str = "data", **kwargs) -> "ReplyLearning":
    global _learning
    if _learning is None:
        _learning = ReplyLearning(data_dir, **kwargs)
    return _learning


def close_reply_learning() -> None:
    global _learning
    _learning = None
