"""跨群记忆/氛围管理器：统筹用户跨群记忆、群氛围画像、即时会话，并负责持久化。

由 crossgroup_bot 的 MemoryManager 移植而来（改名 CrossGroupManager 以避免与
旧版概念混淆），并新增：事实内容去重（防止 LLM 抽取重复事实撑爆存储）与
``flush()`` 接口（供 AnimaCore.stop() 调用，JSON 已增量落盘，此处为幂等收尾）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Any

from .user_memory import UserMemory, UserFact
from .session_buffer import SessionBuffer
from ..atmosphere.profiles import AtmosphereProfile, AtmosphereStyle
from ..atmosphere.analyzer import analyze, merge

logger = logging.getLogger("anima.crossgroup")

# F4：两字名边界检查用「名字延展后缀」字符集。若两字名后紧跟这些字，通常是一个更长的
# 词（如「小宝」→「小宝贝」），不应视为对该短名的引用，避免误命中。
_NAME_EXTENSION_CHARS = set(
    "宝贝猫狗兔熊仔儿女娃妮娜琳婷轩浩宇晨曦月星雪雨花果糖豆米乐欣怡然安宁静雅萌"
    "胖球蛋团圆福禄寿喜哥姐弟妹老"
)


def _boundary_hit(text: str, needle: str) -> bool:
    """子串命中需边界：``needle`` 作为独立称呼出现，而非更长词的一部分。

    规则：命中位置之后若紧跟「文字类」字符（CJK/字母/数字），且该字符属于
    ``_NAME_EXTENSION_CHARS``（名字延展后缀），则视为更长词的一部分，不命中；
    其余情况（句尾、标点/空格、或普通文字如动词「在」）均视为边界安全的独立引用。
    用于防止「小宝」∈「小宝贝」误命中，同时保留「小宝在吗」这类正常引用。
    """
    i = text.find(needle)
    while i >= 0:
        after = text[i + len(needle):i + len(needle) + 1]
        if not after:
            return True  # 句尾
        if not re.match(r"[\u4e00-\u9fff0-9a-zA-Z]", after):
            return True  # 标点/空格等非文字类 → 边界安全
        if after[0] in _NAME_EXTENSION_CHARS:
            # 紧跟名字延展后缀 → 更长词，跳过此命中继续找
            i = text.find(needle, i + 1)
            continue
        # 紧跟普通文字（如动词/助词）→ 视为独立引用边界
        return True
    return False


class CrossGroupManager:
    _MAX_USERS = 500  # 总用户上限，超限淘汰最久未交互的

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
        user = self._users[user_id]
        if name:
            if not user.name:
                user.name = name
            elif name != user.name:
                # P4: 跨群身份统一——不同群不同名字记入 aliases
                if name not in user.aliases and name != user.name:
                    user.aliases.append(name)
                    user.aliases = user.aliases[-5:]  # 最多 5 个别名
        return user

    def add_user_fact(self, user_id: str, content: str, source_group: str,
                      tags: list[str] | None = None, name: str = "",
                      nicknames: list[str] | None = None,
                      confidence: float = 1.0) -> UserFact | None:
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
        fact = user.add_fact(content, source_group, tags, confidence=confidence)
        # FIFO 截断：超出上限时丢弃最旧事实，防止 LLM 抽取无限膨胀存储
        if self.max_facts_per_user > 0 and len(user.facts) > self.max_facts_per_user:
            user.facts = user.facts[-self.max_facts_per_user:]
        if nicknames:
            self.add_nicknames(user_id, nicknames, name=name)
        self._save_users()
        return fact

    def add_nicknames(self, user_id: str, nicknames: list[str],
                      name: str = "") -> int:
        """追加该用户的外号（他人称呼），去重并封顶 8 个。返回新增条数。"""
        if not nicknames:
            return 0
        user = self.get_user(user_id, name)
        added = 0
        for nk in nicknames:
            nk = (nk or "").strip()
            if not nk:
                continue
            # 不与本名/已有别名/已有外号重复
            if nk == user.name or nk in user.aliases or nk in user.nicknames:
                continue
            user.nicknames.append(nk)
            added += 1
        if user.nicknames:
            user.nicknames = user.nicknames[-8:]  # 最多 8 个外号
        if added:
            self._save_users()
        return added

    def search_by_name(self, query: str, limit: int = 5) -> list[tuple[str, str, int]]:
        """按名字/别名/外号模糊搜索用户，返回 [(user_id, 匹配到的名字, 优先级), ...]。

        优先级：本名(name)精确=3 > 外号(nicknames)精确=3 > 本名/外号子串=2 > 别名=1。
        用于「说外号/别名查人」时把自然语映射回 user_id。
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        hits: list[tuple[str, str, int]] = []
        for uid, u in self._users.items():
            if u.name:
                if q == u.name.lower():
                    hits.append((uid, u.name, 3))
                    continue
                if q in u.name.lower():
                    hits.append((uid, u.name, 2))
            # 外号（他人称呼）——最关键的「叫外号查人」场景
            for nk in u.nicknames:
                if q == nk.lower():
                    hits.append((uid, nk, 3))
                    break
                if q in nk.lower():
                    hits.append((uid, nk, 2))
                    break
            # 跨群别名：仅精确匹配（ANM-006：子串匹配易被注入别名劫持查询）
            for al in u.aliases:
                if q == al.lower():
                    hits.append((uid, al, 2))
                    break
        hits.sort(key=lambda h: -h[2])
        seen = set()
        out = []
        for uid, nm, pri in hits:
            if uid in seen:
                continue
            seen.add(uid)
            out.append((uid, nm, pri))
        return out[:limit]

    def find_mentioned(self, text, at_uids=None, exclude_uid=None,
                       group_id=None, limit: int = 2):
        """反向解析：从消息文本 + @段 找出被提及的已知用户。

        三源：
          ① CQ:at 的 qq 直接命中（最高优先级，零歧义）；
          ② 文本子串扫描已知用户的 name/alias/nickname（带短名阈值防误命中）。
        返回 ``[(uid, display_name, priority), ...]``：已排除 exclude_uid（发言者本人），
        按优先级降序、截断 limit（默认 2 人）。若提供 ``group_id``，本群成员
        （``u.groups`` 含该群，即曾在当前群交互过）优先级提升至 3，实现跨群同名碰撞时
        本群成员优先（修复「群优先」缺口，此前 group_id 声明但未使用）。
        """
        from anima.util.name_clean import clean_display_name
        candidates: dict[str, tuple[str, int]] = {}
        text_l = (text or "").lower()
        gid_key = str(group_id) if group_id else None
        # ① CQ:at：qq 直接映射
        for qq in (at_uids or []):
            qq = str(qq)
            if qq in self._users and qq != exclude_uid:
                u = self._users[qq]
                candidates.setdefault(qq, (u.name or qq, 3))
        # ② 文本子串扫描 name / alias / nickname
        for uid, u in self._users.items():
            if uid == exclude_uid:
                continue
            best = 0
            # 本名 / 外号：子串匹配（F4 边界检查在后续提交补充）
            for raw_nm in (u.name, *u.nicknames):
                nm = clean_display_name(raw_nm).lower()
                if len(nm) < 2:
                    # 短名（单字/纯括号）跳过，防「小宝」∈「小宝贝」误伤
                    continue
                if nm and _boundary_hit(text_l, nm):
                    best = max(best, 2)
                    if nm == text_l:
                        best = max(best, 3)  # 全句精确等于名字 → 最高置信
            # 跨群别名：仅精确匹配（ANM-006：防攻击者注入别名后被子串误关联）
            for al in u.aliases:
                al_l = clean_display_name(al).lower()
                if len(al_l) < 2:
                    continue
                if al_l and al_l == text_l:
                    best = max(best, 1)  # 别名精确匹配，最低优先级
            if best:
                # ANM-001：群优先——当前群在用户交互过的群集合内时，提升优先级到 3，
                # 让本群成员在跨群同名碰撞时排在前面。
                if gid_key and gid_key in getattr(u, "groups", {}):
                    best = max(best, 3)
                cur = candidates.get(uid)
                if cur is None or best > cur[1]:
                    candidates[uid] = (clean_display_name(u.name) or uid, best)
        ranked = sorted(candidates.items(), key=lambda kv: -kv[1][1])
        return [(uid, nm, pri) for uid, (nm, pri) in ranked[:limit]]

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
                with open(self._users_path(), encoding="utf-8") as _fh:
                    raw = json.load(_fh)
                for uid, d in raw.items():
                    u = UserMemory(
                        user_id=uid, name=d.get("name", ""),
                        preferences=d.get("preferences", {}),
                        groups=d.get("groups", {}),
                        aliases=d.get("aliases", []),  # P4: 跨群别名
                        nicknames=d.get("nicknames", []),  # 外号（他人称呼）
                        topic_preferences=d.get("topic_preferences", {}),  # E2
                        style_hint=d.get("style_hint", ""),               # E2
                        avg_msg_length=float(d.get("avg_msg_length", 0)), # E2
                    )
                    u._msg_count = int(d.get("_msg_count", 0))            # E2
                    # 只取 UserFact 已知字段：老 users.json 缺新字段由 dataclass
                    # 默认值补齐；万一将来有多余字段也不会让整份记忆加载失败。
                    _ff = UserFact.__dataclass_fields__
                    u.facts = [UserFact(**{k: v for k, v in f.items() if k in _ff})
                               for f in d.get("facts", [])]
                    self._users[uid] = u
        except Exception:
            logger.exception("加载 crossgroup users.json 失败")
        try:
            if os.path.exists(self._atm_path()):
                with open(self._atm_path(), encoding="utf-8") as _fh:
                    raw = json.load(_fh)
                for gid, d in raw.items():
                    fields = AtmosphereProfile.__dataclass_fields__
                    d2 = {k: v for k, v in d.items() if k in fields}
                    d2["style"] = AtmosphereStyle(d["style"])
                    d2["group_id"] = gid
                    self._atmospheres[gid] = AtmosphereProfile(**d2)
        except Exception:
            logger.exception("加载 crossgroup atmospheres.json 失败")

    def _save_users(self) -> None:
        # 总用户上限裁剪：淘汰最久未交互的（以最新 fact 时间戳为准）
        if len(self._users) > self._MAX_USERS:
            def _last_ts(u: UserMemory) -> float:
                return max((f.timestamp for f in u.facts), default=0.0)
            sorted_uids = sorted(self._users, key=lambda uid: _last_ts(self._users[uid]))
            evict = len(self._users) - self._MAX_USERS
            for uid in sorted_uids[:evict]:
                del self._users[uid]
            logger.info("crossgroup 用户裁剪：淘汰 %d 个最久未交互用户", evict)
        raw = {}
        for uid, u in self._users.items():
            raw[uid] = {
                "name": u.name,
                "preferences": u.preferences,
                "groups": u.groups,
                "aliases": u.aliases,  # P4: 跨群别名持久化
                "nicknames": u.nicknames,  # 外号（他人称呼）
                "topic_preferences": u.topic_preferences,  # E2
                "style_hint": u.style_hint,                # E2
                "avg_msg_length": u.avg_msg_length,        # E2
                "_msg_count": u._msg_count,                # E2
                "facts": [
                    {"content": f.content, "source_group": f.source_group,
                     "timestamp": f.timestamp, "tags": f.tags,
                     # P1-2 记忆压缩：置信度/争议/合并信息必须持久化，否则每次
                     # 重启后压缩产出的成果（merge_count / source_groups）会丢失。
                     "confidence": f.confidence, "disputed": f.disputed,
                     "compressed": f.compressed, "merge_count": f.merge_count,
                     "source_groups": f.source_groups,
                     "access_count": f.access_count,
                     "merged_from": f.merged_from}
                    for f in u.facts
                ],
            }
        self._atomic_write_json(self._users_path(), raw)

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
        self._atomic_write_json(self._atm_path(), raw)

    @staticmethod
    def _atomic_write_json(path: str, obj: Any) -> None:
        """原子写 JSON：先写临时文件再 os.replace，防止进程崩溃时文件损坏。"""
        import tempfile as _tf
        target = path
        parent = os.path.dirname(target) or "."
        fd, tmp = _tf.mkstemp(dir=parent, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _users_path(self) -> str:
        return os.path.join(self.data_dir, "users.json")

    def _atm_path(self) -> str:
        return os.path.join(self.data_dir, "atmospheres.json")


# 向后兼容别名（旧 demo/测试可能引用 MemoryManager）
MemoryManager = CrossGroupManager
