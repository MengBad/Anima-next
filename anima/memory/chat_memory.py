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
from typing import Any, Optional, Callable

logger = logging.getLogger("anima.memory.chat_memory")


# 身份混淆标记：历史污染数据中 bot 条目若包含这些 token，视为污染并清洗掉。
_POLLUTION_TOKENS = ("antigravity", "我是 antigravity", "antigravity ai")
from anima.constants import CQ_TRANSPORT_RE as _CQ_TRANSPORT_RE

# 「归属标注」前缀（仅 `get_recent_group_context(annotate=True)` 使用）。
# 背景：群缓冲是**群级共享**的，同群不同用户并发 @bot 时，缓冲里会同时存在
# 「别人问 bot、其后没有任何 assistant 回复」的 user turn，与本轮要回的那条
# 在 messages 里**同形**（都 role=user、都像在问 bot、中间没有 assistant 隔断）。
# `exclude_message_id` 只排掉"自己这条" ⇒ 剩下的他人待答提问成了无标记歧义源，
# 模型会被诱导去答别人那条（2026-09-21 生产事故：引用锚正确、正文答错对象）。
_BYSTAND_PENDING_PREFIX = "[旁听·他人提问·非本轮] "
_BYSTAND_PREFIX = "[旁听] "


def _is_polluted(entry: dict) -> bool:
    """判断一条 bot 记忆是否为身份污染条目。"""
    if entry.get("role") != "bot":
        return False
    text = str(entry.get("content") or "").lower()
    return any(tok in text for tok in _POLLUTION_TOKENS)


#: bot 历史发言里的**自称/贬称漂移词** → 中性替代。
#:
#: 🔴 2026-10-25：与 `persona_core.yaml` 第 2-3 行的禁令**必须保持一致**。
#: 生产实证（`data/chat_memory.json`）：bot 条目含 本大爷 47 / 老子 4 / 两脚兽 7。
#: 那些是**旧人格时代**留下的产物；`get_context` 会把 role=bot 映射成
#: assistant 送进 messages ⇒ 模型读到「我以前就是这么说话的」，
#: 与人格禁令正面冲突 ⇒ 跟示范走（这就是 54/214 条出站含漂移词的机制）。
#:
#: ⚠️ 只作用于**注入面**，不动存档（存档是原始对话记录，改它等于篡改历史）。
#: ⚠️ 只作用于 **bot 条目**（user 条目是别人的原话，一个字都不能改）。
_SELF_ALIAS_FIXES: tuple[tuple[str, str], ...] = (
    ("本大爷", "本喵"),
    ("老娘", "本喵"),
    ("老子", "本喵"),
    ("本座", "本喵"),
    ("朕", "本喵"),
    ("两脚兽", "大家"),
    ("铲屎官", "大家"),
    ("低等生物", "大家"),
)


def _neutralise_self_alias(text: str) -> str:
    """把 bot 历史发言里的自称/贬称漂移词换成中性说法（仅注入用）。

    幂等：替换后的词（本喵 / 大家）不在替换表里，重复调用无变化。
    """
    s = str(text or "")
    if not s:
        return s
    for bad, good in _SELF_ALIAS_FIXES:
        if bad in s:
            s = s.replace(bad, good)
    return s


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
            raw_text = self._persist_path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("读取记忆文件失败（跳过恢复，启动空记忆）")
            return
        try:
            data = json.loads(raw_text)
        except Exception:
            # ANM-007：文件存在但已损坏——先备份原内容再跳过恢复，避免静默丢失
            # 且便于诊断。恢复期为只读，save 不会触发，空记忆不会立即覆盖原文件。
            try:
                bak = self._persist_path.with_name("chat_memory.corrupt.bak.json")
                bak.write_text(raw_text, encoding="utf-8")
                logger.error(
                    "记忆文件损坏，已备份至 %s 并跳过恢复（启动空记忆）", bak)
            except Exception:
                logger.exception("损坏记忆文件备份失败（不影响启动）")
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
            # 恢复期清洗（CQ 剥离/污染剔除）视为脏数据：置脏让下方 save 的
            # 脏守卫放行（深审批次5 新增守卫，此处需同步置脏）
            self._dirty += 1
            self.save()

    def save(self) -> None:
        if getattr(self, "_dirty", 0) == 0:
            return  # 无新增脏数据，跳过整文件重写（心跳补 flush 用）
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
            from anima.core.persistence import atomic_write_text
            atomic_write_text(self._persist_path,
                              json.dumps(data, ensure_ascii=False, indent=2))
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
            # ANM-007：用『现有最大轮号 + 1』命名新 round，而非 len(od)。
            # LRU 淘汰后 od 长度 < 最大轮号，若用 len(od) 会在淘汰后覆盖最新非空轮，
            # 造成该轮用户消息丢失（round 碰撞 / 上下文断裂）。
            max_n = 0
            for rk in od:
                if rk.startswith("round_"):
                    suffix = rk[len("round_"):]
                    if suffix.isdigit():
                        max_n = max(max_n, int(suffix))
            od[f"round_{max_n + 1}"] = []

        # LRU：轮数超上限时淘汰最旧轮。
        # 兼容 dict / OrderedDict：Python 3.7+ 的普通 dict 也保持插入序，首键即最旧轮。
        # popitem(last=False) 仅 OrderedDict 支持，普通 dict 传关键字会抛
        # TypeError，统一改用「首键删除」等价淘汰最旧轮。
        while len(od) > self.max_rounds:
            od.pop(next(iter(od)))

        # 仅由入站群消息观察路径通知心跳，避免同一条 @ 消息在短期档案写入时重复计数。
        if chat_type == "group" and notify_group and role == "user" and self._on_group_message:
            self._on_group_message(chat_id)

        # 定期清理（7 天 TTL：短于 2h 会导致偶发用户对话上下文频繁丢失）
        now = time.time()
        if now - self._last_cleanup > 600:
            self._cleanup(3600 * 24 * 7)
            self._last_cleanup = now

        # P0-5 修复：运行期新增记忆必须落盘，否则重启即丢全部新对话。
        # 旧逻辑仅 ~10% 概率 save，崩溃窗口可能丢大量对话。改为阈值策略：
        # 累计写入 >=10 条 或 距上次落盘 >=60s 才整文件写，兼顾 IO 抖动与数据安全。
        # 每次 add 必走此路径（不再依赖随机），保证丢数据窗口有上界。
        self._dirty += 1
        now = time.time()
        if self._dirty >= 10 or (now - self._last_save) >= 60:
            self.save()

    def get_group_history(self, group_id: int, *, hours: float = 24.0,
                          limit: int = 200, now: Optional[float] = None) -> list[dict]:
        """按时间正序返回**某群**在时间窗内的消息（跨用户聚合）。

        🔴 2026-10-23（P2 后续）：这是给「整理群动态」这类**长期目标**用的。
        在此之前 Agent 的 28 个工具里**没有任何一个能读群聊记录**
        （`read_file` 只读 data 目录、`get_group_members` 只给成员列表），
        所以「盯着群动态」这个最自然的长期目标**结构上做不成** ——
        生产实证：Agent 两次如实报告「缺少读取聊天记录的 API 或工具」。

        与 `get_context` 的区别（**不能混用**）：
        - `get_context`：**单用户桶**、给对话注入用、会做翻旧账防护；
        - `get_group_history`：**跨用户聚合 + 时间窗过滤 + 按时间排序**，
          供「回顾群里发生了什么」用。

        安全边界：
        * 只读**指定群**，不跨群聚合（避免把别群内容带进来）；
        * 时间窗默认 24h、条数默认 200，**都有硬上限**，防一次拉爆；
        * 不返回 bot 自己的消息以外的私聊内容 —— 桶 key 已按 `group_{gid}_` 前缀过滤；
        * 输出结构 `{time, name, role, content}`，**不含** message_id 等内部字段。
        """
        now = time.time() if now is None else float(now)
        cutoff = now - max(0.0, float(hours)) * 3600.0
        prefix = f"group_{int(group_id)}_"
        rows: list[dict] = []
        for key, od in self._store.items():
            if not str(key).startswith(prefix):
                continue
            for _rid, entries in od.items():
                for entry in entries or []:
                    _t = entry.get("time") or entry.get("ts") or 0
                    try:
                        _t = float(_t)
                    except (TypeError, ValueError):
                        continue
                    # 无时间戳的条目无法定位，跳过（不猜）
                    if not _t or _t < cutoff:
                        continue
                    rows.append({
                        "time": _t,
                        "name": str(entry.get("name") or ("你" if entry.get("role") == "bot" else "?")),
                        "role": str(entry.get("role") or "user"),
                        "content": str(entry.get("content") or ""),
                    })
        rows.sort(key=lambda r: r["time"])
        if limit is not None and int(limit) > 0:
            rows = rows[-int(limit):]
        return rows

    def get_context(self, chat_type: str, chat_id: int,
                    recent_rounds: int = 10,
                    user_id: Optional[int] = None,
                    max_age_minutes: Optional[float] = None,
                    max_messages: Optional[int] = None) -> list[dict]:
        key = self._key(chat_type, chat_id, user_id)
        od = self._store.get(key, OrderedDict())
        if not od:
            return []

        rounds = list(od.items())[-recent_rounds:]
        messages: list[dict] = []
        now = time.time()
        for _, entries in rounds:
            for entry in entries:
                # 翻旧账防护：超过时间窗的持久记忆不注入上下文
                if max_age_minutes is not None:
                    _t = entry.get("time") or entry.get("ts")
                    if _t and (now - _t) > max_age_minutes * 60:
                        continue
                label = entry["name"] if entry["role"] == "user" else "你"
                text = entry["content"]
                vd = entry.get("vision_desc")
                if vd:
                    text = f"{text} [图片:{vd}]" if text else f"[图片:{vd}]"
                # 🔴 2026-10-25：**注入时净化 bot 自己的历史发言（只改送出去的，不改存盘）**。
                #
                # 生产实证：`data/chat_memory.json` 里 bot 条目含
                #   本大爷 47 处 / 老子 4 处 / 两脚兽 7 处
                # 而这里把 `role=bot` 映射成 `assistant` 送进 messages ⇒
                # **模型看到的是「我以前就是这么说话的」**。人格正本第 2-3 行
                # 明令禁止这些自称/贬称，但历史消息里全是反例 ——
                # 禁令与示范同时在场时，模型跟示范走（这就是 214 条出站里
                # 54 条含漂移词的机制）。
                #
                # 为什么不改存档：那是**原始对话记录**，改它等于篡改历史
                # （且 `_deliver_text` 之类审计会失去依据）。只在注入面净身。
                #
                # 只处理 bot 条目：user 条目是**别人的原话**，一个字都不能动。
                if entry.get("role") == "bot":
                    text = _neutralise_self_alias(text)
                messages.append({
                    # OpenAI 兼容 Chat Completions 不认识 "bot"；存储层仍使用 bot
                    # 以兼容旧数据，但送给模型时必须映射为 assistant。
                    "role": "assistant" if entry["role"] == "bot" else entry["role"],
                    "content": f"[{label}]: {text}",
                })
        if max_messages is not None and len(messages) > max_messages:
            messages = messages[-max_messages:]
        return messages

    def observe_group_message(self, group_id: int, user_id: int, sender_name: str,
                              content: str, message_id: object = None,
                              *, addressed_to_bot: bool = False,
                              quote_from: Optional[str] = None) -> None:
        """记录所有入站群消息：短时群上下文 + 持久化个人桶。

        持久化写入确保即使消息被回复闸门跳过，对话上下文也不会丢失。

        `addressed_to_bot`：这条消息是否**直接叫到 bot**（@ / 引用 / 正文喊「哈基米」），
        由调用方用 `group_gate.is_direct_group_call`（回复闸门的同一信号）判定。
        只在短时群缓冲上落 `to_bot` 标记 —— 个人桶是 (群,用户) 隔离的，没有归属歧义。

        `quote_from`（2026-09-28）：本条是**引用回复**时，被引用者的称呼
        （引用 bot 自己时为「哈基米」）。有值时在群缓冲正文前加 `[引用 X]` ——
        否则下一轮回看历史时，「X 引用别人的话」与「X 自己说的话」**同形**，
        bot 事后仍会坚持「就是他说的」，无法自我纠正。
        🔴 只落**短时缓冲**、不进 (群,用户) 持久桶：持久桶是「用户原话」的长期记录，
        不宜被引注改写（也避免历史条目永久变长）。
        """
        self._purge_group_recent(group_id)
        text = (content or "").strip()
        if not text:
            return
        # 剥离 CQ 码等传输层载荷，与 memory_stage 的 _clean_transport_text 一致。
        # 群缓冲此前存的是**未清洗的 raw** ⇒ 带引用回复的消息会把 `[CQ:reply,id=…]`
        # 带进 LLM 上下文（削弱可辨识度）。两个落点（缓冲 + 个人桶）现在同源。
        clean_text = _CQ_TRANSPORT_RE.sub("", text).strip() or "[用户发送了图片]"
        _qf = str(quote_from or "").strip()
        _buf_text = f"[引用 {_qf}] {clean_text[:2000]}" if _qf else clean_text[:2000]
        # 1) 短时群上下文（内存，15min TTL）
        self._group_recent[int(group_id)].append({
            "role": "user",
            "content": f"[{sender_name}({user_id})]: {_buf_text}",
            "message_id": str(message_id) if message_id is not None else None,
            "to_bot": bool(addressed_to_bot),
            "time": time.time(),
        })
        # 2) 持久化个人桶 —— 保证被闸门跳过的消息仍保留在对话记忆中
        self.add("group", int(group_id), "user", sender_name, clean_text[:2000],
                 user_id=int(user_id))
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

    def observe_bot_media(self, detail_type: str, chat_id: Any, note: str,
                          *, group_id: Any = None, user_id: Any = None) -> None:
        """记录自己**实际发出**的媒体（当前：表情包），让后续回合「知道自己发过什么」。

        与 `observe_group_bot_reply` 平行（同一行格式），但补上它缺的两条通道：
          - 群聊：写进群短时缓冲 —— 这是模型每轮 `group_history` 的来源；
          - 群聊/私聊：都写进 (chat,user) 持久桶 —— 这是 `get_context` 的来源。
        背景：表情包以 image 段单独成条发出，`observe_group_bot_reply` 只收文本
        ⇒ 此前表情包对自己完全不可见（用户追问「你刚发的那张」时会失忆）。

        纯留痕、不改任何行为；空 note 直接忽略。
        """
        text = (note or "").strip()
        if not text:
            return
        text = text[:2000]
        if detail_type == "group" and group_id is not None:
            self._purge_group_recent(group_id)
            self._group_recent[int(group_id)].append({
                "role": "assistant",
                "content": f"[哈基米]: {text}",
                "message_id": None,
                # 🔴 纯媒体行：`get_recent_group_context(annotate=True)` 据此**不**把它
                # 当作「已答」。否则「文本为空 + 被动附带一张表情」的回合会在群缓冲里
                # 多出一条 assistant 行，把别人尚未被答的提问误判成"已被回答"，
                # 反而削弱 09-21 那次的防跨用户污染标注。
                "media_only": True,
                "time": time.time(),
            })
        if chat_id is not None:
            self.add(detail_type, chat_id, "bot", "Anima", text, user_id=user_id)

    def get_recent_group_context(self, group_id: int, limit: int = 12,
                                 exclude_message_id: object = None,
                                 *, annotate: bool = False) -> list[dict]:
        """返回按时间正序的短时群上下文，可排除本轮正在处理的消息。

        `annotate=True`（**opt-in**）给「非本轮」条目加归属前缀，用于修复群上下文
        跨用户污染：
          - 直接对 bot 说、且其后**没有**任何 assistant 回复 ⇒ `[旁听·他人提问·非本轮]`
            （别人的、还没被答的提问，正是模型误答的对象）
          - 其余群友发言                                       ⇒ `[旁听]`
          - 已有 assistant 回复的旧提问**不打标**（那是真实发生过的对话史）
        只改 `content`，**`role` 仍是 user/assistant** —— 网关只认三种 role，
        多轮交替结构也不能破。

        🔴 `annotate` 默认 False 是硬约束：本函数有 5 个消费者
        （`pipeline/stages/memory.py`、`core/autonomy.py` 两处、
        `constitution/memory_engine.py`、`get_group_context`），只有第一个需要标注。
        """
        self._purge_group_recent(group_id)
        excluded = str(exclude_message_id) if exclude_message_id is not None else None
        rows = [m for m in self._group_recent.get(int(group_id), ())
                if not excluded or m.get("message_id") != excluded]
        rows = rows[-max(0, limit):]
        if not annotate:
            # 与 `get_context` 同一净化点：bot 历史发言里的自称/贬称漂移词
            # （本函数有 5 个消费者，是群语境的**主入口** —— 必须一起净）
            return [
                {"role": m["role"],
                 "content": (_neutralise_self_alias(m["content"])
                             if m["role"] == "assistant" else m["content"])}
                for m in rows
            ]
        annotated: list[dict] = []
        pending = 0
        for i, m in enumerate(rows):
            content = m["content"]
            if m["role"] == "assistant":
                content = _neutralise_self_alias(content)
            if m["role"] == "user":
                if m.get("to_bot"):
                    # 纯媒体行（`media_only`，如「发了一张表情包」）**不算「已答」**：
                    # 一张表情并不构成对那条提问的回答，若算作已答会把他人待答的
                    # 提问误判成对话史，削弱本标注（09-21 跨用户污染事故的修复）。
                    if not any(r["role"] == "assistant" and not r.get("media_only")
                               for r in rows[i + 1:]):
                        content = _BYSTAND_PENDING_PREFIX + content
                        pending += 1
                else:
                    content = _BYSTAND_PREFIX + content
            annotated.append({"role": m["role"], "content": content})
        if pending:
            # 只在命中「他人待答」（= 本次事故的条件）时留痕：这是本修复唯一的运行时
            # 证据面（群缓冲不落盘），同时也是观察"并发 @bot 窗口"是否被踩到的探针。
            # 纯闲聊旁听属常态，不打日志以免淹没 INFO 面。
            logger.info(
                "群上下文归属标注：%d 条「他人待答」已标为非本轮（群 %s，共 %d 条）"
                "—— 该群本回合存在并发 @bot 窗口",
                pending, group_id, len(rows),
            )
        return annotated

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

    def recent_bot_lines(self, group_id: int, limit: int = 6,
                         max_age_sec: float = 24 * 3600) -> list[str]:
        """该群最近若干条 **bot 自己的出站正文**（时间倒序，去重）。

        为什么要这个方法（2026-10-18 生产实证）：
          * 10-17 23:56 / 10-18 00:32 / 06:00 三条回复是**同一句式骨架**
            （「又丢这种…链接过来……本喵才不…，要看你自己看去喵」）。
            根因是模型完全看不到"自己刚说过什么"——`observe_group_bot_reply`
            只写短时缓冲（15 分钟 TTL），跨小时就查不到。
          * 10-18 00:32 说「才不帮你看」、06:32 议程跟进却说「勉为其难帮你瞅了
            一眼」——同一链接两次表态相反，同因。

        与 `get_group_context` 的区别：那个是**混合**上下文且带 `[名字]:` 前缀；
        这里只要 bot 自己的裸正文，供"防复读 / 防自相矛盾"直接喂回 prompt。

        只读 `_store`，不写盘、不发网络请求；`role == "bot"` 才算。
        """
        cutoff = time.time() - float(max_age_sec)
        found: list[tuple[float, str]] = []
        for key in self._group_user_keys(group_id):
            od = self._store.get(key) or {}
            for _round_key, entries in od.items():
                for entry in entries or []:
                    if entry.get("role") != "bot":
                        continue
                    try:
                        if float(entry.get("time") or 0.0) < cutoff:
                            continue
                    except (TypeError, ValueError):
                        continue
                    text = str(entry.get("content") or "").strip()
                    if text:
                        found.append((float(entry.get("time") or 0.0), text))
        found.sort(key=lambda x: x[0], reverse=True)
        out: list[str] = []
        seen: set[str] = set()
        for _t, text in found:
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= max(1, int(limit)):
                break
        return out

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
                    # 与 `get_context` 同一净化点（bot 历史发言里的自称/贬称漂移词）
                    _text = str(entry["content"] or "")
                    if entry["role"] == "bot":
                        _text = _neutralise_self_alias(_text)
                    collected.append({
                        "role": "assistant" if entry["role"] == "bot" else entry["role"],
                        "content": f"[{label}]: {_text}",
                        "_t": entry.get("time", 0.0),
                    })
        collected.sort(key=lambda m: m.pop("_t"))
        # 粗略裁剪到 recent_rounds * 4 条，控制 token
        return collected[: recent_rounds * 4]

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
        if stale:
            logger.info("chat_memory cleanup: 清除 %d 个过期桶 (TTL=%.1fh): %s",
                        len(stale), ttl / 3600, stale)


# 全局单例（P2-A：默认落盘路径收口——无参调用不再锚死空路径导致永不落盘）
_default_persist_path: str = ""
_chat_memory: Optional[ChatMemory] = None


def set_chat_memory_persist_path(path: str) -> None:
    """把 get_chat_memory() 的默认落盘路径钉到配置目录下的 chat_memory.json。"""
    global _default_persist_path
    _default_persist_path = path or ""


def get_chat_memory(persist_path: str = "") -> ChatMemory:
    global _chat_memory
    pp = persist_path or _default_persist_path
    if _chat_memory is None:
        _chat_memory = ChatMemory(persist_path=pp)
    return _chat_memory


def reset_chat_memory() -> None:
    """Test seam: discard the process-global singleton so each test starts clean.

    生产代码绝不调用本函数；它仅用于测试隔离，避免跨测试/跨 case 共享同一份
    群聊短时上下文导致非确定性（见 integration-stability Phase 1 诊断）。
    """
    global _chat_memory
    _chat_memory = None
