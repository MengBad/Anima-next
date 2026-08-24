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
- chat     : 其它                         → 走正常回复，不误动作

仅 owner 或被@ 的入站触发；未知意图一律回落到正常聊天。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

_CQ = re.compile(r"\[CQ:[^\]]+\]", re.I)
_TIME = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")
_TIME_CN = re.compile(r"(\d{1,2})\s*(?:[点时])\s*(\d{1,2})?\s*分?")

_TRIGGER = {
    "remember": ("记住", "别忘", "记一下", "帮我记", "记好", "牢牢记", "谨记"),
    "remind": ("提醒我", "设个提醒", "定个闹钟", "几点提醒", "到点提醒", "叫我"),
    "ignore": ("别再理", "不要理", "别理", "冷落", "别鸟", "别再搭"),
    "query": ("还记得吗", "你还记得", "我上次说的", "我之前说的", "我们的关系"),  # 通用"帮我查"交还带工具的流水线
    "do_task": ("有个问题", "帮我修", "帮我查下代码", "看下日志"),  # 通用"帮我看看/帮我做"交还流水线
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

    async def _remind(self, text: str, group_id) -> str:
        if group_id is None:
            return "提醒是按群定的，私聊设不了喵。"
        calendar = getattr(self.core, "calendar", None)
        if calendar is None or not hasattr(calendar, "add_daily"):
            return "提醒功能不可用喵。"
        t = _CQ.sub("", text or "")
        m = _TIME.search(t) or _TIME_CN.search(t)
        if not m:
            return "设提醒要有时间呀，像「明早8点提醒我交稿」。格式：时间 + 内容。"
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        if hh > 23 or mm > 59:
            return "时间不对喵（时 0-23、分 0-59）。"
        content = t[: m.start()].strip() + " " + t[m.end():].strip()
        content = _strip_trigger(content).strip() or "（提醒）"
        try:
            calendar.add_daily(int(group_id), f"{hh:02d}:{mm:02d}", content)
        except (TypeError, ValueError):
            return "提醒设置失败喵。"
        return f"已设提醒喵：群 {group_id}，每天 {hh:02d}:{mm:02d} 提醒「{content}」。"

    async def _do_task(self, text: str, user_id: int) -> str:
        tasks = getattr(self.core, "tasks", None)
        if tasks is None or not hasattr(tasks, "create"):
            return "任务系统暂不可用喵。"
        plan = [
            "收集日志、配置与相关代码证据",
            "给出最小修复方案并等待确认",
            "修改后跑测试",
            "部署/重启前单独请求批准",
            "验证并记录结论",
        ]
        req = _strip_trigger(text).strip() or text
        task = tasks.create(req, int(user_id), plan)
        return (
            f"已给本喵记下问题喵（任务 {task.id}）。先诊断给方案，改/部署会再单独请示你。"
            f"回复「!anima task approve {task.id}」执行。"
        )