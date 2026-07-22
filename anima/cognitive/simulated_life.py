"""模拟生活 —— Anima 的「日常」：早晚自省、经历消化、主动追欲望。

日常节律：
- 00:00-06:00 休眠（几乎不说话）
- 07:00-09:00 醒来 → 看看昨晚错过了什么、今天的欲望
- 12:00 午间心流 → 随机的碎片思考
- 21:00-23:00 晚间反思 → 写日记、消化一天经历

持久化：日记与当日状态通过 PersistenceProvider 写入 data/。
"""

from __future__ import annotations
import logging
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.life")

TZ = timezone(timedelta(hours=8))


def _now_hour() -> int:
    return datetime.now(TZ).hour


class SimulatedLife:
    """模拟生活的日常管理。"""

    def __init__(self, core: "AnimaCore", data_dir: str = "data"):
        self.core = core
        self._persist = PersistenceProvider(data_dir)
        self._diary_path = "diary.md"
        self._state_path = "life_state.json"

        # 当日状态追踪
        self._last_morning = 0.0
        self._last_evening = 0.0
        self._last_flow = 0.0
        self._morning_done = False
        self._evening_done = False
        self._flow_count = 0

        self._restore_state()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        state = self._persist.read_json(self._state_path, {})
        self._last_morning = state.get("last_morning", 0.0)
        self._last_evening = state.get("last_evening", 0.0)
        self._last_flow = state.get("last_flow", 0.0)
        self._flow_count = state.get("flow_count", 0)

    def _save_state(self) -> None:
        self._persist.write_json(self._state_path, {
            "last_morning": self._last_morning,
            "last_evening": self._last_evening,
            "last_flow": self._last_flow,
            "flow_count": self._flow_count,
        })

    def _same_day(self, ts: float) -> bool:
        if ts == 0:
            return False
        t = datetime.fromtimestamp(ts, TZ)
        now = datetime.now(TZ)
        return t.date() == now.date()

    # ------------------------------------------------------------------
    # 晨间觉醒
    # ------------------------------------------------------------------

    @scoped_llm("simulated_life", "morning")
    async def morning_routine(self) -> Optional[str]:
        """晨间反思（7-10 点，每天一次）。"""
        hour = _now_hour()
        if not (7 <= hour <= 10):
            return None
        if self._same_day(self._last_morning):
            return None

        self._last_morning = time.time()
        self._morning_done = True
        self._save_state()

        if self.core.llm is None:
            return None

        from anima.cognitive.desire import get_desire_system
        from anima.cognitive.learning import get_learning
        from anima.cognitive.time_sense import time_description
        from anima.cognitive.world_model import get_world_model

        desire = get_desire_system()
        learning = get_learning(self._persist.data_dir)
        world_model = get_world_model(self._persist.data_dir)
        notes = learning.get_notes(1500)

        active_desires = desire.get_active(0.3)
        desire_text = "\n".join(f"- {d.content}" for d in active_desires[:5]) or "（暂无特别想做的）"

        prompt = f"""现在是 {time_description()}，你刚醒来。

## 你的日记
{notes if notes else "（还没有日记）"}

## 今天的想法
{desire_text}

来一段 2-3 句的晨间独白——你在想什么、今天想干什么。像人刚睡醒时自然的心声。"""

        try:
            messages = [{"role": "user", "content": prompt}]
            reflection = await self.core.llm.chat(messages, temperature=0.7, max_tokens=150)
            if reflection:
                learning.add_note(f"[晨间] {reflection}")
                world_model.add(reflection, category="belief", weight=0.45, source="morning_routine")
                logger.info("晨间反思已保存（text_len=%d）", len(reflection))
                return reflection
        except Exception:
            logger.exception("晨间反思失败")
        return None

    # ------------------------------------------------------------------
    # 碎片心流
    # ------------------------------------------------------------------

    @scoped_llm("simulated_life", "flow")
    async def flow_thought(self) -> Optional[str]:
        """午间碎片思考（11-17 点，随机触发）。"""
        hour = _now_hour()
        if not (11 <= hour <= 17):
            return None
        # 每 2 小时最多一次
        if time.time() - self._last_flow < 7200:
            return None
        # 30% 概率
        if random.random() > 0.3:
            return None

        self._last_flow = time.time()
        self._flow_count += 1
        self._save_state()

        if self.core.llm is None:
            return None

        from anima.cognitive.learning import get_learning
        from anima.cognitive.world_model import get_world_model
        learning = get_learning(self._persist.data_dir)
        world_model = get_world_model(self._persist.data_dir)
        notes = learning.get_notes(800)

        prompt = "你闲着没事，脑子里飘过一个念头。写 1 句话——可能是对最近某件事的想法，或者突然想做的事。自然随意一点。"
        if notes:
            prompt += f"\n\n你最近的笔记：\n{notes[-500:]}"

        try:
            messages = [{"role": "user", "content": prompt}]
            thought = await self.core.llm.chat(messages, temperature=0.9, max_tokens=80)
            if thought and len(thought) > 5:
                learning.add_note(f"[心流] {thought}")
                world_model.add(thought, category="belief", weight=0.35, source="flow_thought")
                logger.info("碎片思考已保存（text_len=%d）", len(thought))
                return thought
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 晚间反思
    # ------------------------------------------------------------------

    @scoped_llm("simulated_life", "evening")
    async def evening_reflection(self) -> Optional[str]:
        """晚间日记（21-24 点，每天一次）。"""
        hour = _now_hour()
        if not (21 <= hour <= 23):
            return None
        if self._same_day(self._last_evening):
            return None

        self._last_evening = time.time()
        self._evening_done = True
        self._save_state()

        if self.core.llm is None:
            return None

        from anima.cognitive.learning import get_learning
        from anima.cognitive.world_model import get_world_model
        from anima.memory.chat_memory import get_chat_memory
        learning = get_learning(self._persist.data_dir)
        world_model = get_world_model(self._persist.data_dir)
        notes = learning.get_notes(2000)

        # 收集今天的对话片段
        memory = get_chat_memory()
        today_snippets = ""
        for key in list(memory._store.keys()):
            od = memory._store[key]
            for _, entries in list(od.items())[-3:]:
                for e in entries:
                    if e.get("role") == "user":
                        today_snippets += f"{e.get('name', '?')}: {e.get('content', '')[:80]}\n"
        today_snippets = today_snippets[-600:]

        prompt = f"""现在是深夜，你在写今天的日记。回顾一下今天发生了什么、你有什么感受。

## 今天的对话片段
{today_snippets if today_snippets else "（今天没怎么聊天）"}

## 之前的日记
{notes[-800:] if notes else "（还没有）"}

写一段 3-5 句的晚间日记——今天有什么有趣的？学到了什么？对谁有了新的认识？"""

        try:
            messages = [{"role": "user", "content": prompt}]
            diary = await self.core.llm.chat(messages, temperature=0.7, max_tokens=250)
            if diary and len(diary) > 10:
                date_str = datetime.now(TZ).strftime("%Y-%m-%d")
                learning.add_note(f"[日记 {date_str}] {diary}")
                world_model.add(diary, category="belief", weight=0.5, source="evening_reflection")
                # 追加到 diary.md
                current = self._persist.read_text(self._diary_path, "")
                self._persist.write_text(self._diary_path, f"{current}\n## {date_str}\n{diary}\n")
                logger.info("晚间日记已保存（text_len=%d）", len(diary))
                return diary
        except Exception:
            logger.exception("晚间反思失败")
        return None


# 全局单例
_simulated_life: Optional[SimulatedLife] = None


def get_life(core=None, data_dir="data") -> Optional[SimulatedLife]:
    global _simulated_life
    if _simulated_life is None and core is not None:
        _simulated_life = SimulatedLife(core, data_dir)
    return _simulated_life
