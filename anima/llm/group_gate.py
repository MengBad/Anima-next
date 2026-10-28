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

#: 问句特征（含问号或疑问词）—— 短句命中这些 ⇒ 是在问，不是闲聊。
_QUESTION_MARKERS = (
    "？", "?", "什么", "啥", "哪", "怎么", "咋", "为什么", "为何", "多少",
    "吗", "呢", "是不是", "有没有", "能不能", "可不可以", "如何", "几",
)

#: 请求特征 —— 在让 bot 做事（发/说/来/给/查…）。
_REQUEST_MARKERS = (
    "发点", "发个", "来点", "来个", "给我", "帮我", "说说", "讲讲", "讲讲看",
    "看看", "查查", "找找", "推荐", "讲一下", "说一下", "详细", "展开",
    "再多", "然后呢", "继续", "接着", "还有呢",
)

#: 指向 bot 的人称（未点名但明显在对它说）。
#: 🔴 单靠人称不够：「视频太长了你自己看去」也含「你」，但那是**陈述**不是提问。
#: ⇒ 人称必须与「疑问/请求」共现才算（见 `_looks_like_question_or_request` 第 3 条）。
_ADDRESSED_MARKERS = ("你", "您", "本喵", "哈基米")

#: 指向 bot 的**提问句式**：人称 + 疑问词/请求动词的紧邻组合。
#: 比「含『你』」窄得多 —— 只有这些形态才视为「在问它」。
_ADDRESSED_PATTERNS = (
    "你看了", "你看过", "你知道", "你觉得", "你会", "你能", "你可以",
    "你说说", "你讲讲", "你推荐", "你查", "你发", "你给", "你帮",
    "你最近", "你刚才", "你上次", "你刷到", "你看到", "你听过",
    "你叫什么", "你是谁", "你在干嘛", "你在干什么",
)


def _looks_like_question_or_request(text: str) -> bool:
    """短句是否**在问它/求它**（而不是纯闲聊）。

    ## 为什么需要它（2026-10-26）

    `_FAMILIAR_IDLE_MAX_CHARS = 12` 一刀切把「在问它」和「纯闲聊」一起拦掉：

        9字 「发点人类的视频好吗」    → 被判 idle（实际是请求）
        7字 「你看了什么视频」        → 被判 idle（实际是提问）
       10字 「你最近刷到什么好玩的」  → 被判 idle（实际是提问）
       10字 「视频太长了你自己看去」  → 被判 idle（这条确实该拦 ✅）

    后果：**群友以为它不会回答，实际是它根本没收到**。

    ## 判据（三选一，保守）

    1. 含问号或疑问词（什么/哪/怎么/吗…）
    2. 含请求词（发点/来点/给我/说说/推荐…）
    3. 含**指向 bot 的提问句式**（`_ADDRESSED_PATTERNS`：你看了/你觉得/你推荐…）

    ⚠️ 第 3 条刻意用**具体句式**而不是「含『你』」：
    实测「视频太长了你自己看去」含「你」但那是陈述句，
    用「含『你』」会把它放行 ⇒ 熟人乱插话。

    纯函数、无 I/O。
    """
    s = str(text or "").strip()
    if not s:
        return False

    # 1. 问号 / 疑问词
    if any(m in s for m in _QUESTION_MARKERS):
        return True
    # 2. 请求词
    if any(m in s for m in _REQUEST_MARKERS):
        return True
    # 3. 指向 bot 的具体提问句式（不是"含你"这么粗）
    if any(p in s for p in _ADDRESSED_PATTERNS):
        return True
    return False


# 对方示意对话结束的信号：命中后 bot 不再连环追着说。
# 规则：硬缺口——若群友已明示「不聊/睡了/别再回复/下线/晚安/收工」等收尾，
# bot 除非被直接点名需要实际回应，否则不接不追，避免把聊天拖成一串互怼。
_END_SIGNAL_PATTERNS = (
    "不聊了", "别回了", "别再回", "别回复", "别理", "消停", "闭嘴", "收工",
    "下线", "去睡了", "睡了", "晚安", "睡了喵", "关了", "关机", "撤了", "走了",
    "先这样", "就到这", "到此为止", "不跟你聊", "不想说话", "不喜欢说了",
    "禁言", "别刷了", "别发了", "少说点", "慢走", "回聊", "明天再说", "改天聊",
    "先不回了", "不回了", "别回了喵", "转私聊", "别跟了", "别烦了", "不再说",
    "别再烦", "别来了", "先撤", "下回再说", "回头再说",
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


# ---------------------------------------------------------------------------
# 会话连续性信号：纠正/补充 与 撤销
# ---------------------------------------------------------------------------
# 每用户被动冷却的本意是治「戳一句又回一句」的刷屏，但它分不清闲话和对上一轮
# 的纠正/补充。唤醒窗内的纠正（"不对，我问的是历史题"）是对话仍在进行的信号，
# 不应被吞。词表刻意收窄，且只在唤醒窗活跃时豁免（双闸门），窗外纪律不变。
_CONTINUATION_PATTERNS = (
    "不对", "不是", "错了", "说错", "等等", "补充", "还有", "对了", "重说",
)


def is_conversation_continuation(text: str) -> bool:
    """是否为对上一轮的纠正/补充（配合唤醒窗豁免每用户被动回复冷却）。

    只认句首引导词（startswith）：纠正几乎总是先说「不对/等等」再展开；
    句中出现这些词的普通聊天不豁免（2026-09-06 复审收窄防刷面）。
    """
    plain = _plain_group_text(text or "")
    return bool(plain) and plain.startswith(_CONTINUATION_PATTERNS)


# 撤销：用户在 bot 生成/发送期间改主意，在途回复应让路（首段前弃答/停止分段）。
_CANCEL_SIGNAL_PATTERNS = (
    "算了", "不用了", "不用回", "不用答", "别回了", "当我没说", "撤回",
    "收回", "不问了", "取消刚才",
)
# 纠正：发送中发现理解错了，剩余分段停止，让位给纠正后的新回复。
_CORRECTION_SIGNAL_PATTERNS = (
    "不对", "不是这样", "我说的是", "你理解错", "理解错了", "问的不是",
    "重说", "说错了",
)


def is_cancel_signal(text: str) -> bool:
    """是否为撤销信号（生成/发送期间用户改主意）。"""
    plain = _plain_group_text(text or "")
    return bool(plain) and any(p in plain for p in _CANCEL_SIGNAL_PATTERNS)


def is_correction_signal(text: str) -> bool:
    """是否为纠正信号（对 bot 正在说的内容改口）。"""
    plain = _plain_group_text(text or "")
    return bool(plain) and any(p in plain for p in _CORRECTION_SIGNAL_PATTERNS)


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
    # I6（2026-09-19）：原判据 `re.search(r"\[CQ:at,", raw)` 是死分支——`event["text"]`
    # 由纯 text 段拼接（onebot11.py:361），CQ 已被剥离，该正则永不命中。改用协议层
    # `at_uids`（onebot11.py:364，已排除 bot 自身）。能走到这里 ⇒ `is_direct_group_call`
    # 已返回 False ⇒ 必非「@ 我」，故无需再判 mentions_self。
    if event.get("at_uids"):
        return True
    plain = _plain_group_text(raw)
    if not plain:
        # 纯图/纯表情：熟人模式也不自动长篇接话（避免误抢戏）；直接呼叫已在上面放行。
        return True
    compact = re.sub(r"[\s\W_]+", "", plain, flags=re.UNICODE)
    # 未点名时抬高门槛：短闲聊/半句不接，减少熟人乱插话
    #
    # 🔴 2026-10-26：**问句/请求句不受长度限制**。
    #
    # 生产实证：`_FAMILIAR_IDLE_MAX_CHARS = 12` 一刀切，把「在问它」的短句
    # 和「纯闲聊」一起拦掉了：
    #
    #     9字 「发点人类的视频好吗」      → idle（这是在请求它做事！）
    #     7字 「你看了什么视频」          → idle（这是在问它！）
    #    10字 「你最近刷到什么好玩的」    → idle（这是在问它！）
    #    10字 「视频太长了你自己看去」    → idle（这条确实该拦 ✅）
    #
    # 前三句被拦的直接后果：**群友以为它不会回答，实际是它根本没收到**。
    #
    # ⇒ 判据从「长度」改为「长度 + 是不是在问/求它」：
    #   命中问句/请求特征 ⇒ 交给后续亲密度判定（不再按 idle 拦）。
    #   纯闲聊短句（「视频太长了你自己看去」）仍按长度拦。
    if len(plain) <= _FAMILIAR_IDLE_MAX_CHARS or len(compact) <= _FAMILIAR_IDLE_MAX_CHARS:
        if not _looks_like_question_or_request(plain):
            return True
    normalized = plain.replace("？", "").replace("?", "").replace("！", "").replace("!", "").strip()
    if plain in _FAMILIAR_IDLE_PHRASES or normalized in _FAMILIAR_IDLE_PHRASES:
        return True
    if compact.lower() in _FAMILIAR_IDLE_PHRASES:
        return True
    # 🔴 2026-10-28：**纯标点/纯符号不接**（与 `_is_session_thread_idle` 同一修法）。
    #
    # 生产实证（18:54:03）：「？！」三个字符 ——
    #   · `plain`      = "？！" → 不在短语表
    #   · `normalized` = ""    → 空串不在表（**漏洞在此**）
    #   · `compact`    = ""    → 同上
    #   · 长度 2 ≤ 12，但 `_looks_like_question_or_request` 认「？」为问句
    #     ⇒ 走不到上面那个 return True ⇒ 最终放行
    #
    # 结果：bot 把两个标点读成「被说中的反应」，凭空接了一整句。
    #
    # ⇒ 去掉标点/空白/符号后什么都不剩 ⇒ 无内容 ⇒ 不接。
    # ⚠️ 放在**问句判定之后**：`「在吗？」` 这类是真实短问句，
    #    应该交给 `_looks_like_question_or_request` 放行，
    #    只有「全是标点、一个实词都没有」才拦。
    if not compact:
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
    # I6（2026-09-19）：原判据 `re.search(r"\[CQ:at,", raw)` 是死分支——`event["text"]`
    # 由纯 text 段拼接（onebot11.py:361），CQ 已被剥离，该正则永不命中。改用协议层
    # `at_uids`（onebot11.py:364，已排除 bot 自身）。能走到这里 ⇒ `is_direct_group_call`
    # 已返回 False ⇒ 必非「@ 我」，故无需再判 mentions_self。
    if event.get("at_uids"):
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
    # 🔴 2026-10-28：**纯标点/纯符号不接**（与 `is_familiar_idle_unaddressed` 同一修法）。
    #
    # 生产实证（18:54:03）：「？！」三个字符 ⇒ bot 回了一整句
    # 「干嘛突然这种语气看着我……难道被本喵说中了，心虚啦？」——
    # 它把两个标点读成「被说中的反应」，凭空脑补。
    #
    # 根因：三层短语表全落空，`compact` 去符号后是**空串**，不在表里。
    #
    # ⇒ 去掉标点/空白/符号后什么都不剩 ⇒ 无内容 ⇒ 不接。
    if not compact:
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
