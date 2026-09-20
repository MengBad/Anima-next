"""多 Bot 社交系统（Anima-next）。

职责：
- 识别其他 Bot（基于已知 UID 列表 / OneBot11 role / 事件标记）
- 维护 Bot 关系图谱（bot_relations.json：熟悉度/竞争度/信任度 + 演化）
- 防 Bot 循环（gate 调用 classify_bot_reply_skip：未@我忽略、冷却、循环截断）
- 群角色分工（bot_roles.json：专长 / 让出）
- 可选：Bot 间私聊（bot_whisper.py）、心跳主动找 Bot 说话

设计原则：所有 Bot 私有数据落在 data/ 下独立 JSON，绝不污染 chat_memory.json
（人类对话历史）。循环/冷却是生产级 Critical 防护，异常一律放行不阻塞正常回复。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from anima.util.name_clean import sender_display_name

logger = logging.getLogger("anima.cognitive.bot_social")

_BEIJING = timezone(timedelta(hours=8))

# ── 默认种子 Bot 角色（已知群 Bot；data/bot_roles.json 可覆盖）──
# 注意：这里只描述「能力/分工」，不用于判定「是不是 bot」（那走 known_bot_ids/role）。
DEFAULT_BOT_ROLES: dict[str, dict] = {
    "2752719822": {  # 哈基米（本喵）
        "name": "哈基米",
        "specialties": ["聊天", "表情包", "情感陪伴", "群历史"],
        "avoid_topics": ["管理", "禁言", "踢人"],
    },
    "838916454": {  # 丰川刻子（群内另一只 Bot）
        "name": "丰川刻子",
        "specialties": ["管理", "头衔", "群规执行"],
        "avoid_topics": ["情感倾诉", "闲聊"],
    },
}

# 动态发现：昵称启发式（保守 allowlist，避免把真人误判为 Bot）
_BOT_NICKNAME_HINTS = ("bot", "robot", "机器人", "機器人", "aki", "刻子", "沙琪玛")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class BotRelation:
    bot_uid: str
    bot_name: str = ""
    familiarity: float = 0.0       # 熟悉度（互动频率）
    rivalry: float = 0.0           # 竞争度（抢话、改头衔等冲突）
    trust: float = 0.0             # 信任度（是否合作）
    last_interaction: float = 0.0
    interaction_count: int = 0
    notable_events: list[str] = field(default_factory=list)

    def clamp(self) -> None:
        for fld in ("familiarity", "rivalry", "trust"):
            v = float(getattr(self, fld))
            setattr(self, fld, max(0.0, min(1.0, v)))


def _bj_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, _BEIJING).strftime("%Y-%m-%d")


def _days_between(a: float, b: float) -> int:
    if not a or not b:
        return 0
    return max(0, (datetime.fromtimestamp(b, _BEIJING).date()
                   - datetime.fromtimestamp(a, _BEIJING).date()).days)


# ---------------------------------------------------------------------------
# 原子 JSON 读写工具
# ---------------------------------------------------------------------------
_file_lock = threading.Lock()


def _atomic_write(path: Path, data: dict) -> None:
    from anima.core.persistence import atomic_write_text
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _load_json(path: Path, default: dict) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 %s 失败，已用默认值: %s", path, e)
    return default


# ---------------------------------------------------------------------------
# Bot 关系图谱
# ---------------------------------------------------------------------------
class BotRelationStore:
    """bot_relations.json：熟悉度 / 竞争度 / 信任度 + 每日衰减。"""

    def __init__(self, data_dir: str = "data"):
        self.path = Path(data_dir) / "bot_relations.json"
        self._data: dict[str, Any] = {}
        self._relations: dict[str, BotRelation] = {}
        self._meta: dict[str, Any] = {}
        self._loaded = False
        self.load()

    def load(self) -> None:
        with _file_lock:
            raw = _load_json(self.path, {"relations": {}, "meta": {}})
            self._meta = raw.get("meta", {}) or {}
            self._relations = {}
            for uid, d in (raw.get("relations", {}) or {}).items():
                try:
                    self._relations[str(uid)] = BotRelation(**{k: d.get(k, v) for k, v in _DEFAULT_REL_FIELDS(uid).items()})
                except Exception:  # noqa: BLE001
                    logger.debug("关系记录解析失败 uid=%s，已跳过", uid)
            self._loaded = True

    def save(self) -> None:
        with _file_lock:
            payload = {
                "relations": {uid: asdict(rel) for uid, rel in self._relations.items()},
                "meta": self._meta,
            }
            _atomic_write(self.path, payload)

    # ── 读取 ──
    def get(self, uid: str) -> Optional[BotRelation]:
        return self._relations.get(str(uid))

    def ensure(self, uid: str, name: str = "") -> BotRelation:
        uid = str(uid)
        rel = self._relations.get(uid)
        if rel is None:
            rel = BotRelation(bot_uid=uid, bot_name=name or uid)
            self._relations[uid] = rel
        elif name:
            # bot_name 仅作展示：改名时刷新展示名，但关系主键是 UID，不受影响
            rel.bot_name = name
        return rel

    # ── 演化 ──
    def record_interaction(self, uid: str, name: str | None, kind: str, now: float | None = None) -> Optional[BotRelation]:
        now = now or time.time()
        rel = self.ensure(str(uid), name or "")
        # 每日衰减（按自然日差）
        if rel.last_interaction:
            d = _days_between(rel.last_interaction, now)
            if d > 0:
                rel.familiarity = max(0.0, rel.familiarity - 0.01 * d)
        if kind == "mentioned_me":
            rel.familiarity = min(1.0, rel.familiarity + 0.1)
            rel.notable_events.append(f"{_bj_day(now)} 被 @")
        elif kind == "rivalry":
            rel.rivalry = min(1.0, rel.rivalry + 0.3)
            rel.trust = max(0.0, rel.trust - 0.2)
            rel.notable_events.append(f"{_bj_day(now)} 头衔/冲突")
        elif kind == "help":
            rel.trust = min(1.0, rel.trust + 0.2)
            rel.notable_events.append(f"{_bj_day(now)} 被帮助")
        elif kind == "chat":
            rel.familiarity = min(1.0, rel.familiarity + 0.05)
        rel.familiarity = max(0.0, min(1.0, rel.familiarity))
        rel.rivalry = max(0.0, min(1.0, rel.rivalry))
        rel.trust = max(0.0, min(1.0, rel.trust))
        rel.last_interaction = now
        rel.interaction_count += 1
        rel.notable_events = rel.notable_events[-10:]
        self.save()
        return rel

    def get_relation_context(self, uid: str) -> Optional[str]:
        rel = self.get(str(uid))
        return build_relation_context(rel) if rel else None


def _DEFAULT_REL_FIELDS(uid: str) -> dict:
    return {
        "bot_uid": uid,
        "bot_name": "",
        "familiarity": 0.0,
        "rivalry": 0.0,
        "trust": 0.0,
        "last_interaction": 0.0,
        "interaction_count": 0,
        "notable_events": [],
    }


# ---------------------------------------------------------------------------
# Bot 冷却（防循环）
# ---------------------------------------------------------------------------
class BotCooldownStore:
    """bot_cooldown.json：每个 Bot 的沉默冷却截止时间 + 私聊日限额。"""

    def __init__(self, data_dir: str = "data"):
        self.path = Path(data_dir) / "bot_cooldown.json"
        self._cooldowns: dict[str, float] = {}
        self._meta: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with _file_lock:
            raw = _load_json(self.path, {"cooldowns": {}, "meta": {}})
            self._meta = raw.get("meta", {}) or {}
            self._cooldowns = {str(k): float(v) for k, v in (raw.get("cooldowns", {}) or {}).items()}

    def save(self) -> None:
        with _file_lock:
            _atomic_write(self.path, {"cooldowns": self._cooldowns, "meta": self._meta})

    def set_cooldown(self, uid: str, until_ts: float) -> None:
        self._cooldowns[str(uid)] = float(until_ts)
        self.save()

    def is_in_cooldown(self, uid: str, now: float | None = None) -> bool:
        now = now or time.time()
        until = self._cooldowns.get(str(uid))
        return bool(until) and now < until

    # ── 私聊日限（Task 6）──
    def whisper_count_today(self, now: float | None = None) -> int:
        now = now or time.time()
        if self._meta.get("whisper_day") != _bj_day(now):
            self._meta["whisper_day"] = _bj_day(now)
            self._meta["whisper_count"] = 0
        return int(self._meta.get("whisper_count", 0))

    def bump_whisper_count(self, now: float | None = None) -> int:
        now = now or time.time()
        if self._meta.get("whisper_day") != _bj_day(now):
            self._meta["whisper_day"] = _bj_day(now)
            self._meta["whisper_count"] = 0
        self._meta["whisper_count"] = int(self._meta.get("whisper_count", 0)) + 1
        self.save()
        return int(self._meta["whisper_count"])


# ---------------------------------------------------------------------------
# Bot 角色分工
# ---------------------------------------------------------------------------
class BotRolesStore:
    """bot_roles.json：每个 Bot 的专长 / 回避话题（用于让出分工）。"""

    def __init__(self, data_dir: str = "data"):
        self.path = Path(data_dir) / "bot_roles.json"
        self._roles: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        with _file_lock:
            raw = _load_json(self.path, {"roles": {}})
            self._roles = dict(DEFAULT_BOT_ROLES)
            for uid, d in (raw.get("roles", {}) or {}).items():
                if isinstance(d, dict):
                    self._roles[str(uid)] = d

    def save(self) -> None:
        with _file_lock:
            _atomic_write(self.path, {"roles": self._roles})

    def get_roles(self, uid: str) -> dict:
        return self._roles.get(str(uid), {})

    def specialties(self, uid: str) -> list[str]:
        return list(self._roles.get(str(uid), {}).get("specialties", []))

    def avoid_topics(self, uid: str) -> list[str]:
        return list(self._roles.get(str(uid), {}).get("avoid_topics", []))


# 话题分类（轻量关键词，用于分工让出）
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "管理": ("禁言", "踢人", "踢出", "头衔", "群规", "管理", "ban", "封禁", "拉黑"),
    "表情包": ("表情包", "表情", "梗图", "沙雕图", "动图", "gif"),
    "情感陪伴": ("安慰", "陪我", "好难过", "好孤单", "心情不好", "倾诉", "不开心"),
    "群历史": ("上次", "之前说过", "记录", "记得吗", "群历史", "谁说过"),
}


def classify_topic(text: str) -> Optional[str]:
    """把用户问题归到某 Bot 的专长话题，无法归类返回 None。"""
    if not text:
        return None
    low = (text or "").lower()
    for topic, kws in _TOPIC_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in low:
                return topic
    return None


def should_delegate(text: str, other_bot_online: bool = True, roles_store: BotRolesStore | None = None) -> bool:
    """判断本喵是否应把这个问题让给其他专长 Bot。

    - 话题命中某「其他 Bot」的 specialties，且该 Bot 在线 → 让出（True）
    - 无在线其他 Bot 可接管 → 不让出（False，本喵自己答）
    """
    if not other_bot_online:
        return False
    topic = classify_topic(text or "")
    if not topic:
        return False
    store = roles_store or BotRolesStore()
    for uid, info in store._roles.items():
        specs = info.get("specialties", [])
        if topic in specs and "聊天" not in specs:  # 避免「聊天」万能兜底吃掉一切
            return True
    return False


# ---------------------------------------------------------------------------
# 已知 Bot 注册表（UID 为唯一主键）
# ---------------------------------------------------------------------------
def _canon_uid(uid: Any) -> Optional[str]:
    try:
        return str(int(uid))
    except (TypeError, ValueError):
        return None


class KnownBotsStore:
    """data/known_bots.json：以 UID(QQ号) 为唯一主键的已确认 Bot 注册表。

    设计铁律：
    - UID 是唯一真相来源；改名/换昵称不影响识别（主键是 UID，不是昵称）。
    - 昵称启发式 NEVER 写入本注册表（只作日志建议），避免把真人误判为 Bot。
    - 只有配置 known_bot_ids 或显式确认（confirmed=true）的 UID 才生效。
    """

    def __init__(self, data_dir: str = "data"):
        self.path = Path(data_dir) / "known_bots.json"
        self._bots: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        with _file_lock:
            if not self.path.exists():
                # 惰性物化空注册表，明确 schema（UID 为键 + confirmed 字段）
                _atomic_write(self.path, {"bots": {}})
            raw = _load_json(self.path, {"bots": {}})
            self._bots = {}
            for uid, d in (raw.get("bots", {}) or {}).items():
                if not isinstance(d, dict):
                    continue
                u = _canon_uid(uid)
                if u is None:
                    continue
                self._bots[u] = {
                    "bot_name": str(d.get("bot_name", "") or ""),
                    "confirmed": bool(d.get("confirmed", True)),
                }

    def save(self) -> None:
        with _file_lock:
            _atomic_write(self.path, {"bots": self._bots})

    def is_known(self, uid: Any) -> bool:
        u = _canon_uid(uid)
        if u is None:
            return False
        return bool(self._bots.get(u, {}).get("confirmed"))

    def add(self, uid: Any, name: str = "", *, confirmed: bool = True) -> None:
        u = _canon_uid(uid)
        if u is None:
            return
        entry = self._bots.get(u, {"bot_name": "", "confirmed": True})
        if name:
            entry["bot_name"] = name
        entry["confirmed"] = bool(confirmed)
        self._bots[u] = entry
        self.save()

    def uids(self) -> set[int]:
        return {int(u) for u, d in self._bots.items() if d.get("confirmed")}


_KNOWN_BOTS_CACHE: dict[str, tuple[float, "KnownBotsStore"]] = {}
_KNOWN_BOTS_TTL = 15.0


def get_known_bots_store(data_dir: str = "data") -> "KnownBotsStore":
    now = time.time()
    cached = _KNOWN_BOTS_CACHE.get(data_dir)
    if cached and (now - cached[0]) < _KNOWN_BOTS_TTL:
        return cached[1]
    store = KnownBotsStore(data_dir)
    _KNOWN_BOTS_CACHE[data_dir] = (now, store)
    return store


def authoritative_known_bot_uids(
    known_ids: "set[int] | list[int] | None" = None, data_dir: str = "data"
) -> "set[int]":
    """UID 权威集 = 配置 known_bot_ids ∪ known_bots.json(confirmed)。

    识别其他 Bot 时只用这个集合做判定；昵称启发式不在此列。
    """
    s: set[int] = {int(x) for x in (known_ids or set())}
    try:
        s |= get_known_bots_store(data_dir).uids()
    except Exception:  # noqa: BLE001
        logger.warning("合并 known_bots.json 失败，仅用配置集")
    return s


# ---------------------------------------------------------------------------
# Bot 识别
# ---------------------------------------------------------------------------
def is_bot_uid(uid: Any, known_ids: set[int] | None = None) -> bool:
    if uid is None:
        return False
    try:
        u = int(uid)
    except (TypeError, ValueError):
        return False
    if known_ids:
        return u in {int(x) for x in known_ids}
    return False


def is_bot_sender(event: dict | None, known_ids: set[int] | None = None) -> bool:
    """判定入站消息发送者是否为其他 Bot。

    优先级：事件标记（onebot11 已基于 known_bot_ids/role 算好）> role=="bot" > known_ids。
    """
    if not isinstance(event, dict):
        return False
    if event.get("_is_bot_sender") or event.get("sender_is_bot"):
        return True
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    if str(sender.get("role", "")).lower() == "bot":
        return True
    uid = event.get("user_id")
    if uid is not None and known_ids:
        try:
            return int(uid) in {int(x) for x in known_ids}
        except (TypeError, ValueError):
            return False
    return False


def discover_bots_from_members(members: list[dict], strict: bool = True) -> set[int]:
    """按群成员列表发现 Bot UID。

    仅返回平台显式标记 role=='bot' 的 UID（平台真相，非启发式）。
    昵称命中启发式的 UID 一律只记日志建议（见 suggest_bots_from_nickname），
    绝不自动确认、绝不进入返回集 —— UID 才是唯一主键，避免把真人误判为 Bot。
    """
    found: set[int] = set()
    for m in members or []:
        if not isinstance(m, dict):
            continue
        uid = m.get("user_id")
        if uid is None:
            continue
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        if str(m.get("role", "")).lower() == "bot":
            found.add(uid)
            continue
        nick = str(m.get("nickname", "")).lower()
        if any(h.lower() in nick for h in _BOT_NICKNAME_HINTS):
            # 仅作日志建议，不自动确认、不加入返回集
            logger.info(
                "[多Bot发现-建议] 成员 %s(nick=%s) 昵称疑似 Bot，但仅作人工确认建议，"
                "不会自动识别——UID 才是唯一主键。", uid, m.get("nickname")
            )
    return found


def suggest_bots_from_nickname(members: list[dict]) -> list[int]:
    """返回昵称疑似 Bot 的 UID 列表（仅供日志 / 人工确认建议，绝不自动确认）。"""
    out: list[int] = []
    for m in members or []:
        if not isinstance(m, dict):
            continue
        uid = m.get("user_id")
        if uid is None:
            continue
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        nick = str(m.get("nickname", "")).lower()
        if any(h.lower() in nick for h in _BOT_NICKNAME_HINTS):
            out.append(uid)
    return out


# ---------------------------------------------------------------------------
# 防循环：入站环缓冲
# ---------------------------------------------------------------------------
_recent_lock = threading.Lock()
_recent: dict[int, list[tuple[float, int, bool, bool]]] = {}
_RECENT_MAX = 40


def observe_inbound(group_id: Any, uid: Any, is_bot: bool, to_self: bool, now: float | None = None) -> None:
    """记录一条入站消息（含人类），供循环检测构建连续 Bot 链。"""
    if group_id is None or uid is None:
        return
    try:
        gid = int(group_id)
        u = int(uid)
    except (TypeError, ValueError):
        return
    now = now or time.time()
    with _recent_lock:
        buf = _recent.setdefault(gid, [])
        buf.append((now, u, bool(is_bot), bool(to_self)))
        if len(buf) > _RECENT_MAX:
            del buf[: len(buf) - _RECENT_MAX]


def detect_bot_loop(group_id: Any, self_id: Any = None, max_depth: int = 2, now: float | None = None) -> bool:
    """检查最近连续 Bot 消息是否形成「Bot 互 @ 循环」。

    在 observe_inbound(当前消息) 之后调用；当前 Bot 消息已在缓冲中。
    判定：最近连续（直到被人类消息打断）的 Bot 消息数 ≥ max_depth，
    且涉及 ≥2 个不同 Bot UID（即 Bot 之间在互相 @，而非单个 Bot 反复 ping 我——
    后者由冷却机制按 30s/次 限频，不会无限循环）。
    """
    if group_id is None:
        return False
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return False
    with _recent_lock:
        hist = list(_recent.get(gid, []))
    chain: list[int] = []
    for _ts, u, is_bot, _to in reversed(hist):
        if not is_bot:
            break  # 人类消息打断循环
        chain.append(u)
    if len(chain) >= max_depth and len(set(chain)) >= 2:
        return True
    return False


# ---------------------------------------------------------------------------
# Gate 集成：Bot 回复抑制
# ---------------------------------------------------------------------------
def classify_bot_reply_skip(
    event: dict,
    text: str,
    *,
    self_id: Any = None,
    now: float | None = None,
    data_dir: str = "data",
    known_ids: set[int] | None = None,
    cooldown_sec: int = 30,
    max_depth: int = 2,
) -> str | None:
    """群消息若来自其他 Bot，返回跳过原因；否则返回 None（放行）。

    原因码：bot_ignore（未@我）| bot_cooldown（冷却中）| bot_loop（循环）
    放行时会：记录熟悉度 + 设 30s 冷却，避免无限互 @。
    """
    if not is_bot_sender(event, known_ids):
        return None
    now = now or time.time()
    sender_uid = str(event.get("user_id"))
    mentions_self = bool(event.get("mentions_self") or event.get("reply_to_self"))
    if not mentions_self:
        return "bot_ignore"
    cool = BotCooldownStore(data_dir)
    if cool.is_in_cooldown(sender_uid, now):
        return "bot_cooldown"
    if detect_bot_loop(event.get("group_id"), self_id, max_depth, now):
        return "bot_loop"
    # 放行：更新关系 + 设冷却
    # 🔴 群名片优先（2026-09-20）：此前直读 `sender["nickname"]` ⇒ 记录的是对方的
    #   **QQ 昵称**而非本群显示名，与 handler/commands 那个「Rezon vs Aki」同源。
    name = sender_display_name(event.get("sender"), sender_uid)
    try:
        BotRelationStore(data_dir).record_interaction(sender_uid, name, "mentioned_me", now)
        cool.set_cooldown(sender_uid, now + cooldown_sec)
    except Exception:  # noqa: BLE001
        logger.exception("Bot 关系/冷却更新失败（放行不受影响）")
    return None


# ---------------------------------------------------------------------------
# Task 4：关系上下文文案
# ---------------------------------------------------------------------------
def build_relation_context(rel: BotRelation) -> str:
    """根据关系状态生成注入 system 的提示（不含前缀标签）。"""
    if rel.rivalry > 0.5:
        return (
            f"对方是 {rel.bot_name or rel.bot_uid}，你们关系紧张（竞争度 {rel.rivalry:.1f}）。"
            f"回应可以带刺、吐槽，但不要人身攻击、不要真的辱骂。"
        )
    if rel.trust > 0.5:
        return (
            f"对方是 {rel.bot_name or rel.bot_uid}，你们关系不错（信任度 {rel.trust:.1f}）。"
            f"可以撒娇或合作，自然接话。"
        )
    return (
        f"对方是 {rel.bot_name or rel.bot_uid}，你们不太熟（熟悉度 {rel.familiarity:.1f}）。"
        f"保持礼貌但疏远，不必热络。"
    )


# ---------------------------------------------------------------------------
# 心跳主动找 Bot 说话（每日最多一次，rivalry>0.3）
# ---------------------------------------------------------------------------
def pick_bot_talk_target(data_dir: str = "data", now: float | None = None) -> Optional[dict]:
    """选一个 rivalry>0.3 的 Bot 作为今日「主动找它说话」目标；每日仅一次。"""
    now = now or time.time()
    today = _bj_day(now)
    store = BotRelationStore(data_dir)
    if store._meta.get("last_bot_talk_day") == today:
        return None
    best: Optional[BotRelation] = None
    for rel in store._relations.values():
        if rel.rivalry > 0.3 and (best is None or rel.rivalry > best.rivalry):
            best = rel
    if best is None:
        return None
    store._meta["last_bot_talk_day"] = today
    store.save()
    return {"uid": best.bot_uid, "name": best.bot_name or best.bot_uid}


def reset_bot_talk_today(data_dir: str = "data") -> None:
    """测试/调试用：清除今日已说话标记。"""
    store = BotRelationStore(data_dir)
    store._meta.pop("last_bot_talk_day", None)
    store.save()
