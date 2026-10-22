"""通用口头发话执行器（In-chat Request Executor）。

把 owner/被@ 的“一句话要求”在运行时识别成动作意图，直接调用现成工具落地，
并返回一句可发到 QQ 的确认回执。用关键词组做保守路由，避免每个新说法都要
打补丁——凡是落到现有能力（记忆/提醒/忽略/查询/任务）的要求，bot 自己办好。

意图：
- remember : 记住/别忘/记一下            → 写入口头要求存储
- remind   : X时间提醒我                 → calendar 真实提醒
- ignore   : 别再理那两人/别理X          → 忽略名单（群内，最近发言者或显式 qq）
- query    : 还记得吗/我之前说的/帮我查   → 回取口头要求存储
- do_task  : 有个问题/帮我修/帮我看看     → 建 Owner 任务，等 approve 执行
- stop     : 停/别做了/取消（2026-10-21）→ 取消本人在跑的多步 Agent 任务
- chat     : 其它                         → 走正常回复，不误动作

仅 owner 或被@ 的入站触发；未知意图一律回落到正常聊天。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

#: 🔴 2026-10-21：本模块此前**没有 logger**，`_stop` 的异常分支引用它会抛
#: NameError（把「取消失败」变成「代码崩溃」）。由测试
#: `test_stop_never_lies_when_cancel_fails` 抓出。
logger = logging.getLogger("anima.llm.request_executor")

_CQ = re.compile(r"\[CQ:[^\]]+\]", re.I)
_TIME = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")
_TIME_CN = re.compile(r"(\d{1,2})\s*(?:[点时])\s*(\d{1,2})?\s*分?")

_TRIGGER = {
    "remember": ("记住", "别忘", "记一下", "帮我记", "记好", "牢牢记", "谨记"),
    "remind": ("提醒我", "设个提醒", "定个闹钟", "几点提醒", "到点提醒", "叫我"),
    "ignore": ("别再理", "不要理", "别理", "冷落", "别鸟", "别再搭"),
    "query": ("还记得吗", "你还记得", "我上次说的", "我之前说的", "我们的关系"),  # 通用"帮我查"交还带工具的流水线
    # 🔴 2026-10-23 P2：长期目标。**必须排在 do_task 之前**
    #   `_TRIGGER` 是 dict，`classify_request_intent` 按插入顺序遍历命中即返回
    #   ⇒ 若放在 do_task 之后，「帮我盯着 X」会被 do_task 抢走，退化成一次性任务。
    #   边界：「帮我**盯着**」= goal（长期） / 「帮我**查一下**」= do_task（一次性）。
    "goal": ("帮我盯着", "帮我盯", "持续关注", "一直盯着", "长期跟进", "长期",
             "定期", "每周帮我", "每天帮我", "盯着", "盯一下", "盯一盯", "关注着",
             # 查看类（否则「我的目标」会被判成 chat，走不到 goal 分支）
             "我的目标", "在追什么", "追哪些", "目标列表", "有哪些目标"),
    "do_task": ("有个问题", "帮我修", "帮我查下代码", "看下日志"),  # 通用"帮我看看/帮我做"交还流水线
    # 🔴 2026-10-21：自然语言叫停。owner 原话「能不能不用命令」——
    #   既然启动是自然语言，叫停也必须能自然语言，否则等于承诺做不到。
    #   词表刻意**窄**（只收明确的停止说法）。实测踩过的误伤：
    #   「今天好累想停下来歇会」含「停下」⇒ 曾误判为叫停。
    #   故**去掉裸「停下」**，只保留带宾语/语气明确的说法。
    "stop": ("别做了", "不要做了", "别干了", "取消任务", "先别做",
             "停手", "算了别做", "停一下别做", "别做了吧"),
}


def classify_request_intent(text: str) -> str:
    """把一条入站文本映射到动作意图；无匹配返回 chat。"""
    t = _CQ.sub("", text or "").strip()
    if not t:
        return "chat"
    for intent, words in _TRIGGER.items():
        if any(w in t for w in words):
            return intent
    return "chat"


def _strip_trigger(content: str) -> str:
    t = _CQ.sub("", content or "").strip()
    for _intent, words in _TRIGGER.items():
        for w in words:
            if t.startswith(w):
                t = t[len(w):].lstrip("：:，,、。 ")
                return t
            if w in t:
                t = t.replace(w, " ", 1)
    return t


_DATE_ONCE_RE = re.compile(r"(大后天|后天|明天|明早|明晚)")
_PERIOD_RE = re.compile(r"(凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)")
_RECUR_RE = re.compile(r"(每天|每日)")
_POLITENESS_RE = re.compile(r"[，,]?\s*(可以吗|好吗|行吗|好不好|麻烦了|谢谢)[？?！!。~～]*\s*$")


def _once_date(text: str, now: float | None = None) -> "str | None":
    """明天/明早/明晚/后天/大后天 → 北京时间 YYYY-MM-DD；无一次性日期词返回 None。"""
    if not _DATE_ONCE_RE.search(text or ""):
        return None
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=8))
    base = datetime.fromtimestamp(time.time() if now is None else now, tz).date()
    if "大后天" in text:
        base += timedelta(days=3)
    elif "后天" in text:
        base += timedelta(days=2)
    else:  # 明天 / 明早 / 明晚
        base += timedelta(days=1)
    return base.isoformat()



class RequestExecutor:
    """一句话要求 → 落地动作 → 回执。data_dir 下 request_memory.json 存口头要求。"""

    def __init__(
        self,
        core,
        *,
        data_dir: str = "data",
        recent_speakers: Callable[[Any], list[int]] | None = None,
        ignore_store=None,
        store_cap: int = 20,
    ):
        self.core = core
        self.data_dir = data_dir
        self.recent_speakers = recent_speakers or (lambda gid: [])
        self.ignore_store = ignore_store
        self.store_cap = store_cap
        self._lock = threading.RLock()
        self._mem_path = Path(data_dir) / "request_memory.json"
        self._mem: dict[str, list[dict]] = self._load_mem()

    # ---- 口头要求存储 ----
    def _load_mem(self) -> dict[str, list[dict]]:
        try:
            d = json.loads(self._mem_path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {str(k): v for k, v in d.items() if isinstance(v, list)}
        except Exception:
            pass
        return {}

    def _save_mem(self) -> None:
        try:
            self._mem_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._mem_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._mem, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._mem_path)
        except Exception:
            pass

    # ---- 入口 ----
    async def handle(
        self,
        text: str,
        *,
        user_id: int,
        group_id: int | None = None,
        detail_type: str = "private",
    ) -> str | None:
        intent = classify_request_intent(text)
        if intent == "chat":
            return None
        scope = f"group_{group_id}_{user_id}" if detail_type == "group" else f"private_{user_id}"
        try:
            if intent == "remember":
                return self._remember(text, scope)
            if intent == "query":
                return self._query(text, scope)
            if intent == "ignore":
                return await self._ignore(text, group_id, detail_type, user_id)
            if intent == "remind":
                return await self._remind(text, group_id)
            if intent == "do_task":
                return await self._do_task(text, user_id)
            if intent == "goal":
                return await self._goal(text, user_id)
            if intent == "stop":
                return await self._stop(user_id)
        except Exception as e:  # 执行器隔离：失败回落到正常回复，不阻塞主线
            return f"（执行失败喵，已记下你的要求——{str(e)[:80]}）"
        return None

    # ---- 各意图 ----
    def _remember(self, text: str, scope: str) -> str:
        req = _strip_trigger(text).strip()
        if not req or len(req) < 2:
            return "要记住啥呀？给本喵一个具体内容吧。"
        now = time.strftime("%m-%d %H:%M")
        with self._lock:
            items = self._mem.setdefault(scope, [])
            items.insert(0, {"text": req, "ts": now})
            items[:] = items[: self.store_cap]
            self._save_mem()
        return f"记住了喵：{req}（{now}）"

    def _query(self, text: str, scope: str) -> str:
        keys = [scope]
        if scope.startswith("group_"):
            keys.append("private_" + scope.rsplit("_", 1)[-1])
        hits = []
        with self._lock:
            for k in keys:
                for item in self._mem.get(k, []):
                    hits.append(f"· {item['text']}（{item['ts']}）")
        if not hits:
            return "本喵翻遍了也没找着你说的这事——还没叮嘱过我吗？"
        return "你之前叮嘱过本喵的这些：" + "，".join(hits[:5])

    async def _ignore(self, text, group_id, detail_type, user_id) -> str:
        if detail_type != "group" or group_id is None:
            return "忽略名单是群维度的，私聊没法“别理某人”，用 !anima ignore <qq> 吧。"
        if self.ignore_store is None:
            return "忽略名单暂不可用喵。"
        qqs = [int(x) for x in re.findall(r"\d{5,}", _CQ.sub("", text))]
        if qqs:
            self.ignore_store.add(str(group_id), qqs)
            return f"妥了，本喵以后不再理 {qqs} 了。"
        recent = [int(u) for u in self.recent_speakers(group_id)]
        if not recent:
            return "本喵还没记下该忽略谁——你可以用 !anima ignore <qq> 指给我。"
        n = 2 if any(w in text for w in ("两个", "俩", "2")) else 1
        targets = recent[:n]
        self.ignore_store.add(str(group_id), targets)
        return f"妥了，本喵以后不再理 {targets} 了。"

    async def _remind(self, text: str, group_id) -> "str | None":
        if group_id is None:
            return "提醒是按群定的，私聊设不了喵。"
        calendar = getattr(self.core, "calendar", None)
        if calendar is None or not hasattr(calendar, "add_daily"):
            return "提醒功能不可用喵。"
        t = _CQ.sub("", text or "")
        m = _TIME.search(t) or _TIME_CN.search(t)
        if not m:
            # 无时间的「提醒我」多为闲聊表达（如打招呼「叫我小测」），回落
            # 正常 LLM 回复，不再抢答格式提示（2026-09-06 体验测试①）。
            return None
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        period_m = _PERIOD_RE.search(t)
        if period_m and hh < 12 and period_m.group(1) in ("下午", "晚上", "傍晚", "夜里"):
            hh += 12
        if hh > 23 or mm > 59:
            return "时间不对喵（时 0-23、分 0-59）。"
        content = t[: m.start()].strip() + " " + t[m.end():].strip()
        content = _strip_trigger(content)
        # 只留提醒本体：去掉残留日期/时段/重复词与句尾客套（体验测试②）
        content = _RECUR_RE.sub(" ", _PERIOD_RE.sub(" ", _DATE_ONCE_RE.sub(" ", content)))
        content = _POLITENESS_RE.sub("", content)
        content = re.sub(r"\s+", " ", content).strip() or "（提醒）"
        once_date = _once_date(t)
        try:
            if once_date is not None:
                calendar.add_once(int(group_id), once_date, f"{hh:02d}:{mm:02d}", content)
                return (f"已设一次性提醒喵：{once_date} {hh:02d}:{mm:02d} "
                        f"提醒「{content}」。")
            calendar.add_daily(int(group_id), f"{hh:02d}:{mm:02d}", content)
        except (TypeError, ValueError):
            return "提醒设置失败喵。"
        return f"已设每天提醒喵：{hh:02d}:{mm:02d} 提醒「{content}」。"

    async def _do_task(self, text: str, user_id: int) -> str:
        from anima.agent.owner_proposal import handle_owner_problem
        req = _strip_trigger(text).strip() or text
        _task, reply = await handle_owner_problem(self.core, req, int(user_id))
        return reply

    async def _goal(self, text: str, user_id: int) -> str:
        """建立/查看长期目标（P2，2026-10-23）。

        **owner-only**：长期目标会让它在后台持续花 token、派生任务，
        只有 owner 能建立。非 owner 给**诚实指引**而不是静默失败
        （沿用 `remind` 的既有处理哲学）。

        支持的自然语言：
          「帮我盯着 X」 / 「每周帮我整理 Y」 ⇒ 建立目标
          「我的目标」 / 「你在追什么」       ⇒ 查看目标列表
        """
        # 查看请求：不含建立类触发词，只是问
        from anima.agent.goal import GOAL_TRIGGERS, GoalStore, format_goal_list, parse_goal
        t = str(text or "").strip()
        _view_words = ("我的目标", "在追什么", "追哪些", "目标列表", "有哪些目标")
        _is_view = any(w in t for w in _view_words) and not any(
            w in t for w in ("帮我盯着", "帮我盯", "持续关注", "长期跟进"))

        persistence = getattr(self.core, "persistence", None)
        if persistence is None:
            return "本喵这边暂时记不住长期目标（存储未就绪），稍后再试喵。"

        store = GoalStore(persistence)

        if _is_view:
            return format_goal_list(store)

        # 建立目标：owner-only
        owners = list(getattr(getattr(self.core.config, "permissions", None),
                              "owners", []) or [])
        if int(user_id) not in [int(o) for o in owners]:
            return ("长期目标只有主人能设喵 —— 它会在后台一直推进、"
                    "持续花算力，不能随便交出去。")

        if len(store.active()) >= 5:
            return ("本喵现在追着 5 个目标了，再多就顾不过来喵。"
                    "先完成或暂停一个再设新的吧。")

        objective, constraints, criteria, cadence = parse_goal(t)
        if not objective:
            return "想让本喵盯什么？说清楚一点，比如「帮我盯着 X 群的动态」。"

        g = store.create(objective, int(user_id),
                         constraints=constraints,
                         success_criteria=criteria,
                         cadence_sec=cadence)
        _cad = ("" if not cadence
                else f"\n周期：每 {int(cadence // 3600)} 小时" if cadence < 86400
                else f"\n周期：每 {int(cadence // 86400)} 天")
        return (f"好喵，本喵记住了，会一直盯着：{objective[:70]}"
                f"{_cad}\n"
                f"目标号：{g.id}\n"
                f"（想让它停就说「暂停目标 {g.id}」，或「我的目标」看看进展）")

    async def _stop(self, user_id: int) -> str:
        """自然语言叫停：取消**本人**所有在跑的多步 Agent 任务。

        🔴 2026-10-21（owner：「能不能不用命令 !anima，用自然语言」）：
        启动能自然语言，叫停也必须能 —— 否则报备里承诺的「说停就行」是空话。

        边界：
        * 只取消 `creator == 本人` 且在跑（running/executing/planning）的任务，
          **不碰别人的任务**（即便同群、即便 owner 是群主）；
        * 没有在跑的任务 ⇒ 如实说「没有在跑的活」，不假装取消成功；
        * 异常只报「没能停下」，绝不谎报已取消。
        """
        agent = getattr(self.core, "agent", None)
        if agent is None:
            return "本喵现在没有在办的活喵，不用停～"
        store = getattr(agent, "store", None)
        if store is None or not hasattr(store, "list"):
            return "本喵现在没有在办的活喵，不用停～"
        running_states = {"running", "executing", "planning"}
        try:
            mine = [
                t for t in store.list()
                if int(getattr(t, "creator", 0) or 0) == int(user_id)
                and str(getattr(t, "status", "")) in running_states
            ]
        except Exception:
            logger.debug("stop：任务列表读取失败", exc_info=True)
            return "本喵看了一下，没找到正在办的事。"
        if not mine:
            return "本喵现在没有在办的活喵，不用停～"
        cancelled = 0
        for t in mine:
            try:
                agent.cancel(t.id)
                cancelled += 1
            except Exception:
                logger.warning("stop：取消任务失败 task=%s",
                               getattr(t, "id", "?"), exc_info=True)
        if cancelled <= 0:
            return "本喵试着停，但没停下来——可以再说一次。"
        if cancelled == 1:
            return "好喵，停下了。"
        return f"好喵，都停下了（{cancelled} 件）。"
