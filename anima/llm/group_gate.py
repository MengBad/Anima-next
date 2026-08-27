"""群消息触发判定 —— mention / familiar / open 三级门控。

从 llm/handler.py 提取（Phase 2.2），职责：
- 判断群消息是否直接叫到了 bot（@/引用/喊名字）
- 解析群回复模式（mention | familiar | open）
- 熟人模式 Gate B：过滤超短闲聊/无意义短句
- 不抢其它 bot 的话（句首点名其它 bot 时静默）
- 综合判定是否进入完整回复流水线
"""

from __future__ import annotations
import logging
import re
import time
from typing import Any

logger = logging.getLogger("anima.llm.group_gate")

BOT_DISPLAY_NAME = "哈基米"


def is_direct_group_call(event: dict, text: str) -> bool:
    """判断群消息是否直接叫到了 bot。

    QQ 的 @ / reply 段是强触发；群友直接喊「哈基米」也应自然接话。
    其他群聊仅作短时上下文观察，避免 bot 误入他人的对话。
    """
    return bool(
        event.get("mentions_self")
        or event.get("reply_to_self")
        or BOT_DISPLAY_NAME in (text or "")
    )


def resolve_group_reply_mode(group_policy: dict | None, autonomy) -> str:
    """解析群回复模式：mention | familiar | open。

    兼容旧字段 reply_only_when_mentioned；group_policies 可显式指定 reply_mode。
    """
    policy = group_policy if isinstance(group_policy, dict) else {}
    mode = str(policy.get("reply_mode") or "").strip().lower()
    if mode in {"mention", "familiar", "open"}:
        return mode
    if "reply_only_when_mentioned" in policy:
        return "mention" if bool(policy.get("reply_only_when_mentioned")) else "open"
    return "mention" if bool(getattr(autonomy, "reply_only_when_mentioned", True)) else "open"


_CQ_STRIP_RE = re.compile(r"\[CQ:[^\]]+\]", re.I)
# 熟人模式 Gate B：无直接呼叫时，这些短闲聊/调情不进入完整回复（避免「约吗」误接）。
_FAMILIAR_IDLE_PHRASES = frozenset({
    "约吗", "约嘛", "月吗", "月嘛", "在吗", "在嘛", "你好", "嗨", "哈喽", "hello", "hi",
    "嗯", "嗯嗯", "好", "好的", "好吧", "行", "行吧", "哦", "噢", "额", "呃",
    "哈哈哈", "哈哈", "呵呵", "666", "牛", "草", "卧槽", "靠", "？", "?", "？？", "??",
    "在干嘛", "干嘛", "干什么", "咋了", "怎么了", "没事", "随便", "都行", "看看",
    "啊", "呀", "哇", "嘿", "喂", "哼", "切", "得了", "算了", "滚", "爬",
    "傻逼", "煞笔", "沙煲", "脑残", "垃圾", "去死", "蠢", "笨蛋",
    "肺雾", "我没叫你", "没叫你", "关你啥事", "关你什么事",
})
# 熟人未点名时：超过此长度才考虑插话（原 4 字太松，导致乱接闲聊）。
_FAMILIAR_IDLE_MAX_CHARS = 12
# 对方示意对话结束的信号：命中后 bot 不再连环追着说。
# 规则：硬缺口——若群友已明示「不聊/睡了/别再回复/下线/晚安/收工」等收尾，
# bot 除非被直接点名需要实际回应，否则不接不追，避免把聊天拖成一串互怼。
_END_SIGNAL_PATTERNS = (
    "不聊了", "别回了", "别再回", "别回复", "别理", "消停", "闭嘴", "收工",
    "下线", "去睡了", "睡了", "晚安", "睡了喵", "关了", "关机", "撤了", "走了",
    "先这样", "就到这", "到此为止", "不跟你聊", "不想说话", "不喜欢说了",
    "禁言", "别刷了", "别发了", "少说点", "慢走", "回聊", "明天再说", "改天聊",
)

_ENDSIG_RE = None


def _end_signal_pats() -> list[str]:
    global _ENDSIG_RE
    if _ENDSIG_RE is None:
        _ENDSIG_RE = tuple(sorted(_END_SIGNAL_PATTERNS, key=len, reverse=True))
    return _ENDSIG_RE


def is_conversation_end_signal(text: str) -> bool:
    """对方是否示意对话结束/收尾（用于 bot 停止连环追说的硬闸门）。"""
    plain = _plain_group_text(text or "")
    if not plain:
        return False
    compact = re.sub(r"[\s\W_]+", "", plain, flags=re.UNICODE)
    for sig in _end_signal_pats():
        if sig in plain or sig in compact:
            return True
    return False


# 群内其它 bot / 常被点名的同伴；句首点到他们且未叫本喵时不抢话。
_DEFAULT_OTHER_BOT_NAMES = (
    "沙琪玛", "沙其玛", "莎琪玛", "sakima", "Sakima", "SAKIMA",
    "aki", "Aki", "AKI", "阿基",
)


def _plain_group_text(text: str) -> str:
    """去掉 CQ 段后的纯文本，用于长度/短语闸门。"""
    return _CQ_STRIP_RE.sub("", text or "").strip()


def resolve_other_bot_names(group_policy: dict | None, autonomy) -> list[str]:
    """合并全局 + 群策略的其它 bot 名字（长名优先匹配）。"""
    names: list[str] = []
    for src in (
        getattr(autonomy, "other_bot_names", None),
        (group_policy or {}).get("other_bot_names") if isinstance(group_policy, dict) else None,
        _DEFAULT_OTHER_BOT_NAMES,
    ):
        if not src:
            continue
        if isinstance(src, str):
            names.append(src)
        else:
            try:
                names.extend(str(x) for x in src if str(x).strip())
            except TypeError:
                continue
    # 去重保序，排除本喵自称
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        n = str(n).strip()
        if not n or n == BOT_DISPLAY_NAME or n.lower() in seen:
            continue
        seen.add(n.lower())
        out.append(n)
    out.sort(key=len, reverse=True)
    return out


def is_addressed_to_other_bot(
    event: dict,
    text: str,
    *,
    other_names: list[str] | None = None,
    group_policy: dict | None = None,
    autonomy=None,
) -> bool:
    """是否点名了其它 bot/同伴且未叫到本喵。

    用于避免「沙琪玛，xxx」时哈基米抢话。直接 @/引用/喊哈基米 时返回 False。
    """
    if is_direct_group_call(event, text):
        return False
    names = other_names
    if names is None:
        names = resolve_other_bot_names(group_policy, autonomy)
    plain = _plain_group_text(text or "")
    if not plain or not names:
        return False
    # 去掉前导空白/@符号残片
    head = plain.lstrip(" 　@＠")
    for name in names:
        if not name:
            continue
        # 大小写不敏感的拉丁名；中文精确前缀
        if name.isascii():
            if not head.lower().startswith(name.lower()):
                continue
            rest = head[len(name):]
        else:
            if not head.startswith(name):
                continue
            rest = head[len(name):]
        # 「沙琪玛」单独成句 / 「沙琪玛，…」/ 「沙琪玛 今日运势」/ 「沙琪玛今日运势」
        if not rest:
            return True
        if rest[0] in "，,：:、；;!！?？ \t　":
            return True
        # 名字后直接接汉字/字母数字 → 仍视作点名（如 沙琪玛今日运势）
        ch = rest[0]
        if ch.isalnum() or ("\u4e00" <= ch <= "\u9fff"):
            return True
    return False


def is_familiar_idle_unaddressed(event: dict, text: str) -> bool:
    """Gate B：熟人模式下，未直接叫 bot 的超短/闲聊句不应进入回复流水线。

    - @ / 引用 bot / 含「哈基米」→ 不算 idle（由 is_direct_group_call 放行）
    - 正文里 @ 了别人且未 @ bot → 视为人对人，不接
    - 纯表情/空正文、≤12 字、或命中闲聊短语表 → idle
    - 纯辱骂/无任务短句 → idle（避免互怼刷屏）
    """
    if is_direct_group_call(event, text):
        return False
    raw = text or ""
    if re.search(r"\[CQ:at,", raw, re.I) and not event.get("mentions_self"):
        return True
    plain = _plain_group_text(raw)
    if not plain:
        # 纯图/纯表情：熟人模式也不自动长篇接话（避免误抢戏）；直接呼叫已在上面放行。
        return True
    compact = re.sub(r"[\s\W_]+", "", plain, flags=re.UNICODE)
    # 未点名时抬高门槛：短闲聊/半句不接，减少熟人乱插话
    if len(plain) <= _FAMILIAR_IDLE_MAX_CHARS or len(compact) <= _FAMILIAR_IDLE_MAX_CHARS:
        return True
    normalized = plain.replace("？", "").replace("?", "").replace("！", "").replace("!", "").strip()
    if plain in _FAMILIAR_IDLE_PHRASES or normalized in _FAMILIAR_IDLE_PHRASES:
        return True
    if compact.lower() in _FAMILIAR_IDLE_PHRASES:
        return True
    return False


def _is_session_thread_idle(event: dict, text: str) -> bool:
    """唤醒会话窗内的轻量可接续判定：只拦明显刷屏/插不进话的情形。

    与 familiar 的 12 字闲聊闸不同：短句有意义的接续应放行（否则“自然接续”会被
    字数闸误杀）。仍拦截：纯图/纯噪音、闲聊短语表、以及 @ 他人的两人对话。
    """
    if is_direct_group_call(event, text):
        return False
    raw = text or ""
    if re.search(r"\[CQ:at,", raw, re.I) and not event.get("mentions_self"):
        return True
    plain = _plain_group_text(raw)
    if not plain:
        return True
    normalized = plain.replace("？", "").replace("?", "").replace("！", "").replace("!", "").strip()
    if plain in _FAMILIAR_IDLE_PHRASES or normalized in _FAMILIAR_IDLE_PHRASES:
        return True
    compact = re.sub(r"[\s\W_]+", "", plain, flags=re.UNICODE).lower()
    if compact in _FAMILIAR_IDLE_PHRASES:
        return True
    return False


def classify_group_reply_skip(
    event: dict,
    text: str,
    *,
    user_id: int,
    group_policy: dict | None,
    autonomy,
    data_dir: str = "data",
    social=None,
    session_active: bool = False,
) -> str | None:
    """若不回复返回原因码，否则 None。

    原因：bot_ignore | bot_cooldown | bot_loop | other_bot | idle | mention_only | unfamiliar | other
    session_active：唤醒会话窗内，相关消息可自然接续（不必每条再 @/喊名），
    但仍由 idle 次闸门挡住超短闲聊/对人打招呼/其它 bot，避免刷屏。
    """
    # ── 多 Bot 社交：防止 Bot 互 @ 死循环（Critical）──
    # 对所有群消息记录入站（构建连续 Bot 链），再判断发送者是否为其它 Bot。
    gid = event.get("group_id")
    if gid is not None and event.get("post_type") == "message":
        try:
            from anima.cognitive import bot_social
            scfg = social
            if not scfg or getattr(scfg, "bot_social_enabled", True):
                _now = time.time()
                _is_bot = bot_social.is_bot_sender(event)
                bot_social.observe_inbound(
                    gid, int(user_id), _is_bot, bool(event.get("mentions_self")), _now,
                )
                if _is_bot:
                    _reason = bot_social.classify_bot_reply_skip(
                        event, text,
                        self_id=event.get("self_id"),
                        now=_now,
                        data_dir=data_dir,
                        cooldown_sec=int(getattr(scfg, "bot_cooldown_sec", 30) or 30),
                        max_depth=int(getattr(scfg, "bot_loop_max_depth", 2) or 2),
                    )
                    if _reason:
                        return _reason
        except Exception:
            logger.exception("多 Bot 社交 gate 异常，已放行（不阻塞正常回复）")

    mode = resolve_group_reply_mode(group_policy, autonomy)
    if is_addressed_to_other_bot(event, text, group_policy=group_policy, autonomy=autonomy):
        return "other_bot"
    # 对方示意结束即停：群友已明示「不聊/睡了/别回/晚安/下线」等收尾 → 不追不接，
    # 避免把聊天拖成一串连环互怼。除非被直接点名需要实际回应，仍放行。
    if is_conversation_end_signal(text) and not is_direct_group_call(event, text):
        return "end_signal"
    # 唤醒会话窗：窗内非刷屏即可自然接续（等效于放宽到可接话，但不刷屏）。
    if session_active:
        if _is_session_thread_idle(event, text):
            return "idle"
        return None
    if mode == "open":
        return None
    if is_direct_group_call(event, text):
        return None
    if mode != "familiar":
        return "mention_only"
    if is_familiar_idle_unaddressed(event, text):
        return "idle"
    try:
        from anima.cognitive.relation import get_relation
        manager = get_relation(data_dir)
        store = getattr(manager, "_relations", {}) or {}
        rel = store.get(str(int(user_id))) or store.get(int(user_id))
        if rel is None:
            return "unfamiliar"
        min_f = float(
            (group_policy or {}).get("familiar_min")
            if isinstance(group_policy, dict) and "familiar_min" in (group_policy or {})
            else getattr(autonomy, "familiar_reply_min", 0.35)
        )
        if float(getattr(rel, "familiarity", 0.0) or 0.0) >= min_f:
            return None
        return "unfamiliar"
    except Exception:
        logger.exception("熟人可插话亲密度读取失败，已回退仅直接呼叫")
        return "other"


def should_reply_in_group(
    event: dict,
    text: str,
    *,
    user_id: int,
    group_policy: dict | None,
    autonomy,
    data_dir: str = "data",
) -> bool:
    """群聊是否进入完整回复流水线（含熟人可插话策略 + Gate B + 不抢其它 bot）。"""
    return classify_group_reply_skip(
        event, text,
        user_id=user_id,
        group_policy=group_policy,
        autonomy=autonomy,
        data_dir=data_dir,
    ) is None
