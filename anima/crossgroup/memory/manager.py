"""跨群记忆/氛围管理器：统筹用户跨群记忆、群氛围画像、即时会话，并负责持久化。

由 crossgroup_bot 的 MemoryManager 移植而来（改名 CrossGroupManager 以避免与
旧版概念混淆），并新增：事实内容去重（防止 LLM 抽取重复事实撑爆存储）与
``flush()`` 接口（供 AnimaCore.stop() 调用，JSON 已增量落盘，此处为幂等收尾）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any

from .user_memory import UserMemory, UserFact
from .session_buffer import SessionBuffer
from ..atmosphere.profiles import AtmosphereProfile, AtmosphereStyle
from ..atmosphere.analyzer import analyze, merge

logger = logging.getLogger("anima.crossgroup")


class CrossGroupManager:
    def __init__(self, data_dir: str = "data/crossgroup"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._users: dict[str, UserMemory] = {}
        self._atmospheres: dict[str, AtmosphereProfile] = {}
        self._sessions = SessionBuffer()
        self.max_facts_per_user: int = 50   # 单用户事实上限，超限 FIFO 截断防膨胀
        # 群级滚动缓冲 + 观察节流：适配线上"逐条消息"场景（原 observe_group 接收批量，
        # 这里累积窗口、每隔若干条才重新分析，兼顾成本与响应）。
        self._group_buf: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._group_obs_counter: dict[str, int] = defaultdict(int)
        self._load()

    # ---------- 用户跨群记忆 ----------
    def get_user(self, user_id: str, name: str = "") -> UserMemory:
        if user_id not in self._users:
            self._users[user_id] = UserMemory(user_id=user_id, name=name)
        if name and not self._users[user_id].name:
            self._users[user_id].name = name
        return self._users[user_id]

    def add_user_fact(self, user_id: str, content: str, source_group: str,
                      tags: list[str] | None = None, name: str = "") -> UserFact | None:
        """新增一条用户事实。

        内容去重：若该用户已有"内容标准化后相同"的事实，则跳过（返回 None），
        避免 LLM 每条消息重复抽取同一事实导致存储膨胀。
        """
        content = (content or "").strip()
        if not content:
            return None
        user = self.get_user(user_id, name)
        # 归一化：去除所有空白后小写比较，兼容 "正在学 Rust" / "正在学Rust" 这类
        # 仅空格/标点差异的重复（LLM 抽取常产生此类变体）。
        norm = "".join(content.split()).lower()
        for f in user.facts:
            if "".join(f.content.split()).lower() == norm:
                return None  # 已存在，跳过
        fact = user.add_fact(content, source_group, tags)
        # FIFO 截断：超出上限时丢弃最旧事实，防止 LLM 抽取无限膨胀存储
        if self.max_facts_per_user > 0 and len(user.facts) > self.max_facts_per_user:
            user.facts = user.facts[-self.max_facts_per_user:]
        self._save_users()
        return fact

    def replace_user_fact(self, user_id: str, index: int, content: str) -> bool:
        """按展示序号（1-based）纠正一条跨群事实。"""
        user = self._users.get(str(user_id))
        value = (content or "").strip()
        if user is None or not value or not 1 <= index <= len(user.facts):
            return False
        user.facts[index - 1].content = value
        user.facts[index - 1].timestamp = time.time()
        self._save_users()
        return True

    def delete_user_fact(self, user_id: str, index: int) -> bool:
        """按展示序号（1-based）删除一条跨群事实。"""
        user = self._users.get(str(user_id))
        if user is None or not 1 <= index <= len(user.facts):
            return False
        user.facts.pop(index - 1)
        self._save_users()
        return True

    def forget_user(self, user_id: str) -> int:
        """删除某用户的全部跨群事实，返回删除数量。"""
        user = self._users.pop(str(user_id), None)
        if user is None:
            return 0
        count = len(user.facts)
        self._save_users()
        return count

    # ---------- 群氛围 ----------
    def get_atmosphere(self, group_id: str) -> AtmosphereProfile:
        return self._atmospheres.get(group_id, AtmosphereProfile(group_id=group_id))

    def observe_group(self, group_id: str, messages: list[str],
                      weight: float = 0.3) -> AtmosphereProfile:
        if not messages:
            return self.get_atmosphere(group_id)
        if group_id in self._atmospheres:
            prof = merge(self._atmospheres[group_id], messages, weight)
        else:
            prof = analyze(messages)
            prof.group_id = group_id
        self._atmospheres[group_id] = prof
        self._save_atmospheres()
        return prof

    def observe_message(self, group_id: str, text: str, window: int = 10,
                        every: int = 5) -> AtmosphereProfile:
        """逐条消息增量观察：累积滚动缓冲，每隔 ``every`` 条用最近 ``window`` 条
        重新分析并 merge 一次。避免每条都全量分析，兼顾成本与氛围时效。
        """
        buf = self._group_buf[group_id]
        buf.append(text)
        self._group_obs_counter[group_id] += 1
        if self._group_obs_counter[group_id] < every:
            return self.get_atmosphere(group_id)
        self._group_obs_counter[group_id] = 0
        sample = list(buf)[-window:]
        return self.observe_group(group_id, sample)

    # ---------- 即时会话 ----------
    def append_session(self, user_id: str, group_id: str, role: str, text: str) -> None:
        self._sessions.append(user_id, group_id, role, text)

    def get_session(self, user_id: str, group_id: str) -> list[dict]:
        return self._sessions.get(user_id, group_id)

    # ---------- 持久化 ----------
    def flush(self) -> None:
        """幂等收尾：JSON 已在每次写入时落盘，这里再做一次确保。"""
        try:
            self._save_users()
            self._save_atmospheres()
        except Exception:
            logger.exception("crossgroup flush 失败")

    def _load(self) -> None:
        try:
            if os.path.exists(self._users_path()):
                raw = json.load(open(self._users_path(), encoding="utf-8"))
                for uid, d in raw.items():
                    u = UserMemory(
                        user_id=uid, name=d.get("name", ""),
                        preferences=d.get("preferences", {}),
                        groups=d.get("groups", {}),
                    )
                    u.facts = [UserFact(**f) for f in d.get("facts", [])]
                    self._users[uid] = u
        except Exception:
            logger.exception("加载 crossgroup users.json 失败")
        try:
            if os.path.exists(self._atm_path()):
                raw = json.load(open(self._atm_path(), encoding="utf-8"))
                for gid, d in raw.items():
                    fields = AtmosphereProfile.__dataclass_fields__
                    d2 = {k: v for k, v in d.items() if k in fields}
                    d2["style"] = AtmosphereStyle(d["style"])
                    d2["group_id"] = gid
                    self._atmospheres[gid] = AtmosphereProfile(**d2)
        except Exception:
            logger.exception("加载 crossgroup atmospheres.json 失败")

    def _save_users(self) -> None:
        raw = {}
        for uid, u in self._users.items():
            raw[uid] = {
                "name": u.name,
                "preferences": u.preferences,
                "groups": u.groups,
                "facts": [
                    {"content": f.content, "source_group": f.source_group,
                     "timestamp": f.timestamp, "tags": f.tags}
                    for f in u.facts
                ],
            }
        with open(self._users_path(), "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)

    def _save_atmospheres(self) -> None:
        raw = {}
        for gid, a in self._atmospheres.items():
            raw[gid] = {
                "style": a.style.value,
                "confidence": a.confidence,
                "descriptors": a.descriptors,
                "signals": a.signals,
                "sample_phrases": a.sample_phrases,
                "last_updated": a.last_updated,
            }
        with open(self._atm_path(), "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)

    def _users_path(self) -> str:
        return os.path.join(self.data_dir, "users.json")

    def _atm_path(self) -> str:
        return os.path.join(self.data_dir, "atmospheres.json")


# 向后兼容别名（旧 demo/测试可能引用 MemoryManager）
MemoryManager = CrossGroupManager
