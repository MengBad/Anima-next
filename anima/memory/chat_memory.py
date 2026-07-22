"""短期对话记忆 —— 按会话保存最近对话 + 可选持久化。

v1.2: (群,用户) 级隔离。
  - 群聊记忆按 (群, 用户) 拆分为独立桶，避免同群跨用户串台。
  - 私聊仍按 (类型, 用户) 单桶，向后兼容旧数据。
  - 主动发言（群级可见）写入该群所有已知用户的桶，保证群上下文连续。
  - 加载旧数据时：丢弃无用户后缀的旧群级共享桶（即污染来源），并清洗含
    身份混淆标记的 bot 条目（如「我是 Antigravity」类历史污染）。
"""

from __future__ import annotations
import json
import logging
import re
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("anima.memory.chat_memory")


# 身份混淆标记：历史污染数据中 bot 条目若包含这些 token，视为污染并清洗掉。
_POLLUTION_TOKENS = ("antigravity", "我是 antigravity", "antigravity ai")
_CQ_TRANSPORT_RE = re.compile(r"\[CQ:(?:image|face|bface|sface|record|video)[^\]]*\]", re.I)


def _is_polluted(entry: dict) -> bool:
    """判断一条 bot 记忆是否为身份污染条目。"""
    if entry.get("role") != "bot":
        return False
    text = str(entry.get("content") or "").lower()
    return any(tok in text for tok in _POLLUTION_TOKENS)


def _clean_transport_entry(entry: dict) -> dict:
    """剥离不应进入上下文的 CQ 媒体载荷，保留普通文字。"""
    cleaned = dict(entry)
    content = str(cleaned.get("content") or "")
    content = _CQ_TRANSPORT_RE.sub("", content).strip()
    if not content and "CQ:" in str(entry.get("content") or ""):
        content = "[用户发送了图片]" if entry.get("role") == "user" else ""
    cleaned["content"] = content
    return cleaned


class ChatMemory:
    """短期对话记忆，支持 JSON 文件持久化。"""

    def __init__(self, max_rounds: int = 20, persist_path: str = ""):
        self.max_rounds = max_rounds
        self._store: dict[str, OrderedDict[str, list[dict]]] = defaultdict(
            lambda: OrderedDict()
        )
        self._last_cleanup = time.time()
        # P0-5：落盘节流计数（累计 N 条 或 间隔 T 秒触发整文件 save）
        self._dirty = 0
        self._last_save = time.time()
        self._persist_path = Path(persist_path) if persist_path else None
        self._on_group_message: Optional[Callable] = None  # 群消息回调（通知心跳）
        # 群内可见的短时上下文：只保留内存、按群隔离、带 TTL，绝不进入个人档案或
        # 向量库。这样既能理解“刚才那句”，又不会恢复旧版跨用户共享持久化桶。
        self._group_recent: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=32))
        self._group_recent_ttl = 15 * 60

        # 尝试恢复
        if self._persist_path and self._persist_path.exists():
            self._restore()

    # ------------------------------------------------------------------
    # 消息计数回调（供心跳使用）
    # ------------------------------------------------------------------

    def set_group_callback(self, cb: Callable) -> None:
        self._on_group_message = cb

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    @staticmethod
    def _is_old_group_key(key: str) -> bool:
        """旧版群级共享桶（无用户后缀），如 'group_123'。

        新版群桶格式为 'group_{chat_id}_{user_id}'，旧格式无用户段，
        是跨用户串台与身份污染的历史来源，恢复时应丢弃。
        """
        if not key.startswith("group_"):
            return False
        remainder = key[len("group_"):]
        # 旧格式：group_123（纯 chat_id，无第二段）
        return "_" not in remainder

    def _restore(self) -> None:
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception:
            return
        # P1-5：旧版群级共享桶（无用户后缀）是跨用户串台与身份污染的历史来源，
        # 恢复时应丢弃；丢弃前写入 chat_memory.legacy.bak.json 备份并告警，
        # 便于追溯而非静默丢失数据。
        legacy_keys = [k for k in data.keys() if self._is_old_group_key(k)]
        if legacy_keys:
            try:
                bak = self._persist_path.with_name("chat_memory.legacy.bak.json")
                existing: dict = {}
                if bak.exists():
                    try:
                        existing = json.loads(bak.read_text(encoding="utf-8"))
                    except Exception:
                        existing = {}
                for k in legacy_keys:
                    existing[k] = data[k]
                bak.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                logger.warning("丢弃 %d 个旧版群级共享桶（串台风险），已备份至 %s：%s",
                               len(legacy_keys), bak, legacy_keys)
            except Exception:
                logger.exception("旧桶备份失败（不影响启动）")
        changed = False
        for key, rounds in data.items():
            # 丢弃旧版群级共享桶（污染来源）
            if self._is_old_group_key(key):
                continue
            od = OrderedDict()
            for rk, entries in rounds.items():
                cleaned = []
                for entry in entries:
                    if not isinstance(entry, dict) or _is_polluted(entry):
                        changed = True
                        continue
                    item = _clean_transport_entry(entry)
                    changed = changed or item != entry
                    if item.get("content"):
                        cleaned.append(item)
                    else:
                        changed = True
                if cleaned:
                    od[rk] = cleaned
            if od:
                self._store[key] = od
        if changed:
            self.save()

    def save(self) -> None:
        if not self._persist_path:
            return
        try:
            data = {}
            for k, od in self._store.items():
                # 写盘前剥离身份污染条目，确保错误人格永不持久化到磁盘
                cleaned_rounds = {
                    rk: [_clean_transport_entry(e) for e in entries
                         if isinstance(e, dict) and not _is_polluted(e)
                         and _clean_transport_entry(e).get("content")]
                    for rk, entries in od.items()
                }
                cleaned_rounds = {rk: v for rk, v in cleaned_rounds.items() if v}
                if cleaned_rounds:
                    data[k] = cleaned_rounds
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self._persist_path)
            self._dirty = 0
            self._last_save = time.time()
        except Exception:
            logger.exception("记忆落盘失败（路径=%s）", self._persist_path)

    # ------------------------------------------------------------------
    # key 派生：(群,用户) 级隔离
    # ------------------------------------------------------------------

    def _key(self, chat_type: str, chat_id: int, user_id: Optional[int] = None) -> str:
        """派生记忆桶 key。

        - 私聊 / 未提供 user_id：'{chat_type}_{chat_id}'（向后兼容）
        - 群聊 + user_id：'{chat_type}_{chat_id}_{user_id}'（每用户独立桶）
        """
        if chat_type == "group" and user_id:
            return f"{chat_type}_{chat_id}_{user_id}"
        return f"{chat_type}_{chat_id}"

    # ------------------------------------------------------------------
    # 消息管理
    # ------------------------------------------------------------------

    def add(self, chat_type: str, chat_id: int, role: str,
            sender_name: str, content: str,
            user_id: Optional[int] = None,
            vision_desc: str = "", notify_group: bool = False) -> None:
        key = self._key(chat_type, chat_id, user_id)

        # 简单情感强度：辱骂 + 高情绪符号
        emotion = 0.0
        negative = {"滚", "烦", "讨厌", "垃圾", "傻逼", "闭嘴", "别说了", "恶心",
                    "废物", "菜", "fuck", "shit", "😡", "💢"}
        positive = {"哈哈", "笑死", "棒", "厉害", "喜欢", "爱", "😊", "❤️"}
        neg = sum(1 for w in negative if w in content)
        pos = sum(1 for w in positive if w in content)
        if pos > neg:
            emotion = 0.3
        elif neg > pos:
            emotion = 0.6

        entry = {
            "role": role,
            "name": sender_name,
            "content": content,
            "time": time.time(),
            "ts": time.time(),
            "salience": 0.6 if role == "bot" else 0.5,
            "access_count": 0,
            "emotion": emotion,
            "retention_score": 1.0,
        }
        if vision_desc:
            entry["vision_desc"] = vision_desc

        od = self._store[key]
        if not od:
            od["round_1"] = []
        last_round_key = next(reversed(od))
        od[last_round_key].append(entry)

        if role == "bot":
            round_num = len(od)
            od[f"round_{round_num + 1}"] = []

        # LRU
        while len(od) > self.max_rounds:
            od.popitem(last=False)

        # 仅由入站群消息观察路径通知心跳，避免同一条 @ 消息在短期档案写入时重复计数。
        if chat_type == "group" and notify_group and role == "user" and self._on_group_message:
            self._on_group_message(chat_id)

        # 定期清理
        now = time.time()
        if now - self._last_cleanup > 600:
            self._cleanup(3600 * 2)
            self._last_cleanup = now

        # P0-5 修复：运行期新增记忆必须落盘，否则重启即丢全部新对话。
        # 旧逻辑仅 ~10% 概率 save，崩溃窗口可能丢大量对话。改为阈值策略：
        # 累计写入 >=10 条 或 距上次落盘 >=60s 才整文件写，兼顾 IO 抖动与数据安全。
        # 每次 add 必走此路径（不再依赖随机），保证丢数据窗口有上界。
        self._dirty += 1
        now = time.time()
        if self._dirty >= 10 or (now - self._last_save) >= 60:
            self.save()

    def get_context(self, chat_type: str, chat_id: int,
                    recent_rounds: int = 10,
                    user_id: Optional[int] = None) -> list[dict]:
        key = self._key(chat_type, chat_id, user_id)
        od = self._store.get(key, OrderedDict())
        if not od:
            return []

        rounds = list(od.items())[-recent_rounds:]
        messages: list[dict] = []
        for _, entries in rounds:
            for entry in entries:
                label = entry["name"] if entry["role"] == "user" else "你"
                text = entry["content"]
                vd = entry.get("vision_desc")
                if vd:
                    text = f"{text} [图片:{vd}]" if text else f"[图片:{vd}]"
                messages.append({
                    # OpenAI 兼容 Chat Completions 不认识 "bot"；存储层仍使用 bot
                    # 以兼容旧数据，但送给模型时必须映射为 assistant。
                    "role": "assistant" if entry["role"] == "bot" else entry["role"],
                    "content": f"[{label}]: {text}",
                })
        return messages

    def observe_group_message(self, group_id: int, user_id: int, sender_name: str,
                              content: str, message_id: object = None) -> None:
        """记录所有入站群消息的短时群上下文，不落盘、不跨群。"""
        self._purge_group_recent(group_id)
        text = (content or "").strip()
        if not text:
            return
        self._group_recent[int(group_id)].append({
            "role": "user",
            "content": f"[{sender_name}({user_id})]: {text[:2000]}",
            "message_id": str(message_id) if message_id is not None else None,
            "time": time.time(),
        })
        if self._on_group_message:
            self._on_group_message(int(group_id))

    def observe_group_bot_reply(self, group_id: int, reply: str) -> None:
        """记录实际发出的群回复，使任意成员的后续追问都能接上。"""
        self._purge_group_recent(group_id)
        if not reply:
            return
        self._group_recent[int(group_id)].append({
            "role": "assistant",
            "content": f"[哈基米]: {reply[:2000]}",
            "message_id": None,
            "time": time.time(),
        })

    def get_recent_group_context(self, group_id: int, limit: int = 12,
                                 exclude_message_id: object = None) -> list[dict]:
        """返回按时间正序的短时群上下文，可排除本轮正在处理的消息。"""
        self._purge_group_recent(group_id)
        excluded = str(exclude_message_id) if exclude_message_id is not None else None
        rows = [m for m in self._group_recent.get(int(group_id), ())
                if not excluded or m.get("message_id") != excluded]
        return [{"role": m["role"], "content": m["content"]}
                for m in rows[-max(0, limit):]]

    def group_recent_ids(self) -> set[int]:
        """返回仍有有效短时上下文的群，用于自主心跳选群。"""
        for gid in list(self._group_recent):
            self._purge_group_recent(gid)
        return {gid for gid, rows in self._group_recent.items() if rows}

    def _purge_group_recent(self, group_id: int) -> None:
        rows = self._group_recent.get(int(group_id))
        if not rows:
            return
        cutoff = time.time() - self._group_recent_ttl
        while rows and rows[0].get("time", 0.0) < cutoff:
            rows.popleft()

    def set_vision_desc(self, chat_type: str, chat_id: int,
                        user_id: Optional[int], desc: str) -> None:
        """把最近一条用户消息补上视觉描述（后台视觉理解完成后调用）。

        让后续轮次的 get_context 能忆起「用户之前发过一张什么图」，
        而不必把图片本身重复塞进上下文。
        """
        if not desc:
            return
        key = self._key(chat_type, chat_id, user_id)
        od = self._store.get(key)
        if not od:
            return
        for rk in reversed(od):
            for entry in reversed(od[rk]):
                if entry.get("role") == "user":
                    entry["vision_desc"] = desc
                    self.save()
                    return

    def last_bot_message(self, chat_type: str, chat_id: int,
                         user_id: Optional[int] = None) -> Optional[str]:
        key = self._key(chat_type, chat_id, user_id)
        od = self._store.get(key, OrderedDict())
        for _, entries in reversed(list(od.items())):
            for entry in reversed(entries):
                if entry["role"] == "bot":
                    return entry["content"]
        return None

    def forget_user(self, user_id: int) -> int:
        """删除该用户的私聊与所有群内个人会话档案，返回删除档数。"""
        suffix = f"_{int(user_id)}"
        targets = [key for key in self._store if key.endswith(suffix)]
        for key in targets:
            self._store.pop(key, None)
        if targets:
            self.save()
        return len(targets)

    def export_user(self, user_id: int) -> dict[str, list[dict]]:
        """导出该用户自己的会话档案副本，供自助数据访问。"""
        suffix = f"_{int(user_id)}"
        data: dict[str, list[dict]] = {}
        for key, rounds in self._store.items():
            if not key.endswith(suffix):
                continue
            data[key] = [entry.copy() for entries in rounds.values() for entry in entries]
        return data

    # ------------------------------------------------------------------
    # 群级聚合（供自主发言使用）：合并该群所有 (群,用户) 桶的近期内容
    # ------------------------------------------------------------------

    def _group_user_keys(self, group_id: int) -> list[str]:
        """返回该群所有 (群,用户) 级桶的 key 列表。"""
        prefix = f"group_{group_id}_"
        return [k for k in self._store.keys() if k.startswith(prefix)]

    def get_group_context(self, group_id: int,
                          recent_rounds: int = 6) -> list[dict]:
        """聚合该群所有用户的近期对话，供自主发言参考群内氛围。

        按时间倒序取最近若干条，格式与 get_context 一致。
        """
        # 新路径优先使用真实的群可见短时上下文，且保持时间正序。
        recent = self.get_recent_group_context(group_id, limit=recent_rounds * 4)
        if recent:
            return recent

        collected: list[dict] = []
        for key in self._group_user_keys(group_id):
            od = self._store[key]
            for _, entries in list(od.items())[-recent_rounds:]:
                for entry in entries:
                    label = entry["name"] if entry["role"] == "user" else "你"
                    collected.append({
                        "role": "assistant" if entry["role"] == "bot" else entry["role"],
                        "content": f"[{label}]: {entry['content']}",
                        "_t": entry.get("time", 0.0),
                    })
        collected.sort(key=lambda m: m.pop("_t"))
        # 粗略裁剪到 recent_rounds * 4 条，控制 token
        return collected[: recent_rounds * 4]

    def add_group_bot_reply(self, group_id: int, reply: str) -> None:
        """将群级可见的 bot 回复写入该群所有已知用户的桶。

        保证每位成员在自己的 (群,用户) 上下文中都能看到 bot 的群发言，
        同时不污染源数据（每个桶独立）。
        """
        for key in self._group_user_keys(group_id):
            # 从 key 反解 user_id
            parts = key.split("_")
            # key 形如 group_{chat_id}_{user_id}
            if len(parts) >= 3:
                try:
                    uid = int(parts[2])
                except ValueError:
                    uid = None
            else:
                uid = None
            self.add("group", group_id, "bot", "Anima", reply, user_id=uid)

    def _cleanup(self, ttl: float) -> None:
        now = time.time()
        stale = []
        for key, od in self._store.items():
            last_time = 0
            for _, entries in od.items():
                for e in entries:
                    last_time = max(last_time, e.get("time", 0))
            if now - last_time > ttl:
                stale.append(key)
        for key in stale:
            del self._store[key]


# 全局单例
_chat_memory: Optional[ChatMemory] = None


def get_chat_memory(persist_path: str = "") -> ChatMemory:
    global _chat_memory
    if _chat_memory is None:
        _chat_memory = ChatMemory(persist_path=persist_path)
    return _chat_memory
