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

logger = logging.getLogger("anima.cognitive.life")

TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# D2: 生命里程碑
# ---------------------------------------------------------------------------

class MilestoneTracker:
    """记录哈基米的关键生命事件。"""

    def __init__(self, data_dir: str = "data"):
        self._persist = PersistenceProvider(data_dir)
        self._path = "milestones.json"
        self._milestones: list[dict] = []
        self._restore()

    def _restore(self) -> None:
        data = self._persist.read_json(self._path, {"milestones": []})
        self._milestones = data.get("milestones", [])

    def persist(self) -> None:
        self._persist.write_json(self._path, {"milestones": self._milestones[-50:]})

    def record(self, event: str, kind: str = "relation") -> None:
        """记录一条里程碑。"""
        # 去重（同类型事件 24h 内不重复）
        now = time.time()
        for m in self._milestones:
            if m["event"] == event and (now - m["ts"]) < 86400:
                return
        self._milestones.append({"event": event, "kind": kind, "ts": now})
        self.persist()
        logger.info("里程碑: %s", event)

    def get_recent(self, n: int = 5) -> list[dict]:
        """获取最近 n 条里程碑。"""
        return self._milestones[-n:]

    def fragment(self) -> str:
        """生成注入 prompt 的里程碑片段。"""
        recent = self.get_recent(3)
        if not recent:
            return ""
        lines = ["## 你的生命里程碑"]
        for m in recent:
            days_ago = int((time.time() - m["ts"]) / 86400)
            if days_ago == 0:
                when = "今天"
            elif days_ago == 1:
                when = "昨天"
            else:
                when = f"{days_ago}天前"
            lines.append(f"- {when}：{m['event']}")
        return "\n".join(lines)

    def days_since_first(self, user_id: int) -> int:
        """获取与某用户第一次交互距今的天数。"""
        for m in self._milestones:
            if str(user_id) in m.get("event", "") and m["kind"] == "first_contact":
                return max(1, int((time.time() - m["ts"]) / 86400))
        return 0


_milestone_tracker: MilestoneTracker | None = None


def get_milestone_tracker(data_dir: str = "data") -> MilestoneTracker:
    global _milestone_tracker
    if _milestone_tracker is None:
        _milestone_tracker = MilestoneTracker(data_dir)
    return _milestone_tracker


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
        self._last_goodnight = state.get("last_goodnight", 0.0)
        self._flow_count = state.get("flow_count", 0)

    def _save_state(self) -> None:
        self._persist.write_json(self._state_path, {
            "last_morning": self._last_morning,
            "last_evening": self._last_evening,
            "last_flow": self._last_flow,
            "last_goodnight": getattr(self, "_last_goodnight", 0.0),
            "flow_count": self._flow_count,
        })

    def _same_day(self, ts: float) -> bool:
        if ts == 0:
            return False
        t = datetime.fromtimestamp(ts, TZ)
        now = datetime.now(TZ)
        return t.date() == now.date()

    # ------------------------------------------------------------------
    # B1: 日记回顾辅助方法
    # ------------------------------------------------------------------

    def _recent_diary_entries(self, n: int = 5) -> list[str]:
        """B1: 读取 diary.md 最后 n 条记录（按 ## YYYY-MM-DD 分割）。"""
        current = self._persist.read_text(self._diary_path, "")
        if not current:
            return []
        # 按 ## 日期分割
        import re
        parts = re.split(r'(?=^## \d{4}-\d{2}-\d{2})', current, flags=re.MULTILINE)
        # 过滤空条目，取最后 n 条
        entries = [p.strip() for p in parts if p.strip() and p.strip().startswith("##")]
        return entries[-n:]

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

        # M1: 特殊日期/时段感知
        now = datetime.now(TZ)
        weekday = now.weekday()  # 0=周一, 6=周日
        date_context = ""
        if weekday == 0:
            date_context = "\n\n又是周一…你有点不想起。"
        elif weekday >= 5:  # 周六/周日
            date_context = "\n\n今天是周末，你比平时更放松，可能赖床到更晚。"
        # 简单节假日检测（月-日）
        _HOLIDAYS = {(1, 1): "元旦", (2, 14): "情人节", (12, 25): "圣诞节"}
        holiday = _HOLIDAYS.get((now.month, now.day))
        if holiday:
            date_context += f"\n\n今天是{holiday}，群里可能会有节日氛围。"

        # B1: 日记回顾——摘取最近 2 条日记注入晨间 prompt
        recent_diary = self._recent_diary_entries(2)
        diary_recall = ""
        if recent_diary:
            diary_recall = "\n## 最近的日记\n" + "\n".join(recent_diary)

        # D1: 长期目标注入
        aspiration_text = ""
        try:
            aspiration_text = desire.aspiration_fragment()
            if aspiration_text:
                aspiration_text = f"\n{aspiration_text}"
        except Exception:
            pass

        # D2: 里程碑注入
        milestone_text = ""
        try:
            milestone_text = get_milestone_tracker(self._persist.data_dir).fragment()
            if milestone_text:
                milestone_text = f"\n{milestone_text}"
        except Exception:
            pass

        prompt = f"""现在是 {time_description()}，你刚醒来。{date_context}

## 你的日记
{notes if notes else "（还没有日记）"}
{diary_recall}

## 今天的想法
{desire_text}
{aspiration_text}{milestone_text}

来一段 2-3 句的晨间独白——你在想什么、今天想干什么。像人刚睡醒时自然的心声。"""

        try:
            messages = [{"role": "user", "content": prompt}]
            reflection = await self.core.llm.chat(messages, temperature=0.7, max_tokens=150)
            if reflection:
                learning.add_note(f"[晨间] {reflection}")
                world_model.add(reflection, category="belief", weight=0.45, source="morning_routine")
                logger.info("晨间反思已保存（text_len=%d）", len(reflection))
                # M3: 20% 概率向最近活跃的群发一句早安
                if random.random() < 0.2 and hasattr(self.core, "propose_agenda"):
                    try:
                        from anima.cognitive.group_presence import get_group_presence
                        gp = get_group_presence(self._persist.data_dir)
                        active_groups = gp.get_active_groups(limit=1)
                        if active_groups:
                            gid = active_groups[0]
                            # 基于当天心情/星期的随口感叹
                            _greetings = [
                                "唔…周一…本喵的猫生需要重启",
                                "今天精神不错喵，早~",
                                "哈欠…本喵醒了喵",
                                "早啊…本喵刚睡醒，脑子还在开机",
                            ]
                            greeting = random.choice(_greetings)
                            self.core.propose_agenda(
                                int(gid), f"早安问候：{greeting}",
                                priority=0.35, expires_at=time.time() + 3600,
                                source="morning_greeting",
                            )
                            logger.info("早安议程已创建 group=%s", gid)
                    except Exception:
                        logger.debug("早安议程创建失败")
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
                # H2: 30% 概率将心流转化为低优先级 agenda（表达欲）
                if random.random() < 0.3 and hasattr(self.core, "propose_agenda"):
                    try:
                        from anima.cognitive.group_presence import get_group_presence
                        gp = get_group_presence(self._persist.data_dir)
                        active_groups = gp.get_active_groups(limit=1)
                        if active_groups:
                            gid = active_groups[0]
                            self.core.propose_agenda(
                                int(gid),
                                f"你突然想到：{thought}。随口说一句就行。",
                                priority=0.4,
                                expires_at=time.time() + 2 * 3600,
                                source="flow_thought",
                            )
                            logger.info("心流表达议程已创建 group=%s", gid)
                    except Exception:
                        logger.debug("心流表达议程创建失败")
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

        # B1: 晚间反思中对比“早上想做什么”和“今天实际经历了什么”
        morning_intent = ""
        if self._morning_done and notes:
            # 提取晨间独白中的意图
            for line in notes.split("\n"):
                if "[晨间]" in line:
                    morning_intent = line.split("[晨间]")[-1].strip()
                    break

        prompt = f"""现在是深夜，你在写今天的日记。回顾一下今天发生了什么、你有什么感受。

## 今天的对话片段
{today_snippets if today_snippets else "（今天没怎么聊天）"}

## 之前的日记
{notes[-800:] if notes else "（还没有）"}

{f'## 今天早上的想法\n{morning_intent}\n' if morning_intent else ''}写一段 3-5 句的晚间日记——今天有什么有趣的？学到了什么？对谁有了新的认识？{f'和早上想的比起来，今天过得怎么样？' if morning_intent else ''}"""

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
                # S2: 晚间反思中回顾当天未满足的欲望
                try:
                    from anima.cognitive.desire import get_desire_system
                    desire_sys = get_desire_system(self._persist.data_dir)
                    regrets = desire_sys.expire_unfulfilled()
                    if regrets:
                        regret_text = "\n".join(regrets)
                        learning.add_note(f"[遗憾 {date_str}] {regret_text}")
                        logger.info("欲望落空笔记: %s", regret_text[:100])
                except Exception:
                    pass
                return diary
        except Exception:
            logger.exception("晚间反思失败")
        return None

    # ------------------------------------------------------------------
    # M2: 晚安/告别行为
    # ------------------------------------------------------------------

    async def goodnight_behavior(self) -> bool:
        """晚安行为（23:00-00:00 触发，每天一次）。

        若最近 1 小时内有群消息活跃，30% 概率通过 agenda 发一句晚安。
        """
        hour = _now_hour()
        if not (23 <= hour <= 23):  # 只在 23 点触发
            return False
        if self._same_day(getattr(self, "_last_goodnight", 0.0)):
            return False

        self._last_goodnight = time.time()
        self._save_state()

        if random.random() > 0.3:
            return False

        try:
            from anima.cognitive.group_presence import get_group_presence
            gp = get_group_presence(self._persist.data_dir)
            # 检查最近 1 小时内是否有活跃群
            active_groups = gp.get_active_groups(limit=1, within_sec=3600)
            if not active_groups:
                # 没有活跃群，静默入睡（只写日记）
                from anima.cognitive.learning import get_learning
                learning = get_learning(self._persist.data_dir)
                learning.add_note("[晚安] 群里都安静了，本喵也去睡了喵…")
                return False

            gid = active_groups[0]
            _goodnights = [
                "好啦本喵要去睡了…你们也别太晚喵",
                "困了…本喵先睡一步喵…",
                "哈欠…本喵撑不住了，晚安喵…",
            ]
            goodnight = random.choice(_goodnights)
            if hasattr(self.core, "propose_agenda"):
                self.core.propose_agenda(
                    int(gid), f"晚安问候：{goodnight}",
                    priority=0.3, expires_at=time.time() + 1800,
                    source="goodnight",
                )
                logger.info("晚安议程已创建 group=%s", gid)
                return True
        except Exception:
            logger.debug("晚安行为失败")
        return False


# 全局单例
_simulated_life: Optional[SimulatedLife] = None


def get_life(core=None, data_dir="data") -> Optional[SimulatedLife]:
    global _simulated_life
    if _simulated_life is None and core is not None:
        _simulated_life = SimulatedLife(core, data_dir)
    return _simulated_life


def recent_activity_hint(data_dir: str = "data") -> str:
    """返回最近内心活动的一句话描述，供注入 system prompt 营造“连续存在”感。

    规则：
    - 只在最近 30 分钟内有活动时输出
    - 概率性触发（40%），避免每次都提
    - 返回空字符串表示“无需注入”
    """
    import random as _rnd
    life = _simulated_life
    if life is None:
        return ""
    now = time.time()
    hour = _now_hour()

    # 深夜/凌晨：休眠状态
    if 0 <= hour < 7:
        if _rnd.random() < 0.4:
            return "你刚在打盹，被叫醒了，还有点迷糊。"
        return ""

    # 检查最近心流活动
    flow_ts = getattr(life, "_last_flow", 0.0) or 0.0
    flow_age = now - flow_ts if flow_ts > 0 else 99999
    if flow_age < 1800:  # 30 分钟内
        if _rnd.random() < 0.4:
            # 尝试读取最近的心流笔记
            try:
                from anima.cognitive.learning import get_learning
                learning = get_learning(data_dir)
                notes = learning.get_notes(200)
                for line in reversed(notes.splitlines()):
                    if "[心流]" in line:
                        thought = line.split("]", 1)[-1].strip()[:40]
                        if thought:
                            return f"你刚在发呆想事情（{thought}），被拉回来了。"
            except Exception:
                pass
            return "你刚在发呆，脑子还在飘着。"

    # 检查晨间反思
    morning_ts = getattr(life, "_last_morning", 0.0) or 0.0
    morning_age = now - morning_ts if morning_ts > 0 else 99999
    if morning_age < 1800 and _rnd.random() < 0.3:
        return "你刚睡醒在整理昨天的事，还有点困。"

    return ""
