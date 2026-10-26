"""自主思考心跳 —— 定期内省、产生主动发言冲动。

设计（基于 §6 设计规格）：
- Heartbeat Tick ~30s → emit "heartbeat.tick" → Cognitive Core 评估
- Intent: think|speak|act
- speak: 人类频率闸门（每群独立冷却 + 比例 + 预算）
- act: Tool call
- 断路熔断: 连续 N 次 LLM 失败 → 暂时退化为纯应答模式
"""

from __future__ import annotations
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Optional

from anima.core.gateway import OutboundMsg
from anima.core.persistence import PersistenceProvider

_BJ = timezone(timedelta(hours=8))  # 北京时间

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.core.autonomy")

# P1-C：fire-and-forget 后台任务统一收口（句柄 + 异常留痕 + shutdown cancel）。
_background_tasks: set[asyncio.Task] = set()


def _launch_bg(coro: 'asyncio.Future', name: str) -> asyncio.Task:
    """安全启动后台任务：存句柄、异常回调留痕(不被静默吞)、纳入 stop 时 cancel。"""
    task: asyncio.Task = asyncio.ensure_future(coro)
    task.set_name(f"anima.autonomy.{name}")

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.exception("后台任务异常 (%s)", name, exc_info=exc)

    task.add_done_callback(_done)
    _background_tasks.add(task)
    return task


async def _shutdown_background_tasks() -> None:
    """停服时取消全部仍挂起的后台任务，避免泄漏。"""
    pending = [t for t in _background_tasks if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _background_tasks.clear()


def is_user_avoided(data_dir: str, user_id: int, now: float | None = None) -> bool:
    """该用户当前是否处于伤痕回避期（供自主发言门控复用）。"""
    from anima.cognitive.scar import get_scar_system
    try:
        return get_scar_system(data_dir).is_avoiding(user_id, now)
    except Exception:
        return False


# 群上下文为方便模型分辨说话人会使用「[名字]: 内容」格式。主动发言的
# 回复本身不是转述，绝不能把这个上下文标记一起发出去。
_ACTIVE_REPLY_LABEL_RE = re.compile(
    r"^\s*(?:\[(?:你|哈基米|anima|Anima|bot)\]|(?:你|哈基米|anima))\s*[:：]\s*",
    re.IGNORECASE,
)
_ACTIVE_LOW_VALUE_RE = re.compile(
    r"(群里怎么突然这么安静|是不是都在偷偷|键盘被|梦游敲键盘|大半夜的怎么突然发|"
    r"别老是喊个名字就没下文|本喵的猫罐头还没着落|发乱码|被别的猫踩了|"
    r"睡大觉|去睡觉|快去睡|早点睡|赶紧睡|乖乖睡觉|该睡觉了|睡觉舒服|快去乖乖睡)"
)


# CQ 码清理（统一引用 anima.constants，含未闭合兜底）
from anima.constants import CQ_TRANSPORT_RE as _CQ_TRANSPORT_RE


def _clean_active_reply(reply: str) -> str:
    """移除主动发言中被模型照抄的上下文说话人标签及 CQ 码。"""
    text = _ACTIVE_REPLY_LABEL_RE.sub("", (reply or "").strip(), count=1).strip()
    text = re.sub(
        r"(?:^|\n)\s*\[(?:你|哈基米|anima|Anima)\]\s*[:：]\s*",
        "\n", text, flags=re.IGNORECASE,
    ).strip()
    # 剥离残留 CQ 码（LLM 可能照抄议程中的 CQ 片段）
    text = _CQ_TRANSPORT_RE.sub("", text).strip()
    return text


def _normalized_active_reply(reply: str) -> str:
    """用于主动发言去重的宽松规范化文本。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _clean_active_reply(reply)).lower()


def _is_low_value_active_reply(reply: str) -> bool:
    """拦截主动发言里常见的空转水话（刷存在感、骂乱码、问安静）。"""
    text = _clean_active_reply(reply)
    if not text:
        return True
    if _ACTIVE_LOW_VALUE_RE.search(text):
        return True
    # 过短且不含任何实质内容
    if len(text) < 6:
        return True
    return False


# 接地网关（2026-09-06 第三轮体验测试 B1）：proactive_grounding 此前是哑配置
# （config 声明「未接地一律丢弃」但全仓零实现），实测把「输入数据解析异常…
# 请提供具体的地理坐标」这类内部排障话术发进了群。现在落地为真检查：
# 主动发言命中排障/索要话术一律丢弃——群里没人能回应 bot 的内部错误。
_PROACTIVE_UNGROUNDED_RE = re.compile(
    r"(解析异常|解析失败|参数不足|缺少[^。，]{0,8}(?:参数|信息)|请提供|请给出|请具体说明|"
    r"无法检索|检索失败|访问失败|请求失败|接口[^。，]{0,4}不可用|工具执行错误|工具暂时不可用|"
    r"需要[^。，]{0,6}(?:提供|补充)[^。，]{0,8}(?:信息|坐标|城市|名称))"
)


def _is_ungrounded_proactive(reply: str) -> bool:
    return bool(_PROACTIVE_UNGROUNDED_RE.search(_clean_active_reply(reply or "")))


_OPEN_QUESTION_RE = re.compile(
    r"(?:[?？]|为什么|为啥|怎么(?:办|弄|说)?|咋(?:办|整)?|谁知道|有没有|能不能|可不可以|吗[啊呀呢]?$)"
)
_CASUAL_HOOK_RE = re.compile(r"(?:哈哈|笑死|绷不住|离谱|绝了|逆天|救命|好家伙|乐了)")
_NOISE_ONLY_RE = re.compile(r"^[\s?？!！.。,，~～…·\d]+$")


def score_active_context(history: list[dict]) -> float:
    """评估群聊是否存在值得主动接住的近期语境，返回 0~1。

    只使用短时群缓冲，不读取跨群资料。开放问题权重最高；单纯热闹但没有
    接话点只给低分；最后一句是 bot 自己时强降权，避免自问自答。
    """
    usable = [m for m in history if str(m.get("content") or "").strip()]
    if not usable:
        return 0.0
    recent = usable[-12:]
    human = [m for m in recent if m.get("role") == "user"]
    if not human:
        return 0.0

    score = min(0.18, len(human) * 0.03)
    last = recent[-1]
    last_text = str(last.get("content") or "").strip()
    if last.get("role") != "user":
        score -= 0.40
    else:
        if _NOISE_ONLY_RE.match(last_text) or len(last_text) < 2:
            return 0.0
        if _OPEN_QUESTION_RE.search(last_text):
            score += 0.58
        if _CASUAL_HOOK_RE.search(last_text):
            score += 0.22
        if 6 <= len(last_text) <= 80:
            score += 0.08
        # 纯表情/极短灌水不值得插话
        if len(last_text) <= 3 and not _OPEN_QUESTION_RE.search(last_text):
            score -= 0.25

    # 多人连续聊天说明这是活的语境，但不足以单独触发主动发言。
    distinct_speakers = set()
    for message in human[-8:]:
        speaker = str(message.get("name") or message.get("sender_name")
                      or message.get("user_id") or "")
        content = str(message.get("content") or "")
        if not speaker and content.startswith("[") and "]:" in content:
            speaker = content[1:content.index("]: ") if "]: " in content else content.index("]:")]
        if speaker:
            distinct_speakers.add(speaker)
    if len(distinct_speakers) >= 2:
        score += 0.06
    return max(0.0, min(1.0, score))

# 昼夜节律活跃度（0-23 时）
HOURLY_ACTIVITY = {
    0: 0.15, 1: 0.10, 2: 0.05, 3: 0.05, 4: 0.10, 5: 0.15,
    6: 0.30, 7: 0.45, 8: 0.60, 9: 0.75, 10: 0.85, 11: 0.90,
    12: 0.80, 13: 0.70, 14: 0.85, 15: 0.90, 16: 0.85, 17: 0.75,
    18: 0.70, 19: 0.65, 20: 0.55, 21: 0.45, 22: 0.30, 23: 0.20,
}


# ---------------------------------------------------------------------------
# 自身状态刷新（P3）：每小时核对群头衔/名片，变更写 self_notes
# ---------------------------------------------------------------------------

def _build_active_speech_system(
    time_ctx: str,
    desire_ctx: str,
    speaking_rules: str,
    self_notes: str = "",
    mood_note: str = "",
    world_context: str = "",
) -> str:
    """人格单源（队列②）：主动发言与被动回复共用 persona_core.yaml 正本。

    时间/欲望片段走与被动回复相同的注入位（anima/cognitive/persona_core）；
    出口专属的发言守则经 extra_rules 追加。身份/边界护栏以 yaml 正本
    「身份与边界」为准，此处不再重复（2026-09-12 去重：三重最高优先级
    指令堆叠会让主动发言更剧本化）。

    2026-09-22：补喂 **内容类** 人格片段 —— `self_notes`（自我笔记，人格
    漂移主产物）与 `mood_note`（当前情绪）。此前主动发言只传 time/desire，
    装配器会把 `{self_notes}` / `{world_context}` / `{scar_context}` 替换成
    空串，于是**人格最外显的场景**（没人问、自己开口）反而看不到自己的笔记
    与情绪。内容类片段与 09-12 去重的**护栏类**片段（身份锁定/角色守卫）
    性质不同：前者是"素材"，后者是"最高优先级指令"，不构成"三重堆叠"。

    2026-09-22（续）：再补 `world_context`。5 个占位符里它是**唯一群级语义
    有效**的一个（`world_model.prompt_fragment()` 与 user 无关）。`relation` /
    `scar` 需 `user_id`，模板里的「这个人」在**群级**主动发言里指向不明，
    刻意**不注入**。本路径是**独立 LLM 调用、不进 ContextComposer 预算池**
    ⇒ 该补充非零和，不挤占其它片段的预算。
    """
    from anima.cognitive.persona_core import assemble_persona_system

    extra_rules = ""
    if mood_note and mood_note.strip():
        extra_rules += mood_note.strip() + "\n\n"
    extra_rules += speaking_rules

    return assemble_persona_system(
        extra_rules=extra_rules,
        time_context=f"当前时间：{time_ctx}",
        desire_context=desire_ctx,
        self_notes=self_notes,
        world_context=world_context,
    )


def _active_reply_drop_reason(reply: str) -> str:
    """主动发言候选回复的丢弃原因码；空串 = 可以说。

    🔴 r28 N1：这三个语义完全不同的出口（模型返回空 / 模型自己说 ``[skip]`` /
    正文太短）此前共用一个**裸 return False**，journal 里什么都没有。生产
    2026-10-06 主动发言 6 次触发只发出 3 次，另外 3 次（10:43 / 18:44 / 18:49）
    只能看到「主动发言人格片段」这一行，事后无从归因到底是配额烧了还是模型
    不想说 —— 而这两者的处置完全相反（降配额 vs 调 prompt）。
    """
    if not reply or not str(reply).strip():
        return "empty"
    if "[skip]" in str(reply).lower():
        return "skip_tag"
    if len(str(reply).strip()) < 4:
        return "too_short"
    return ""


def _active_speech_persona_fragments(core) -> tuple[str, str, str]:
    """主动发言用的内容类人格片段：(自我笔记, 当前情绪, 世界观)。

    三个子系统**各自 fail-soft** —— 任一取值失败都返回空串，绝不拖垮主动
    发言（它跑在 30s 心跳里，不能因某个认知子系统异常而中断）。

    **刻意不含** `relation` / `scar`：二者签名是 `prompt_fragment(user_id)`，
    属 user 维度。主动发言是**群级**出口（本函数拿不到、也不该猜一个 user_id），
    模板里的「这个人」会指向不明 ⇒ 注入只会制造歧义。该决策由
    `tests/test_persona_module_integration.py::test_active_speech_never_injects_user_scoped_fragments`
    钉死（负向守卫 + 签名守卫）。
    """
    self_notes = ""
    try:
        from anima.cognitive.learning import get_learning
        self_notes = get_learning().prompt_fragment() or ""
    except Exception:
        logger.debug("主动发言：自我笔记取值失败（已隔离）", exc_info=True)

    _pcfg = getattr(getattr(core, "config", None), "persistence", None)
    _data_dir = getattr(_pcfg, "data_dir", "data") if _pcfg else "data"

    mood_note = ""
    try:
        from anima.cognitive.mood import get_mood_system
        mood_note = get_mood_system(_data_dir).prompt_fragment() or ""
    except Exception:
        logger.debug("主动发言：情绪取值失败（已隔离）", exc_info=True)

    world_ctx = ""
    try:
        from anima.cognitive.world_model import get_world_model
        world_ctx = get_world_model(_data_dir).prompt_fragment() or ""
    except Exception:
        logger.debug("主动发言：世界观取值失败（已隔离）", exc_info=True)

    # 运行时可观测：主动发言的 system 内容不落盘 ⇒ 不打日志则"补上没有"不可验证。
    # 只在真正构建主动发言 prompt 时打（通过门控后，低频），纯只读不产生副作用。
    logger.info(
        "主动发言人格片段：self_notes=%d 字，mood=%s，world=%d 字",
        len(self_notes), "有" if mood_note else "无", len(world_ctx),
    )
    return self_notes, mood_note, world_ctx


def _desire_event_match(content: str, recent_text: str) -> bool:
    """欲望内容与最近群消息的宽松匹配：整句/词包含，或任一 2 字滑窗命中。"""
    c = (content or "").strip().lower()
    r = (recent_text or "").strip().lower()
    if not c or not r:
        return False
    if c in r:
        return True
    for w in c.split():
        if len(w) >= 2 and w in r:
            return True
    return any(c[i:i + 2] in r for i in range(len(c) - 1))


def _select_desire_topic(
    desire_sys, group_id, history, now, *,
    min_intensity: float = 0.55,
    freshness_window_sec: float = 1800.0,
    event_match_bonus: float = 0.12,
    repeat_cooldown_sec: float = 3600.0,
    expressed=None,
):
    """A1 欲望深化（队列③）：最强欲望优先选题。

    候选 = ``get_expressible(group_id, min_intensity)``（作用域允许出站的欲望）；
    评分 = intensity
         + 新鲜度加成（created_at 距今 < freshness_window_sec，线性衰减，封顶 +0.15）
         + 新鲜事件匹配（欲望内容命中最近群消息，+event_match_bonus）
    ``repeat_cooldown_sec`` 内已表达过的相似欲望不再入选（防念叨同一话题）。
    无可选返回 None，调用方退回普通接话选题。
    """
    try:
        candidates = desire_sys.get_expressible(group_id, min_intensity)
    except Exception:
        return None
    if not candidates:
        return None

    recent_text = " ".join(
        str(m.get("content") or "") for m in (history or [])[-8:]
    )
    expressed = list(expressed or [])
    best = None
    best_score = -1.0
    for d in candidates:
        content_l = str(d.content or "").strip().lower()
        # 表达冷却：近期已说过的高度相似话题跳过
        cooled = any(
            _desire_text_similar(content_l, str(prev_c or "").lower())
            for prev_c, ts in expressed
            if now - float(ts) < repeat_cooldown_sec
        )
        if cooled:
            continue
        age = max(0.0, now - float(getattr(d, "created_at", now)))
        score = float(d.intensity)
        if age < freshness_window_sec:
            score += 0.15 * (1.0 - age / freshness_window_sec)
        if _desire_event_match(content_l, recent_text):
            score += event_match_bonus
        if score > best_score:
            best, best_score = d, score
    return best


def _desire_text_similar(a: str, b: str) -> bool:
    """表达冷却的相似判定：短串被长串包含即视为同一话题。"""
    if not a or not b:
        return False
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    return s in l


def compute_self_status_notes(
    prev_title: str, new_title: str, prev_card: str, new_card: str, group_id: int
) -> "list[str]":
    """比较自身在群内的头衔/名片前后状态，返回需写入 self_notes 的文本列表。

    仅在发生实际变化时返回条目；首跑（prev 为空）由调用方决定是否跳过，避免刷屏。
    """
    notes: list[str] = []
    if prev_title != new_title:
        if new_title:
            notes.append(f"群 {group_id} 当前头衔：【{new_title}】")
        else:
            notes.append(f"群 {group_id} 头衔被清空了")
    if prev_card != new_card:
        if new_card:
            notes.append(f"群 {group_id} 当前群名片：【{new_card}】")
        else:
            notes.append(f"群 {group_id} 群名片被清空了")
    return notes


def _decide_morning_dream_share(
    hour: int, last_morning_date: str, today: str, rng: random.Random | None = None,
) -> "tuple[bool, bool]":
    """A3 纯函数：仅在 08:00、当天未尝试时以 10% 概率决定分享梦境。

    返回 (should_share, attempted)：
    - should_share=True 时调用方应在分享成功后标记 _last_morning_dream_date=today；
    - attempted=True 表示「08:00 且今天已抽过」——调用方应标记尝试，避免每 tick 重复抽。
    """
    if hour != 8:
        return (False, False)
    if last_morning_date == today:
        return (False, False)
    if (rng or random).random() >= 0.1:
        return (False, True)   # 抽不中，今天已尝试
    return (True, False)       # 抽中分享（成功后再标记）


def _constitution_drive(mood_arousal, mood_valence, relation_depth=0.2, threat=0.0,
                        memory=None):
    """T1.1 轻量宪法桥：Memory → Constitution M1 链(Identity→Need→Affect→Drive) → Drive dict。

    2026-09-18（B1，审计整改）：链首 Memory 改为**优先读取真实水合记忆**——调用方从
    ``MemoryAuthority.snapshot_memory()`` 传入 ``memory``。仅当调用方显式传 None 时才退回
    「现场合成替身 Memory」，且返回体用 ``memory_source``（``live`` / ``live-empty`` /
    ``synthetic``）+ ``record_counts`` + ``identity_traits`` 如实标注来源，杜绝"链首是替身
    却对外标称已接线"的静默（这是 2026-09-18 审计发现的第一条边替身问题）。

    ⚠️ 已知边界（台账 D-13）：当前水合只产 ``episode``，而 ``derive_identity`` 只把 ``semantic``
    的数值字段聚成 traits ⇒ 真实记忆下 ``identity_traits=0``、``need.urgency=0``，Drive 激活退化为
    只由 Perception(novelty) 决定。故在 Reflection→Memory 闭环补齐前，接真记忆 ≈ 让门**更保守**
    （fail-closed），不会凭空产生驱动。

    2026-09-18 后段更新：D-13 口径 (a) **已达成** —— 反思沉淀 producer 已接上
    （``constitution/reflection_loop.py`` + ``Heartbeat._constitution_sediment_carrying``，
    开关 ``autonomy.constitution_reflection_loop`` 默认关）。但**第二层（数值 trait 通道）仍未闭**：
    ``reflect()`` 只产文本 lesson ⇒ 上面 ``identity_traits=0`` 的结论**不变**，
    ``drive_gate`` 仍不可翻转。详见台账 D-13 末节。

    Constitution 只产出趋近/回避取向(Drive)，不改任何出站语言，只可能压制主动表达。
    任何一步失败返回 None(fail-closed)。
    """
    try:
        import anima.constitution.state as _C
        from anima.constitution.identity import derive_identity
        from anima.constitution.need import derive_need
        from anima.constitution.affect import derive_affect
        from anima.constitution.drive import derive_drive
    except Exception:
        logger.debug("constitution 引擎不可用，跳过 Drive 桥", exc_info=True)
        return None
    try:
        if memory is None:
            # 显式替身回退：无 MemoryAuthority 时保持旧行为，但标注来源（不再伪装为真实记忆）
            _mem = _C.Memory(
                semantic=[_C.MemoryRecord(
                    kind="semantic",
                    content={"valence": round(float(mood_valence), 3),
                             "arousal": round(float(mood_arousal), 3)},
                    salience=min(1.0, abs(float(mood_valence))),
                )],
                relations=[_C.MemoryRecord(
                    kind="relation",
                    content={"depth": round(float(relation_depth), 3)},
                    salience=min(1.0, float(relation_depth)),
                )],
            )
            _src = "synthetic"
        else:
            # 真实记忆：只读快照，绝不改源（f(Memory) 契约：purity over 便利）
            _mem = memory.snapshot() if hasattr(memory, "snapshot") else memory
            _src = "live" if (_mem.episodes or _mem.semantic or _mem.relations) else "live-empty"
        _percept = _C.Perception(signals={
            "novelty": max(0.0, float(mood_arousal)),
            "threat": max(0.0, float(threat)),
        })
        _identity = derive_identity(_mem)
        _drive = derive_drive(derive_affect(derive_need(_identity), _percept))
        _ori = _drive.orientation
        return {
            "activated": bool(_drive.activated),
            "approach": float(_ori.get(_C.OrientationAxis.APPROACH, 0.0)),
            "avoid": float(_ori.get(_C.OrientationAxis.AVOID, 0.0)),
            "intensity": float(_ori.get(_C.OrientationAxis.INTENSITY, 0.0)),
            "memory_source": _src,
            "record_counts": {
                "episodes": len(_mem.episodes),
                "semantic": len(_mem.semantic),
                "relations": len(_mem.relations),
            },
            "identity_traits": len(_identity.traits),
        }
    except Exception:
        logger.debug("Drive 推导失败，fail-closed 视为无驱动的主动", exc_info=True)
        return None


def _drive_gate_ok(drive) -> bool:
    """T1.1 主动表达 Drive 门：须呈现且已激活、且趋近>回避，否则 fail-closed 压制。"""
    if not drive or not isinstance(drive, dict):
        return False
    if not drive.get("activated"):
        return False
    return float(drive.get("approach", 0.0)) > float(drive.get("avoid", 0.0))


#: 自治自检议题标题 → `_FAULT_RULES` 的 signal key。
#: 🔴 **只允许映射到规则表里真实存在的键** —— `healed_fault_tickets` 会做
#:    `if any(sig not in thresholds): continue`，映射到表外的键等于永远不自愈
#:    （这正是 2026-10-21 那批 34 天僵尸单的成因之一）。
#: 不在本表内的议题（如「回复质量退化」——它的 tool_failures 指标不属 `_FAULT_RULES`）
#: 一律**不建单**，只在日志留痕；WebUI 侧 `observation/engineering_loop` 仍会消费。
_ISSUE_SIGNAL_MAP: tuple[tuple[str, str], ...] = (
    ("应回却空", "empty_should_reply"),
    ("工具空转", "tool_spins"),
    ("ReAct 频繁触顶", "react_max_rounds"),
    ("QQ 出站失败", "outbound_failures"),
    ("身份泄漏", "identity_leaks"),
    ("B 站主页链接处理失败", "space_link_fail"),
)


def _issue_signal_key(title: str) -> str:
    """把 DevIssue 标题映射为 `_FAULT_RULES` 的 signal key；无法映射返回 ""。

    fail-closed：映射不到就返回空 ⇒ 调用方据此**不建单**，避免再造僵尸。
    """
    t = str(title or "")
    for needle, key in _ISSUE_SIGNAL_MAP:
        if needle in t:
            return key
    return ""


def _issue_topic_key(text: str) -> str:
    """议题主题键：剥掉标题里的具体计数，使「同一病灶」判为同一主题。

    背景：`回复质量退化：工具失败 3/18` 与 `4/23` 是同一件事，但旧去重
    用全等比较（`request == issue.title`）⇒ 计数一变就判成新单 ⇒
    09-17 六小时内连开 3 张（生产实证，躺 34 天）。
    """
    import re as _re
    t = str(text or "")
    # 去掉「数字/数字」与孤立数字、各类空白与标点尾巴
    t = _re.sub(r"\d+\s*/\s*\d+", "#", t)
    t = _re.sub(r"\d+", "#", t)
    t = _re.sub(r"[\s，。：:、（）()【】\[\]…\-—]+", "", t)
    return t[:40]


class Heartbeat:
    """自主心跳引擎。"""

    def __init__(self, core: "AnimaCore",
                 tick_sec: float = 30.0,
                 cooldown_sec: int = 90,
                 daily_max: int = 20,
                 ratio_max: float = 0.30,
                 token_budget: int = 0,
                 active_reply_prob: float = 0.04,
                 active_silence_min_sec: int = 600,
                 active_global_cooldown_sec: int = 1800,
                 active_min_group_messages: int = 12,
                 active_daily_max_per_group: int = 3,
                 active_context_score_threshold: float = 0.55,
                 active_feedback_window_sec: int = 900,
                 active_feedback_min_samples: int = 4,
                 active_feedback_min_acceptance: float = 0.20,
                 desire_speak_min_intensity: float = 0.55,
                 desire_freshness_window_sec: float = 1800.0,
                 desire_event_match_bonus: float = 0.12,
                 desire_repeat_cooldown_sec: float = 3600.0,
                 data_dir: str = "data"):
        self.core = core
        self.tick_sec = tick_sec
        # R16: periodic anomaly self-check throttle + dedupe
        self._last_anomaly_check = 0.0
        self._last_anomaly_signature = None
        self._anomaly_check_interval = 300.0
        self.cooldown_sec = cooldown_sec
        self.daily_max = daily_max
        self.ratio_max = ratio_max
        self.token_budget = token_budget
        self._tokens_today = 0

        # 主动发言策略（配合沉默门控 + 全局冷却，避免刷屏）
        self.active_reply_prob = active_reply_prob
        self.active_silence_min_sec = active_silence_min_sec
        self.active_global_cooldown_sec = active_global_cooldown_sec
        self.active_min_group_messages = active_min_group_messages
        self.active_daily_max_per_group = active_daily_max_per_group
        self.active_context_score_threshold = active_context_score_threshold
        self.active_feedback_window_sec = max(60, int(active_feedback_window_sec))
        self.active_feedback_min_samples = max(1, int(active_feedback_min_samples))
        self.active_feedback_min_acceptance = max(0.0, min(1.0, float(active_feedback_min_acceptance)))
        # A1 欲望深化（队列③）：欲望驱动选题参数
        self.desire_speak_min_intensity = max(0.0, min(1.0, float(desire_speak_min_intensity)))
        self.desire_freshness_window_sec = max(0.0, float(desire_freshness_window_sec))
        self.desire_event_match_bonus = max(0.0, float(desire_event_match_bonus))
        self.desire_repeat_cooldown_sec = max(0.0, float(desire_repeat_cooldown_sec))

        self._task: Optional[asyncio.Task] = None
        self._running = False

        # A1（队列③）：欲望表达记录（选题冷却用），跨重启持久化
        self._recent_expressed_desires: dict[int, list[tuple[str, float]]] = {}

        # 每群独立追踪
        self._group_last_speak: dict[int, float] = {}
        self._group_speak_count: dict[int, int] = {}
        self._group_total_msgs: dict[int, int] = {}
        self._group_last_msg: dict[int, float] = {}   # 最近一次人类发言时间
        self._last_self_status_refresh: float = 0.0
        self._self_status_cache: dict[int, dict] = {}   # group_id -> {title, card}
        self._last_active_speak: float = 0.0           # 最近一次主动发言时间
        # 只在当前进程保留最近主动发言。用于避免模型围绕同一个梗短时间复读，
        # 不写入用户记忆，也不影响正常的被动回复。
        self._recent_active_replies: dict[int, list[tuple[float, str]]] = {}
        self._active_feedback_pending: dict[int, dict[str, object]] = {}
        self._active_feedback_stats: dict[int, dict[str, int]] = {}
        self._day_reset = time.time()

        # 熔断
        self._consecutive_failures = 0
        self._max_failures = 5
        self._degraded = False
        # M3 防 TOCTOU：串行化主动发言的 token 预算 检查→花费，消除并发越界
        self._speech_lock = asyncio.Lock()

        # 持久化
        self._persist = PersistenceProvider(data_dir)
        self._path = "heartbeat.json"
        self._data_dir = data_dir
        self.restore()

        # 反刍引擎
        from anima.cognitive.rumination import RuminationEngine
        self._rumination = RuminationEngine(self.core, data_dir=data_dir)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def restore(self) -> None:
        """从磁盘恢复每日计数与冷却状态。"""
        data = self._persist.read_json(self._path, {})
        self._group_last_speak = {
            int(k): v for k, v in data.get("group_last_speak", {}).items()
        }
        self._group_speak_count = {
            int(k): v for k, v in data.get("group_speak_count", {}).items()
        }
        self._group_total_msgs = {
            int(k): v for k, v in data.get("group_total_msgs", {}).items()
        }
        self._group_last_msg = {
            int(k): v for k, v in data.get("group_last_msg", {}).items()
        }
        self._last_active_speak = data.get("last_active_speak", 0.0)
        self._last_morning_dream_date = data.get("last_morning_dream_date", "")
        self._last_secret_gen_date = data.get("last_secret_gen_date", "")
        self._last_consolidation_date = data.get("last_consolidation_date", "")
        # 2026-10-14：bot 发现扫描日期标记（防重启后当天重复调度）
        self._last_bot_discovery_date = data.get("last_bot_discovery_date", "")
        raw_expressed = data.get("recent_expressed_desires", {})
        self._recent_expressed_desires = {}
        for k, v in raw_expressed.items():
            if not isinstance(v, list):
                continue
            try:
                self._recent_expressed_desires[int(k)] = [
                    (str(c), float(t)) for c, t in v
                ]
            except (TypeError, ValueError):
                continue
        self._active_feedback_pending = {
            int(k): dict(v) for k, v in (data.get("active_feedback_pending", {}) or {}).items()
            if isinstance(v, dict)
        }
        self._active_feedback_stats = {
            int(k): {
                "engaged": int((v or {}).get("engaged", 0) or 0),
                "ignored": int((v or {}).get("ignored", 0) or 0),
            }
            for k, v in (data.get("active_feedback_stats", {}) or {}).items()
            if isinstance(v, dict)
        }
        self._day_reset = data.get("day_reset", time.time())
        self._tokens_today = data.get("tokens_today", 0)
        # 恢复近期自主发言记录（跨重启去重）
        raw_replies = data.get("recent_active_replies", {})
        now = time.time()
        self._recent_active_replies = {
            int(k): [tuple(item) for item in v if now - item[0] < 14400]
            for k, v in raw_replies.items()
            if isinstance(v, list)
        }

    def persist(self) -> None:
        """保存每日计数与冷却状态。"""
        data = {
            "group_last_speak": {str(k): v for k, v in self._group_last_speak.items()},
            "group_speak_count": {str(k): v for k, v in self._group_speak_count.items()},
            "group_total_msgs": {str(k): v for k, v in self._group_total_msgs.items()},
            "group_last_msg": {str(k): v for k, v in self._group_last_msg.items()},
            "last_active_speak": self._last_active_speak,
            "active_feedback_pending": {str(k): v for k, v in self._active_feedback_pending.items()},
            "active_feedback_stats": {str(k): v for k, v in self._active_feedback_stats.items()},
            "day_reset": self._day_reset,
            "tokens_today": self._tokens_today,
            "recent_active_replies": {
                str(k): v[-8:] for k, v in getattr(self, "_recent_active_replies", {}).items()
            },
            # 深审 P1-3：日期标记持久化，防重启后当天重复生成/分享
            "last_morning_dream_date": getattr(self, "_last_morning_dream_date", ""),
            "last_secret_gen_date": getattr(self, "_last_secret_gen_date", ""),
            # 队列②前微修：沉淀日期标记同样持久化，防重启后当天重复调度沉淀
            "last_consolidation_date": getattr(self, "_last_consolidation_date", ""),
            "last_bot_discovery_date": getattr(self, "_last_bot_discovery_date", ""),
            # A1（队列③）：欲望表达冷却记录
            "recent_expressed_desires": {
                str(k): [(str(c), float(t)) for c, t in v[-20:]]
                for k, v in getattr(self, "_recent_expressed_desires", {}).items()
            },
        }
        self._persist.write_json(self._path, data)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        try:
            self.core.health_history.record(force=True)
        except Exception:
            logger.exception("心跳健康初始采样失败")
        logger.info("自主心跳已启动: tick=%.1fs cooldown=%ds daily=%d ratio=%.0f%%",
                     self.tick_sec, self.cooldown_sec, self.daily_max, self.ratio_max * 100)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
        await _shutdown_background_tasks()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.tick_sec)
            try:
                await self._tick()
            except Exception:
                logger.exception("心跳 tick 异常")

    async def _refresh_self_status(self) -> None:
        """每小时刷新 bot 在各群的专属头衔/群名片，变更则写入 self_notes（P3）。"""
        channel = getattr(getattr(self.core, "gateway", None), "channel", None)
        if channel is None or not hasattr(channel, "get_group_member_info"):
            return
        self_id = getattr(channel, "self_id", None)
        if not self_id:
            return
        groups = [int(g) for g in getattr(self, "_group_last_msg", {}).keys()]
        for gid in groups:
            try:
                info = await channel.get_group_member_info(gid, int(self_id))
            except Exception:
                logger.debug("刷新自身状态失败 group=%s", gid, exc_info=True)
                continue
            if not isinstance(info, dict):
                continue
            new_title = (info.get("title") or "").strip()
            new_card = (info.get("card") or "").strip()
            prev = self._self_status_cache.get(gid)
            if prev is None:
                # 首跑只种基线，不刷屏
                self._self_status_cache[gid] = {"title": new_title, "card": new_card}
                continue
            notes = compute_self_status_notes(
                prev.get("title", ""), new_title, prev.get("card", ""), new_card, gid
            )
            if notes:
                try:
                    from anima.cognitive.learning import get_learning
                    data_dir = str(self.core.config.persistence.data_dir)
                    for n in notes:
                        get_learning(data_dir).add_note(n, delta=0.05, category="self_correction")
                    logger.info("[自身状态] group=%s 更新 %d 条", gid, len(notes))
                except Exception:
                    logger.exception("自身状态写入 self_notes 失败")
            self._self_status_cache[gid] = {"title": new_title, "card": new_card}

    def _anomaly_selfcheck(self) -> None:
        # R16 Production Reliability: derive 24h anomaly report and record
        # severe findings into audit. Read-only, notification-only.
        now = time.time()
        if now - self._last_anomaly_check < self._anomaly_check_interval:
            return
        self._last_anomaly_check = now
        hh = getattr(self.core, "health_history", None)
        if hh is None or not hasattr(hh, "anomaly_report"):
            return
        try:
            report = hh.anomaly_report(hours=24.0)
        except Exception:
            logger.exception("anomaly_report failed, isolated")
            return
        severe = [a for a in report.get("anomalies", [])
                  if a.get("type") in ("process_restart", "channel_disconnect",
                                       "watchdog_lag", "llm_circuit_open")]
        if not severe:
            return
        items = sorted((a["type"], a.get("count", 0)) for a in severe)
        sig = repr(items)
        if sig == self._last_anomaly_signature:
            return
        self._last_anomaly_signature = sig
        audit = getattr(self.core, "audit", None)
        if audit is not None:
            audit.emit("runtime_anomaly_detected",
                       anomalies=items,
                       summary=report.get("summary"),
                       samples=report.get("samples"),
                       latest_running=bool(report.get("latest", {}).get("running")),
                       latest_connected=bool(report.get("latest", {}).get("channel_connected")))

    def _config_drift_check(self) -> None:
        # R16 闭环4：周期比对敏感配置/权限基线。仅只读告警进审计，绝不自动回滚。
        monitor = getattr(self.core, "config_drift", None)
        if monitor is None:
            return
        audit = getattr(self.core, "audit", None)
        try:
            monitor.maybe_emit(self.core.config, audit)
        except Exception:
            logger.exception("config drift check failed, isolated")

    # P2-1: health_proof 主动告警冷却（秒）——避免每个 tick 重复生成同一条 owner 待办。
    _HEALTH_ALERT_COOLDOWN_SEC = 6 * 3600.0
    _health_alert_last_run: float = 0.0

    def _health_alerts_tick(self) -> None:
        # 冷却：距上次运行不足冷却期则跳过（防刷屏 owner_tasks）。
        now = time.time()
        if now - self._health_alert_last_run < self._HEALTH_ALERT_COOLDOWN_SEC:
            return
        self._health_alert_last_run = now
        cs = getattr(self.core, "control_surface", None)
        if cs is None:
            try:
                from anima.os.control_surface import HumanAIControlSurface
                cs = HumanAIControlSurface(self.core)
            except Exception:
                logger.exception("control_surface 初始化失败，跳过健康告警")
                return
        owner_id = 0
        try:
            _owners = getattr(getattr(getattr(self.core, "config", None), "permissions", None), "owners", None) or []
            if _owners:
                owner_id = int(_owners[0])
        except (TypeError, ValueError, IndexError):
            owner_id = 0
        try:
            made = cs.health_alerts_to_owner(window_days=7, owner_id=owner_id)
        except Exception:
            logger.exception("health_alerts_to_owner 失败，已隔离")
            return
        if made:
            logger.info("[health_alerts] 已生成 %d 条 owner 健康待办: %s",
                        len(made), [a.get("type") for a in made])

    def _maybe_create_goals_from_drive(self, auth) -> None:
        """由内在驱动生成目标（P2 最后一环，2026-10-23）。

        链路（全部用**真实数据**，不编造）：:

            MemoryAuthority（已由 emotion_bridge 喂入真实情感）
              → derive_identity → derive_need → derive_affect → derive_drive
              → build_goals_from_drive(need.vectors)   ← 文案逐字取自向量
              → GoalStore.create

        Drive **未激活** ⇒ 不产生目标（RFC-0100 §1.7 的激活条件：
        Need 只有被 Affect 充上电才成为 Drive，没电就不该凭空行动）。

        fail-closed：任何异常只记日志，不影响心跳。
        """
        try:
            from anima.constitution.affect import derive_affect
            from anima.constitution.drive import derive_drive
            from anima.constitution.drive_to_goal import create_goals
            from anima.constitution.identity import derive_identity
            from anima.constitution.need import derive_need
            from anima.constitution.state import Perception

            # 情绪作为 Perception 输入（真实 mood，不是编的）
            arousal, valence = 0.2, 0.1
            try:
                from anima.cognitive.mood import get_mood_system
                _dd = getattr(getattr(self.core.config, "persistence", None),
                              "data_dir", "data")
                _m = get_mood_system(_dd).get_state()
                arousal = float(getattr(_m, "arousal", arousal))
                valence = float(getattr(_m, "valence", valence))
            except Exception:
                logger.debug("[drive→goal] mood 读取失败，用默认", exc_info=True)

            memory = auth.snapshot_memory()
            ident = derive_identity(memory)
            need = derive_need(ident)
            perception = Perception(signals={
                "novelty": max(0.0, min(1.0, float(arousal))),
                "threat": max(0.0, -float(valence)),
            })
            aff = derive_affect(need, perception)
            drive = derive_drive(aff)
            drive_dict = {
                "activated": bool(drive.activated),
                "orientation": {k.value: float(v)
                                for k, v in drive.orientation.items()},
            }
            if not drive.activated:
                logger.debug("[drive→goal] Drive 未激活（urgency=%.3f），不产生目标",
                             aff.urgency)
                return
            vectors = {k.value: list(v) for k, v in need.vectors.items()}

            owners = list(getattr(getattr(self.core.config, "permissions", None),
                                  "owners", []) or [])
            if not owners:
                return
            made = create_goals(self.core, drive_dict, vectors, int(owners[0]))
            if made:
                logger.info("[drive→goal] 由内在驱动产生 %d 个目标：%s",
                            len(made), [m["objective"][:30] for m in made])
        except Exception:
            logger.debug("[drive→goal] 异常（已隔离）", exc_info=True)

    def _goal_decisions(self, now: float) -> list:
        """到期的长期目标 → 自主触发决策（P2，2026-10-23）。

        把 Goal 转成 `AutoTriggerDecision`，使其复用 P1 既有的
        去重 / 报备 / 执行管线 —— **不另起一套调度**。

        关键：把上次进展（`notes` 最后一条）一起喂给 Agent，
        否则每次推进都是「失忆的重来」，那不叫持续推进。

        fail-closed：goals 文件损坏 / 读取异常 ⇒ 返回空列表，不崩。
        """
        out = []
        try:
            from anima.agent.auto_trigger import AutoTriggerDecision, topic_key
            from anima.agent.goal import GoalStore
        except Exception:
            logger.debug("goal 模块导入失败", exc_info=True)
            return out
        try:
            persistence = getattr(self.core, "persistence", None)
            if persistence is None:
                return out
            store = GoalStore(persistence)
        except Exception:
            logger.debug("goals 读取失败（跳过该源）", exc_info=True)
            return out

        for g in store.active():
            if not g.due(now=now):
                continue
            _last_note = (g.notes[-1] if g.notes else "")
            _req = g.objective
            if _last_note:
                _req = f"{g.objective}\n（上次进展：{_last_note[:200]}）"
            out.append(AutoTriggerDecision(
                source=f"goal:{g.id}",
                request=f"推进这个长期目标并给出本次进展：{_req[:400]}",
                reason=f"本喵在追的目标「{g.objective[:40]}」到推进时间了",
                topic=topic_key(g.objective),
                goal_id=g.id,
            ))
            # 一次 tick 只派生一个目标，防同日爆量
            break
        return out

    async def _auto_trigger_tick(self, now: float) -> None:
        """P1 自主触发：自己发现该干的活，**先报备再执行**。

        ## 为什么是「报备」而不是「提议」（owner 明确拍板）

        计划书原设计是提议制（只建待办等你批），**owner 已否掉**，要求直接执行。
        但直接执行 = 它会自己启动任务、花 token、可能改文件/联网/发消息。
        折中为 **报备制**：立刻私聊说一声 → **马上开跑不等批准** → 可随时 cancel。
        「报备」只增加一条消息，消除的是「它在你背后动了一周你不知道」。

        ## 三道安全闸（fail-closed，任一不过就不启动）

        1. `autonomy.auto_trigger_enabled` 总开关（默认 **False**，需显式开启）
        2. `AutoTriggerGate`：每日上限 / 静默时段 / 熔断冷却
        3. 主题去重：与当日已触发主题相似 ⇒ 跳过

        ## 纪律

        * 触发源只读，绝不改它们的存储；
        * 状态独立落 `data/auto_trigger.json`，不污染 owner_tasks；
        * 报备发送失败**不阻断执行**（记日志 + 审计），因为「没报备」比「不干活」危害小；
        * 任何异常只记日志，绝不拖垮心跳。
        """
        cfg = getattr(self.core.config, "autonomy", None)
        if not getattr(cfg, "auto_trigger_enabled", False):
            # 🔴 2026-10-24：原来是**静默 return**，而 debug 级日志默认不可见
            #   ⇒ 「为什么今天没触发」完全无法归因（今天已因这类盲区吃三次亏）。
            #   这里按天节流打一条 debug：不开关时本就不该跑，不该刷屏。
            _td = self._bj_date_str(now) if self._bj_date_str else ""
            if getattr(self, "_autotrig_off_logged_day", "") != _td:
                self._autotrig_off_logged_day = _td
                logger.debug("[自主触发] 总开关 auto_trigger_enabled 未开启，今日不再尝试")
            return    # 默认关；开启后才会自主启动任务
        agent = getattr(self.core, "agent", None)
        if agent is None or not hasattr(agent, "submit"):
            logger.debug("[自主触发] core.agent 不可用，跳过", exc_info=False)
            return

        try:
            import datetime as _dt
            _hour = _dt.datetime.now(_BJ).hour
            from anima.agent.auto_trigger import (
                AutoTriggerGate, AutoTriggerState, collect_triggers,
                dedupe_against, DEFAULT_DAILY_MAX, FAIL_STREAK_TRIP,
                COOLDOWN_AFTER_TRIP_SEC, TOPIC_DEDUP_THRESHOLD,
            )
        except Exception:
            logger.debug("自主触发模块导入失败（已隔离）", exc_info=True)
            return

        _dd = getattr(getattr(self.core.config, "persistence", None), "data_dir", "data")
        state = AutoTriggerState(self.core.persistence)
        # 🔴 2026-10-24：跨天重置**必须立刻落盘**。
        #   原实现 roll_day 只改内存，而 save() 只在熔断/实际触发时调用
        #   ⇒ 当天若一直没触发，文件里仍是昨天 ⇒ 重启后跨天重置失效、
        #   当天配额凭空少一次（生产实证：10-24 早上文件仍记 10-23 / count=1）。
        if state.roll_day(self._bj_date_str(now)):
            state.save()
            logger.info("[自主触发] 日切：%s 的配额与主题已重置（并落盘）",
                        self._bj_date_str(now))

        # 🔴 2026-10-23：**清理必须放在闸门之前**。
        #   第一版我把它放在 gate 之后 ⇒ 凌晨 1–5 点（睡眠时段）gate 直接 return，
        #   清理永远跑不到（实测：部署后 100 秒内 0 次清理）。
        #   清理是**卫生维护**（把不可跟进的到期承诺移出队列），
        #   不是「自主启动任务」 ⇒ 不该受睡眠时段/免打扰/每日上限这些
        #   针对**启动任务**的闸门约束。但也必须在 `auto_trigger_enabled`
        #   之后 —— 总开关关闭时什么都不做（默认关的哲学不变）。
        #
        #   为什么需要清理：只跳过不清理 ⇒ 那条「本大爷这就帮你去瞅瞅……」
        #   一直 pending ⇒ 心跳每 30 秒判定一次并打日志，18 小时重复数千次。
        #   移出后垃圾不再进入任何后续判定 —— 一次清理，永久安静。
        #   这是**写操作**，与只读采集分离（见 quarantine 函数文档）。
        try:
            from anima.agent.auto_trigger import quarantine_unfollowable_debts
            _q = quarantine_unfollowable_debts(self.core, now=now)
            if _q:
                logger.warning("[自主触发] 已移出 %d 条不可跟进的承诺：%s",
                               len(_q), _q[:3])
        except Exception:
            logger.debug("[自主触发] 承诺清理失败（不影响后续）", exc_info=True)

        _sleep = None
        if getattr(cfg, "sleep_enabled", True):
            _sleep = (int(getattr(cfg, "sleep_start_hour", 1)),
                      int(getattr(cfg, "sleep_end_hour", 5)))
        _quiet = None
        if getattr(cfg, "quiet_hours_enabled", False):
            _quiet = (int(getattr(cfg, "quiet_hours_start_hour", 23)),
                      int(getattr(cfg, "quiet_hours_end_hour", 7)))
        gate = AutoTriggerGate(
            daily_max=int(getattr(cfg, "auto_trigger_daily_max", DEFAULT_DAILY_MAX)),
            quiet_hours=_quiet, sleep_hours=_sleep,
        )

        blocked = gate.blocked_reason(hour=_hour, state=state, now=now)
        if blocked:
            logger.debug("[自主触发] 跳过：%s", blocked)
            return

        # 熔断中（连续无产出）⇒ 不启动
        if state.fail_streak >= FAIL_STREAK_TRIP:
            state.cooldown_until = now + COOLDOWN_AFTER_TRIP_SEC
            state.fail_streak = 0
            state.save()
            logger.warning("[自主触发] 连续 %d 次无产出，熔断 %d 小时",
                           FAIL_STREAK_TRIP, COOLDOWN_AFTER_TRIP_SEC // 3600)
            return

        decisions = collect_triggers(self.core, now=now)

        # 🔴 2026-10-23 P2：把 **Goal（长期目标）** 接成第三类触发源。
        #   P1 建好了「闸门 + 报备 + 执行」的引擎，但两个原触发源
        #   （承诺 pending=0、健康异常仅 info 级）都空了 ⇒ 引擎空转。
        #   Goal 是**第一个真实数据源**（引擎与燃料的关系）。
        #   这里只把到期 Goal 转成决策，后续去重/报备/执行完全复用既有管线
        #   ⇒ 不重复实现一套调度。
        try:
            decisions.extend(self._goal_decisions(now))
        except Exception:
            logger.debug("[自主触发] Goal 决策收集失败（已隔离）", exc_info=True)

        # 🔴 2026-10-24：**B 站兴趣**作为第四类触发源（它自己去刷并分享）。
        #   与 Goal 的区别：Goal 是「它想做的事」，这里是「它看到想分享的东西」。
        #   因为搜索是 async 而 `collect_triggers` 是同步的，所以单独 await。
        #   闸门：每日 1 次 + 22h 间隔 + 仅 8–23 点 + 总开关默认关。
        #   分享落点是**私聊**（群里刷屏会招人烦）。
        try:
            if bool(getattr(getattr(self.core.config, "autonomy", None),
                            "bilibili_interest_enabled", False)):
                from anima.agent.bilibili_interest import build_request
                _bi = await build_request(self.core, now=now)
                if _bi:
                    from anima.agent.auto_trigger import AutoTriggerDecision
                    decisions.append(AutoTriggerDecision(
                        source="bilibili_interest",
                        request=_bi["request"],
                        reason=_bi["reason"],
                        topic=_bi["topic"],
                    ))
        except Exception:
            logger.warning("[B站兴趣] 决策收集失败（已隔离）", exc_info=True)

        if not decisions:
            return

        # 取第一条通过去重的（一次 tick 只启动一个，防同日爆量）
        picked = None
        for d in decisions:
            if dedupe_against(state.topics_today, d.request):
                logger.debug("[自主触发] 主题重复跳过：%s", d.request[:60])
                continue
            picked = d
            break
        if picked is None:
            return

        # ---- 先报备 ----
        owners = list(getattr(getattr(self.core.config, "permissions", None), "owners", []) or [])
        if not owners:
            logger.warning("[自主触发] permissions.owners 为空，跳过（不伪造目标）")
            return

        # ---- 再执行（报备失败也照跑，只记日志）----
        task = None
        try:
            task = await agent.submit(
                picked.request, int(owners[0]), auto_run=True,
                session={"detail_type": "private", "target_id": int(owners[0])},
            )
        except Exception:
            logger.exception("[自主触发] 提交任务失败（已隔离）")
            return

        # 🔴 2026-10-23：任务一启动就 **标记 Goal 已推进**。
        #   生产实测踩过：任务 4 步全部完成，但 `goal.last_run_at` 仍为 0
        #   ⇒ Goal 不知道自己推进过 ⇒ 下一个心跳立刻重复派生同一个目标。
        #   必须在**提交成功后立即**记，而不是等任务完成 —— 因为任务可能
        #   跑很久、也可能中途失败，但「已经为它投入过一次」这个事实成立。
        if getattr(picked, "goal_id", ""):
            try:
                from anima.agent.goal import GoalStore
                _gs = GoalStore(self.core.persistence)
                _gs.mark_run(picked.goal_id, task.id)
                # 🔴 2026-10-23：把 goal_id 写进 task.context，供任务**完成时**
                #   回写真实进展（`add_note`）。只 mark_run 不 add_note 的后果
                #   生产实测过：目标永远停在旧进展，下次派生又拿旧上下文去跑，
                #   等于「假装在推进」。这里用 context 而不是改 AgentTask
                #   schema —— 避免动状态机字段。
                try:
                    task.context = dict(task.context or {})
                    task.context["goal_id"] = picked.goal_id
                    agent.store.save(task)
                except Exception:
                    logger.debug("[自主触发] 写入 goal_id 到任务失败", exc_info=True)
                logger.info("[自主触发] 已标记目标 %s 推进（task=%s）",
                            picked.goal_id, task.id)
            except Exception:
                logger.debug("[自主触发] 标记目标推进失败（不影响任务）", exc_info=True)

        # 🔴 2026-10-24：B 站兴趣触发成功后**立刻记状态**（22h 间隔的依据）。
        #   与 Goal 的 mark_run 同理：提交即算「刷过一轮」，
        #   否则下次心跳会立刻再刷一次。
        if getattr(picked, "source", "") == "bilibili_interest":
            try:
                from anima.agent.bilibili_interest import load_state, save_state
                _st = load_state(self.core.persistence)
                _st["last_run_at"] = time.time()
                _st["last_topic"] = str(getattr(picked, "topic", "") or "")
                save_state(self.core.persistence, _st)
                logger.info("[B站兴趣] 已记本轮（22h 内不再刷）")
            except Exception:
                logger.warning("[B站兴趣] 状态落盘失败", exc_info=True)

        try:
            from anima.core.gateway import OutboundMsg
            _body = (
                f"本喵自己去干个活喵：\n"
                f"做什么：{picked.request[:90]}{'…' if len(picked.request) > 90 else ''}\n"
                f"为什么：{picked.reason[:90]}\n"
                f"任务：{task.id}\n"
                f"（不用回，本喵自己会做完汇报；想叫停就发 !anima agent cancel {task.id}）"
            )
            for oid in owners:
                try:
                    await self.core.gateway.send(OutboundMsg("private", int(oid), _body[:1500]))
                except Exception:
                    logger.warning("[自主触发] 报备发送失败 owner=%s（任务照跑）", oid, exc_info=True)
            # 计数 + 主题记忆（报备成败都算已启动，避免重复启动同一件事）
            state.count_today += 1
            state.topics_today.append(picked.request)
            state.save()
            logger.warning("[自主触发] 启动任务 %s（源=%s，今日 %d/%d）",
                           task.id, picked.source, state.count_today, gate.daily_max)
            try:
                audit = getattr(self.core, "audit", None)
                if audit is not None and hasattr(audit, "emit"):
                    audit.emit("auto_trigger_started", task_id=task.id, source=picked.source,
                               count_today=state.count_today, daily_max=gate.daily_max)
            except Exception:
                logger.debug("[自主触发] 审计写入失败", exc_info=True)
        except Exception:
            logger.debug("[自主触发] 报备流程异常（任务已启动）", exc_info=True)

    #: owner 每日交付的**持久化**日期标记文件（2026-10-22）。
    #: 此前标记只在内存 ⇒ 每次重启当天会再推一次。生产实证：09:00:29 推
    #: 「攒了 2 件」，09:08:33（部署重启后）又推「攒了 1 件」—— 8 分钟内两条
    #: 同质推送，而 owner 什么都没做。**重启是运维动作，不该造成重复打扰。**
    _OWNER_DIGEST_MARK_FILE = "owner_digest_mark.json"
    _RAINYUN_MARK_FILE = "rainyun_reminder_mark.json"

    @property
    def _owner_digest_mark(self) -> dict:
        """读取持久化标记（懒加载，失败返回空 dict）。"""
        if not hasattr(self, "_owner_digest_mark_cache"):
            _d = {}
            try:
                _p = getattr(getattr(self.core, "persistence", None), "read_json", None)
                if _p is not None:
                    _d = _p(self._OWNER_DIGEST_MARK_FILE, {}) or {}
            except Exception:
                logger.debug("owner 交付标记读取失败（按未推送处理）", exc_info=True)
            self._owner_digest_mark_cache = _d if isinstance(_d, dict) else {}
        return self._owner_digest_mark_cache

    def _owner_digest_mark_save(self, day: str) -> None:
        """写入当日标记（内存 + 落盘），使「当天已推」跨重启成立。"""
        self._owner_digest_mark_cache = {"last_date": str(day)}
        try:
            _w = getattr(getattr(self.core, "persistence", None), "write_json", None)
            if _w is not None:
                _w(self._OWNER_DIGEST_MARK_FILE, self._owner_digest_mark_cache)
        except Exception:
            logger.debug("owner 交付标记落盘失败（内存仍生效）", exc_info=True)

    async def _owner_digest_tick(self, now: float) -> None:
        """D-41：每日一次把 owner 待办推到私聊（此前只落 WebUI，从不推送）。

        🔴 为什么单独开一段而**不复用 agenda**：
        `propose_agenda(group_id, ...)` 的第一参数就是群 id，消费端会走群侧
        的全局冷却/占比/日预算/群自治开关 —— 那些闸门是防群刷屏的，套在
        「给 owner 的每日简报」上语义完全错误（何况 owner 私聊根本没有群）。

        🔴 为什么自建节流：私聊路径**不经过**群侧那套防刷屏纪律
        （`_pick_group` / 冷却 / 日预算全都只在群分支），不建就会绕过整套机制。

        纪律：日切一次 + 日期标记（沿用 `_last_secret_gen_date` 的模式）；
        fail-closed —— owners 为空时不伪造发送；任何异常只记日志。
        内容**不新造**，只搬已经产出的待办（`data/owner_tasks.json`）。
        """
        cfg = getattr(self.core.config, "autonomy", None)
        if not getattr(cfg, "owner_digest_enabled", True):
            return
        # 时刻门控：过了配置小时才发（不是整点触发，避免心跳抖动漏发）。
        import datetime as _dt
        try:
            _now_dt = _dt.datetime.now(_BJ)
        except Exception:
            _now_dt = _dt.datetime.now()
        if _now_dt.hour < int(getattr(cfg, "owner_digest_hour", 9)):
            return
        # 日切一次
        # 🔴 2026-10-22：**必须持久化**。此前 `_last_owner_digest_date` 只存内存 ⇒
        # 每次重启当天就会再推一次。生产实证：09:00:29 推「攒了 2 件」，
        # 09:08:33（我部署重启后）又推「攒了 1 件」——**8 分钟内两条同质推送**，
        # 而 owner 什么都没做。重启是我们自己的运维动作，不该造成重复打扰。
        _today = self._bj_date_str(now)
        _mark = self._owner_digest_mark
        if _mark.get("last_date") == _today:
            return

        owners = getattr(getattr(self.core.config, "permissions", None), "owners", None) or []
        if not owners:
            # fail-closed：无 owner 就不发，绝不伪造目标（同 handler._flush_owner_notice）
            logger.warning("[owner交付] permissions.owners 为空，跳过（不伪造发送）")
            self._owner_digest_mark_save(_today)
            return

        tasks = getattr(self.core, "tasks", None)
        if tasks is None or not hasattr(tasks, "list"):
            return
        pending = [t for t in tasks.list()
                   if getattr(t, "status", "") in ("planned", "running", "awaiting_approval")]
        if not pending:
            # 没东西可推就不发 —— 宁可安静，不做「今日无异常」这类自我打卡。
            self._owner_digest_mark_save(_today)
            return

        limit = max(1, int(getattr(cfg, "owner_digest_max_items", 5)))
        # 🔴 2026-10-21：推送前按**主题**合并同病灶多条 —— 否则「回复质量退化」
        # 那种带计数的标题会让同一件事在白名单里重复出现 3 次（生产实证：
        # 09-17 的 3 张僵尸单在首次推送里占了 3/5 的内容，噪音盖住真待办）。
        _groups: dict[str, list] = {}
        for t in pending:
            _k = _issue_topic_key(str(getattr(t, "request", "") or ""))
            _groups.setdefault(_k, []).append(t)
        rows = []
        for _k, _items in list(_groups.items())[:limit]:
            _req = str(getattr(_items[0], "request", "") or "").strip().replace("\n", " ")
            _line = "· " + (_req[:60] + "…" if len(_req) > 60 else _req)
            if len(_items) > 1:
                _line += "（同主题共 %d 条，此处合并）" % len(_items)
            rows.append(_line)
        more = len(_groups) - len(rows)
        body = "本喵这边攒了 %d 件还等着处理的事喵：\n%s" % (len(_groups), "\n".join(rows))
        if more > 0:
            body += "\n（还有 %d 件没列出来）" % more

        try:
            from anima.core.gateway import OutboundMsg
            sent = False
            for oid in owners:
                try:
                    await self.core.gateway.send(OutboundMsg("private", int(oid), body))
                    sent = True
                except Exception:
                    logger.warning("[owner交付] 私聊发送失败 owner=%s", oid, exc_info=True)
            if sent:
                self._owner_digest_mark_save(_today)
                logger.info("[owner交付] 已推送 %d 条待办给 owner", len(pending))
                try:
                    audit = getattr(self.core, "audit", None) or getattr(self.core, "auditor", None)
                    if audit is not None and hasattr(audit, "emit"):
                        audit.emit("owner_digest_delivered", count=len(pending),
                                   listed=len(rows))
                except Exception:
                    logger.debug("owner 交付审计写入失败", exc_info=True)
        except Exception:
            logger.debug("[owner交付] 构造或发送失败（已隔离）", exc_info=True)

    # ------------------------------------------------------------------
    # 雨云签到提醒（2026-10-26）
    # ------------------------------------------------------------------

    def _rainyun_mark(self) -> dict:
        """读持久化标记（懒加载，失败按「今天没发过」处理）。"""
        if not hasattr(self, "_rainyun_mark_cache"):
            _d = {}
            try:
                _p = getattr(getattr(self.core, "persistence", None), "read_json", None)
                if _p is not None:
                    _d = _p(self._RAINYUN_MARK_FILE, {}) or {}
            except Exception:
                logger.debug("雨云提醒标记读取失败（按未推送处理）", exc_info=True)
            self._rainyun_mark_cache = _d if isinstance(_d, dict) else {}
        return self._rainyun_mark_cache

    def _rainyun_mark_save(self, day: str) -> None:
        """写当日标记（内存 + 落盘），使「当天已推」跨重启成立。"""
        self._rainyun_mark_cache = {"last_date": str(day)}
        try:
            _w = getattr(getattr(self.core, "persistence", None), "write_json", None)
            if _w is not None:
                _w(self._RAINYUN_MARK_FILE, self._rainyun_mark_cache)
        except Exception:
            logger.debug("雨云提醒标记落盘失败（内存仍生效）", exc_info=True)

    async def _rainyun_reminder_tick(self, now: float) -> None:
        """每日一次：雨云「每日签到」还没领就私聊提醒 owner。**只读**。

        ## 为什么单独一段而不复用 agenda / owner_digest

        - 不复用 `propose_agenda`：它的第一参数是群 id，消费端会套上群侧防刷屏
          闸门（冷却/占比/日预算）——语义完全不对（owner 私聊没有群）。
        - 不复用 `_owner_digest_tick`：那个搬的是 `owner_tasks.json` 里的待办，
          与本需求数据源不同；混在一起会让「今天有没有待办」和
          「今天签没签到」互相干扰（一个为空就整体不发）。

        但**纪律完全照抄**它（本项目在「日切一次」上踩过多个坑）：

        - 日切一次 + **持久化**标记（`data/rainyun_reminder_mark.json`）。
          只存内存的话：重启当天会再推一次（`owner_digest` 的真实事故：
          09:00 推一次、09:08 部署重启后又推一次，8 分钟内两条同质推送）。
        - 过了配置小时才发（`rainyun.hour`，默认 9），不整点触发，避免心跳抖动漏发。
        - `permissions.owners` 为空 ⇒ **不发也不标记**（fail-closed）：
          下个 tick 若 owner 出现了还能正常提醒，不伪造目标。
        - 任何异常只记日志、绝不抛（本函数被 `_tick` 的 try 包着，但不依赖它）。

        ## 为什么只提醒不自动领

        领取接口需要腾讯 TCaptcha 通过后的票据。**LLM 识图对这类验证码无效**
        （无感验证没有图可识，通过后是一次性加密票据）——详见
        `anima/tools/builtin/rainyun.py` 的模块 docstring。
        """
        cfg = getattr(self.core.config, "rainyun", None)
        if cfg is None or not bool(getattr(cfg, "enabled", False)):
            return

        # 时刻门控：过了配置小时才发
        import datetime as _dt
        try:
            _now_dt = _dt.datetime.now(_BJ)
        except Exception:
            _now_dt = _dt.datetime.now()
        if _now_dt.hour < int(getattr(cfg, "hour", 9)):
            return

        _today = self._bj_date_str(now)
        if self._rainyun_mark().get("last_date") == _today:
            return

        owners = getattr(getattr(self.core.config, "permissions", None), "owners", None) or []
        if not owners:
            # fail-closed：不伪造发送，也不标记（等下个 tick 再试）
            logger.warning("[雨云] permissions.owners 为空，跳过提醒")
            return

        # 构造文案（只读；含网络请求，放线程池避免阻塞心跳）
        try:
            from anima.tools.builtin.rainyun import build_daily_reminder
            body = await asyncio.to_thread(build_daily_reminder, self.core)
        except Exception:
            logger.warning("[雨云] 生成提醒失败（已隔离）", exc_info=True)
            return
        if not body:
            # 已签到 / 接口不可用 ⇒ 静默。
            # 🔴 这里**不标记**：接口失败时标记了，今天就再也不会重试；
            #   而「已签到」本来就不需要发。两种情况都保持「下次 tick 再看」。
            return

        sent = False
        try:
            from anima.core.gateway import OutboundMsg
            for oid in owners:
                try:
                    await self.core.gateway.send(OutboundMsg("private", int(oid), body))
                    sent = True
                except Exception:
                    logger.warning("[雨云] 私聊发送失败 owner=%s", oid, exc_info=True)
        except Exception:
            logger.debug("[雨云] OutboundMsg 导入失败", exc_info=True)
            return
        if sent:
            self._rainyun_mark_save(_today)
            logger.info("[雨云] 已提醒 owner 签到")
            try:
                audit = getattr(self.core, "audit", None) or getattr(self.core, "auditor", None)
                if audit is not None and hasattr(audit, "emit"):
                    audit.emit("rainyun_reminder_delivered", owners=len(owners))
            except Exception:
                logger.debug("雨云提醒审计写入失败", exc_info=True)

    async def _tick(self) -> None:
        # 🔴 2026-10-23：**成长快照**（每日一次，让「它在变」可见）。
        #   North Star 是「每天比昨天多一点」——这件事必须可核对。
        #   ⚠️ 刻意放在 `_auth` 判空**之外**：快照不需要 memory_authority
        #   （它能读到 goals/mood，宪法链部分会自己兜底）。此前放在 `_auth`
        #   分支里 ⇒ 一旦 `_auth is None` 就永不记录（部署后实测 growth 目录
        #   都没建出来）。
        try:
            if bool(getattr(getattr(self.core.config, "autonomy", None),
                            "growth_snapshot_enabled", False)):
                from anima.constitution.growth import take_snapshot
                # 🔴 2026-10-23：**不要用 `now`** —— 它是 `_tick` 后面才赋值的
                #   局部变量，在函数开头引用会 `UnboundLocalError`
                #   （生产实测：每 30 秒一条「快照失败」，由改成 warning 后才看见）。
                # 🔴 2026-10-23 二次修订：改为**每次心跳都 upsert（同日覆盖）**。
                #   原来「今天记过就跳过」会把当天早些时候的**过时数据**凝固成终值
                #   （生产实例：13:33 记下 episodes=0，而修好 hydrate 后真实值是 16）
                #   ⇒ 明天的对比会把「修 bug 的跳变」误当成「一天的成长」。
                #   upsert 让每日快照反映**当天最终状态**；同日不会产生多条。
                _snap = take_snapshot(self.core)
                _d = self._bj_date_str(time.time())
                if _snap and getattr(self, "_growth_logged_day", "") != _d:
                    self._growth_logged_day = _d
                    _mem = _snap.get("memory") or {}
                    logger.info("[成长] 今日快照已更新（traits=%d 项, episodes=%s）",
                                len(_snap.get("traits") or {}),
                                _mem.get("episodes", 0) if isinstance(_mem, dict) else 0)
                elif not _snap:
                    logger.warning("[成长] 快照采集返回空，下次心跳重试")
        except Exception:
            logger.warning("[成长] 快照失败（已隔离）", exc_info=True)

        # 🔴 2026-10-23：**情感桥接放在 _tick 的无条件路径上**。
        #   第二次踩坑：上一版把它放在「主动发言分支」里（`has_hook` 之后），
        #   而那个分支只在**有接话点**时才执行 ⇒ 群里安静时桥接永不运行
        #   （部署后等 90 秒，日志仍 0 条）。
        #   喂料应该每 tick 无条件执行一次（幂等，重复调用不追加）。
        try:
            if bool(getattr(
                    getattr(self.core.config, "autonomy", None),
                    "emotion_bridge_enabled", False)):
                _auth = getattr(self.core, "memory_authority", None)
                if _auth is not None:
                    from anima.constitution.emotion_bridge import hydrate_emotion_memory
                    _dd = getattr(getattr(self.core.config, "persistence", None),
                                  "data_dir", "data")
                    _stats = hydrate_emotion_memory(_auth.engine, _dd, core=self.core)
                    if _stats.get("semantic") or _stats.get("relations"):
                        logger.info("[情感桥] 宪法记忆喂料：semantic+%d relations+%d",
                                    _stats["semantic"], _stats["relations"])
                    # 🔴 2026-10-23：**最后一环 —— Drive → 它自己想做的事**。
                    #   Drive 活了之后没有出口：`drive_gate` 只用来「压制主动发言」，
                    #   从未用来「产生它想做的事」。这里把 Drive 接到 Goal：
                    #   由**真实的 need.vectors** 生成目标，文案逐字取自链条产出
                    #   （不编造），动机写进 notes（可追溯）。
                    try:
                        if bool(getattr(
                                getattr(self.core.config, "autonomy", None),
                                "drive_to_goal_enabled", False)):
                            self._maybe_create_goals_from_drive(_auth)
                    except Exception:
                        logger.warning("[drive→goal] 生成失败（已隔离）", exc_info=True)
                else:
                    logger.debug("[情感桥] memory_authority 为 None，跳过喂料")
        except Exception:
            logger.warning("[情感桥] 喂料失败（已隔离）", exc_info=True)

        # P1-4：主动优先级单一事实来源（core/proactive.py）。导入失败时回落 0.5，
        # 绝不因为拿不到注册表就让心跳挂掉。
        _pro_cfg = getattr(self.core.config, "proactive", None)
        try:
            from anima.core.proactive import priority_for as _pf
        except Exception:  # pragma: no cover
            def _pf(source, cfg=None):  # type: ignore[misc]
                return 0.5

        self._expire_active_feedback(time.time())
        # 被动回复效果：与主动发言同一心跳节拍结算窗口（B1 只采集，不改行为）。
        self._expire_reply_feedback()
        try:
            self.core.health_history.record()
        except Exception:
            logger.exception("心跳健康采样失败")
        # R16: periodic anomaly self-check (read-only + dedupe + audit)
        try:
            self._anomaly_selfcheck()
        except Exception:
            logger.exception("心跳异常自检失败，已隔离")
        # R16 闭环4: periodic config/permission drift check (read-only + audit)
        try:
            self._config_drift_check()
        except Exception:
            logger.exception("心跳配置漂移自检失败，已隔离")
        # P2-1: health_proof 主动告警——周期把稳定性异常信号转成 owner 待办。
        # 带冷却，避免每个 tick 重复生成同一条待办。
        try:
            self._health_alerts_tick()
        except Exception:
            logger.exception("心跳健康告警检查失败，已隔离")
        try:
            backups = getattr(self.core, "backups", None)
            if backups is not None:
                result = backups.maintain()
                if result.get("created"):
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        audit.emit("scheduled_state_backup_created", backup_id=result["created"]["id"])
        except Exception:
            logger.exception("运行状态备份维护失败，已隔离")
        # 模拟生活节律（最先执行）
        await self._life_checkpoints()

        # Sylann 的无输入 tick 与 Anima 心跳共用同一调度器。引擎自身会按会话
        # 最小间隔合并，适配层只保存摘要，绝不绕过 Agenda/权限直接发 QQ 消息。
        sylann = getattr(self.core, "sylann", None)
        if sylann is not None:
            try:
                observations = await sylann.tick()
                if observations:
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        audit.emit("sylann_tick", count=len(observations))
            except Exception:
                logger.exception("Sylann 心跳推进失败，已隔离")

        # 熔断检查
        if self._degraded:
            await self._check_recovery()
            self.persist()
            return

        # 发展层级 1：记忆沉淀闭环（细节→要点→特质），每日一次（fail-soft）。
        try:
            from anima.memory.consolidation import run_daily_consolidation
            _today_c = self._bj_date_str(time.time())
            if getattr(self, "_last_consolidation_date", "") != _today_c:
                self._last_consolidation_date = _today_c
                self.persist()
                _launch_bg(run_daily_consolidation(self.core, time.time()),
                           "memory_consolidation")
                logger.info("[记忆沉淀] 每日沉淀已调度")
        except Exception:
            logger.debug("记忆沉淀调度失败（已隔离）", exc_info=True)

        # 多 Bot 发现：每日至多一次扫描群成员（受 social.bot_discovery_enabled 保护）。
        # 平台 role=='bot' 自动确认；昵称命中只记建议日志，绝不自动写入权威集。
        try:
            await self._bot_discovery_tick(time.time())
        except Exception:
            logger.debug("bot 发现扫描调度失败（已隔离）", exc_info=True)

        # 记忆可靠性（深审批次5）：关系/对话记忆节流落盘。
        # relation 走 deferred 队列（touch 每次消息登记，此处批量 fsync）；
        # chat_memory.save 自带脏守卫，干净时零写入。
        try:
            from anima.cognitive.relation import get_relation
            get_relation().flush_deferred_if_any()
        except Exception:
            logger.debug("关系延迟落盘 flush 失败", exc_info=True)
        try:
            from anima.memory.chat_memory import get_chat_memory
            get_chat_memory().save()
        except Exception:
            logger.debug("chat_memory 补充落盘失败", exc_info=True)

        # 收敛计划 E1：自治工程自检（2h 节奏，与 ops_cleanup 同量级）。
        # reply_quality 退化信号 → 自动建 owner 待办（fail-soft、open 去重）。
        try:
            _eng_now = time.time()
            if _eng_now - getattr(self, "_last_eng_selfcheck", 0) >= 7200:
                self._last_eng_selfcheck = _eng_now
                from anima.agent.issue_sources import collect_self_monitor_issues
                rq = getattr(self.core, "reply_quality", None)
                rep = rq.report() if rq is not None and hasattr(rq, "report") else None
                for issue in collect_self_monitor_issues(reply_quality_report=rep):
                    tasks = getattr(self.core, "tasks", None)
                    if tasks is None:
                        continue
                    # 🔴 2026-10-21 修复（生产实证 34 天僵尸单）：
                    #   此前对所有 DevIssue 一律 `tasks.create(kind="diagnostic")`。
                    #   但自愈机制 `healed_fault_tickets` 要求 proposal 里有
                    #   `date` + `signals`，且 **signal 必须在 `_FAULT_RULES` 规则表内**
                    #   （否则 `sig not in thresholds` ⇒ 直接跳过）。
                    #   而「回复质量退化」这类议题**根本不在规则表内** ⇒ 建出来就
                    #   **永远无法自愈关闭** ⇒ 一旦开出即终身挂在 owner 待办里。
                    #   实证：09-17 6 小时内连开 3 张（标题带计数，全等去重失效），
                    #   躺到 10-21 首次 owner 推送时才被发现，每天污染推送内容。
                    #   ⇒ 改为**只对可自愈的规则信号建单**；其余仅记日志
                    #   （信号不丢：WebUI 侧 engineering_loop 仍在消费同一函数）。
                    _sig = _issue_signal_key(issue.title)
                    if not _sig:
                        logger.info("[自治自检] 非可自愈信号，仅记录不建单：%s", issue.title)
                        continue
                    # 去重按**主题**判（标题含计数，全等比较会漏判）
                    _topic = _issue_topic_key(issue.title)
                    _dup = False
                    for t in tasks.list():
                        if getattr(t, "status", "") not in ("planned", "running", "awaiting_approval"):
                            continue
                        if _issue_topic_key(getattr(t, "request", "") or "") == _topic:
                            _dup = True
                            break
                    if _dup:
                        continue
                    owner = int((getattr(self.core.config.permissions, "owners", None) or [0])[0])
                    _task = tasks.create(issue.title, owner, [issue.detail])
                    # 登记自愈判定所需元数据（date + signals 均在 _FAULT_RULES 内）
                    try:
                        tasks.set_proposal(_task.id, {
                            "date": self._bj_date_str(time.time()),
                            "signals": [_sig],
                            "signal": _sig,
                            "source": "self_monitor",
                        }, evidence="自治自检建单：登记自愈判定所需元数据")
                    except Exception:
                        logger.debug("[自治自检] 补元数据失败（不影响建单）", exc_info=True)
                    logger.warning("[自治自检] 生成 owner 待办：%s", issue.title)
        except Exception:
            logger.debug("自治工程自检失败（已隔离）", exc_info=True)

        # 日预算重置
        now = time.time()
        # 每日历事件只入队，不绕过 Agenda 的冷却、预算和发送确认。
        calendar = getattr(self.core, "calendar", None)
        if calendar is not None:
            _once = list(getattr(calendar, "once_due", lambda now: [])(now)) if hasattr(calendar, "once_due") else []
            for event in [*calendar.due(now), *calendar.weekly_due(now), *_once]:
                workflows = getattr(self.core, "workflows", None)
                run = workflows.trigger("calendar", event.id, event.group_id, event.text,
                                        event_key=str(getattr(event, "last_triggered_day", ""))) if workflows else None
                agenda_item = self.core.propose_agenda(event.group_id, event.text,
                                priority=_pf("calendar", _pro_cfg),
                                                       expires_at=now + 6 * 3600, source="calendar")
                if run:
                    workflows.bind_intent(run.id, getattr(agenda_item, "intent_id", ""))
                audit = getattr(self.core, "audit", None)
                if audit:
                    audit.emit("calendar_event_enqueued", calendar_id=event.id,
                               group_id=event.group_id)
        if datetime.fromtimestamp(now, _BJ).date() != datetime.fromtimestamp(self._day_reset, _BJ).date():
            self._group_speak_count.clear()
            self._group_total_msgs.clear()
            self._group_last_speak.clear()
            self._tokens_today = 0
            self._day_reset = now
            # 每日低峰：网页知识库置信衰减与过期清理（P1，失败隔离）
            try:
                from anima.memory.web_knowledge import WebKnowledgeStore
                data_dir = str(self.core.config.persistence.data_dir)
                removed = WebKnowledgeStore(data_dir).decay()
                if removed:
                    logger.info("WebKnowledgeStore 每日衰减清理 %d 条过期知识", removed)
            except Exception as e:
                logger.warning("WebKnowledgeStore 每日衰减失败（已忽略）: %s", e)
            # P1-2：夜间记忆压缩（置信度衰减 + 相似合并 + 超容淘汰）
            try:
                from anima.crossgroup.memory.compression import (
                    get_compression as _get_compression,
                )
                _mc = getattr(self.core.config, "memory_compression", None)
                if getattr(_mc, "enabled", True):
                    _cgm = getattr(self.core, "crossgroup", None)
                    if _cgm is None:
                        from anima.crossgroup import get_crossgroup_manager as _gcgm
                        _cgm = _gcgm()
                    _rep = await _get_compression(
                        str(self.core.config.persistence.data_dir), _mc,
                    ).maybe_run(
                        crossgroup_manager=_cgm,
                        vector_memory=getattr(self.core, "vector_memory", None),
                        llm=getattr(self.core, "llm", None),
                    )
                    if _rep.get("merged") or _rep.get("evicted"):
                        logger.info(
                            "夜间记忆压缩：%d 用户，合并 %d 条，淘汰 %d 条",
                            _rep.get("users", 0), _rep.get("merged", 0),
                            _rep.get("evicted", 0),
                        )
            except Exception as e:
                logger.warning("夜间记忆压缩失败（已忽略）: %s", e)
            # P1-1：到期承诺 → 走既有 propose_agenda 主动管线（继承限流/冷却）
            try:
                from anima.cognitive.debt import get_debt_manager as _get_dm
                _dt = getattr(self.core.config, "debt_tracking", None)
                if getattr(_dt, "enabled", True):
                    _dd = str(self.core.config.persistence.data_dir)
                    _mgr = _get_dm(_dd)
                    for _d in _mgr.check_due(
                        reminded_max=getattr(_dt, "reminded_max", 3),
                        remind_interval_sec=getattr(_dt, "remind_interval_sec", 86400),
                        grace_hours=getattr(_dt, "grace_hours", 48.0),
                    ):
                        if not str(_d.source_group).isdigit():
                            continue
                        self.core.propose_agenda(
                            int(_d.source_group),
                            # 🔴 2026-09-20：**不得回贴 `due_hint` 原文**。此前是
                            #   `…{content}（约好 {due_hint}）…别装作没这回事。` ——
                            #   `due_hint` 是解析器**原样保留**的切片，一句斗嘴
                            #   「等下，你怎么也抢本喵的自称啊」就这么被搬进主动发言，
                            #   读起来就是"突然引用一件不存在的事"。措辞也一并软化
                            #   （训斥式「别装作没这回事」与人格不符）。
                            f"你之前答应过的事：{_d.content[:60]}。看看现在能不能做了，"
                            "能就顺手做了，不能就直说还没弄好。",
                            priority=_pf("debt_due", _pro_cfg),
                            expires_at=time.time() + 6 * 3600,
                            source="debt_due",
                        )
                        _mgr.mark_reminded(_d.id)
            except Exception as e:
                logger.warning("debt 到期检查失败（已忽略）: %s", e)
            # R16 闭环5：每日低峰清理过期审计文件（整文件粒度），保持可回溯窗口
            # 连续不断档，同时防止小盘 VPS 上审计日志无限膨胀。
            try:
                audit = getattr(self.core, "audit", None)
                if audit is not None:
                    removed_audit = audit.prune(retention_days=90)
                    if removed_audit:
                        logger.info("audit 保留期清理 %d 个过期审计文件", removed_audit)
            except Exception as e:
                logger.warning("audit 清理失败（已忽略）: %s", e)

        # Token 预算检查
        if self.token_budget and self._tokens_today >= self.token_budget:
            logger.debug("Token 日预算已耗尽")
            self.persist()
            return

        # 低频运维清理：僵尸 selfcode / 粘滞笔记（约每 2 小时一次，失败隔离）
        try:
            last_cleanup = float(getattr(self, "_last_ops_cleanup", 0.0) or 0.0)
            if now - last_cleanup >= 7200:
                self._last_ops_cleanup = now
                from anima.cognitive.ops_cleanup import run_ops_cleanup
                data_dir = str(self.core.config.persistence.data_dir)
                # 运行中只清僵尸任务与玩具欲望，不反复砍笔记（笔记由启动/CLI 一次清）
                rep = run_ops_cleanup(
                    data_dir=data_dir,
                    cancel_all_running_selfcode=True,
                    purge_notes=False,
                    purge_world=False,
                    resolve_toy_desires=True,
                )
                if rep.cancelled_selfcode or rep.resolved_desires:
                    logger.info(
                        "ops_cleanup: cancelled_selfcode=%d resolved_desires=%d",
                        rep.cancelled_selfcode, rep.resolved_desires,
                    )
                # SelfCode 效果评估：只标记建议卸载，永不自动删除
                try:
                    if bool(getattr(self.core.config.self_code, "enabled", False)):
                        from anima.cognitive.selfcode import get_selfcode_manager
                        sug = get_selfcode_manager(self.core).evaluate_tools()
                        if sug:
                            logger.info(
                                "selfcode evaluate: %d tool(s) suggest uninstall",
                                len(sug),
                            )
                except Exception:
                    logger.debug("selfcode evaluate skipped", exc_info=True)
        except Exception:
            logger.exception("ops_cleanup 心跳清理失败（已隔离）")

        # 每小时刷新自身群状态（头衔/名片），变更写 self_notes（P3）
        try:
            if now - float(getattr(self, "_last_self_status_refresh", 0.0) or 0.0) >= 3600:
                self._last_self_status_refresh = now
                await self._refresh_self_status()
        except Exception:
            logger.exception("自身状态刷新失败（已隔离）")

        # 自主发现路径（不依赖是否找到可主动发言的群）：
        # 故障工单扫描 + 自升级熔断/提案。原先挂在 _pick_group 成功之后，
        # 群沉默时整段永不运行，导致 SelfCode 洪水无法被心跳侧熔断。
        dcfg = getattr(self.core.config, "danger", None)
        if dcfg and getattr(dcfg, "self_improvement", False) \
                and getattr(dcfg, "self_improvement_confirm", False):
            try:
                from anima.cognitive.danger import get_danger_behaviors
                danger = get_danger_behaviors(core=self.core, cfg=dcfg)
                if danger is not None:
                    _launch_bg(danger.maybe_self_improvement(), "danger_self_improve")
            except Exception:
                logger.exception("自升级调度失败")
        try:
            from anima.cognitive.controlled_iteration import get_controlled_iteration
            data_dir = str(self.core.config.persistence.data_dir)
            iterate = get_controlled_iteration(data_dir)
            # 约每 10 个 tick（~5min）扫一次；内部还有节流
            if int(time.time() // max(1, float(self.tick_sec))) % 10 == 0:
                opened = iterate.scan_quality_and_open_tickets(self.core)
                if opened:
                    logger.info("受控迭代：新开 %d 个故障工单", len(opened))
                if iterate.is_auto_repair_enabled(self.core):
                    _launch_bg(iterate.maybe_auto_repair(self.core), "controlled_auto_repair")
        except Exception:
            logger.exception("受控迭代调度失败（已隔离）")

        # 自主外部探索独立于群聊活跃度：先只读学习公开资料，只有明确来源群、
        # 达到价值阈值且配置允许时才会创建 Agenda，仍受后续全部出站门控。
        try:
            exploration_cfg = getattr(self.core.config, "exploration", None)
            if exploration_cfg and getattr(exploration_cfg, "enabled", False):
                from anima.cognitive.exploration import get_exploration_manager
                _launch_bg(get_exploration_manager(self.core).maybe_run(), "exploration_maybe_run")
        except Exception:
            logger.exception("自主外部探索调度失败（已隔离）")

        # B2 每日秘密自动生成（每天至多一次，去重落盘）
        try:
            acfg = self.core.config.autonomy
            if getattr(acfg, "secret_auto_enabled", True):
                if getattr(self, "_last_secret_gen_date", "") != self._bj_date_str(now):
                    from anima.cognitive.secret_auto import maybe_generate_daily_secrets
                    dd = str(self.core.config.persistence.data_dir)
                    added = maybe_generate_daily_secrets(dd, now=now)
                    if added:
                        logger.info("[秘密] 每日自动生成 %d 条", len(added))
                    self._last_secret_gen_date = self._bj_date_str(now)
        except Exception:
            logger.debug("每日秘密生成失败（已隔离）", exc_info=True)

        # D-41：owner 私聊交付（每日一次，把「已经在产出却没人看」的待办推出去）
        try:
            await self._owner_digest_tick(now)
        except Exception:
            logger.debug("owner 私聊交付失败（已隔离）", exc_info=True)

        # 🔴 2026-10-26：**雨云每日签到提醒**（只读）。
        # 与 `_owner_digest_tick` 同一纪律：日切一次 + 持久化标记 +
        # 过了配置小时才发 + 无 owner 不伪造发送 + 任何异常只记日志。
        # 为什么只提醒不自动领：领取接口要过人机验证（腾讯 TCaptcha），
        # 见 `anima/tools/builtin/rainyun.py` 的模块 docstring。
        try:
            await self._rainyun_reminder_tick(now)
        except Exception:
            logger.debug("雨云签到提醒失败（已隔离）", exc_info=True)

        # 🔴 P1 自主触发（2026-10-21）：它自己发现该干的活，**先报备再执行**。
        # owner 明确否掉「提议制」（不批就不动），要求直接执行；这里按确认的
        # 折中实现：立刻私聊报备（做什么/为什么/几步）→ 马上开跑（不等批准）
        # → 随时可 `!anima agent cancel <id>` 叫停 → 完成后推结果。
        try:
            await self._auto_trigger_tick(now)
        except Exception:
            logger.debug("自主触发调度失败（已隔离）", exc_info=True)

        # 议程消费（约定跟进 / calendar 等）
        # 注意：议程消费在 _circadian_gate 之前，必须单独做夜间静默检查，
        # 否则凌晨仍会主动发言（如“秽土转生”事件）。
        # P3-3: 除硬静默 0-5 点外，也尊重用户可配置 quiet_hours 免打扰窗口。
        _quiet_hour = datetime.now(_BJ).hour in (0, 1, 2, 3, 4, 5) or self._in_quiet_hours()
        agenda = getattr(self.core, "agenda", None)
        if agenda is not None and not _quiet_hour:
            # 深审 P2-5：owner 对群配置 autonomy=false 时，议程出站同样遵守
            # （此前只有随机接话路径检查，日历/订阅/承诺议程会照发）
            _gpolicies = getattr(getattr(self.core.config, "permissions", None),
                                 "group_policies", None) or {}

            def _group_allowed(gid: int) -> bool:
                p = _gpolicies.get(str(gid)) if isinstance(_gpolicies, dict) else None
                return not (isinstance(p, dict) and p.get("autonomy") is False)

            # P1-4：先清掉被禁群里的议程，再把剩余候选按优先级降序逐个尝试
            # （队首被挡不再堵死整轮）。
            for _it in list(getattr(agenda, "items", []) or []):
                if (str(getattr(_it, "status", "")) == "pending"
                        and not _group_allowed(int(getattr(_it, "group_id", 0) or 0))):
                    agenda.cancel(_it.id)
                    logger.info("议程跳过：群 %s 已配置 autonomy=false", _it.group_id)
            try:
                from anima.core.proactive import rank_agenda as _rank_agenda
                _candidates = _rank_agenda(agenda.ready_items(now=now), now,
                                           group_allowed=_group_allowed, limit=5)
            except Exception:
                logger.debug("议程优先级排序失败（回退单条）", exc_info=True)
                _candidates = [agenda.peek_ready(now=now)] if agenda.peek_ready(now=now) else []

            for agenda_item in _candidates:
                if now - self._last_active_speak < self.active_global_cooldown_sec:
                    break  # 遵守全局冷却（P1-4 不为"要事"开口子）
                subject = getattr(self.core, "subject", None)
                intent = subject.claim_agenda(agenda_item, now) if subject else None
                if subject and intent is None:
                    # 深审 P1-1 僵尸清理：intent 已 terminal/被其它 item 占用时
                    # claim 永远失败，不清理会永久压住队列里所有低优先级议程
                    agenda.cancel(agenda_item.id)
                    logger.info("议程僵尸清理：item=%s 对应意图已终结", agenda_item.id)
                    continue
                if subject and intent:
                    subject.ledger.mark_dispatched(intent.id, "议程已进入出站")
                spoke = await self._speak(agenda_item.group_id, now, agenda_item.text_intent) if (subject is None or intent) else False
                if spoke:
                    agenda.consume(agenda_item.id)
                    if subject and intent:
                        subject.ledger.complete(intent.id, "QQ 出站确认成功")
                        workflows = getattr(self.core, "workflows", None)
                        if workflows:
                            workflows.complete_by_intent(intent.id)
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        audit.emit("agenda_consumed", agenda_id=agenda_item.id,
                                   group_id=agenda_item.group_id, intent_id=getattr(intent, "id", ""))
                    break  # 一轮只主动说一次
                if subject and intent:
                    max_attempts = int(getattr(self, "active_intent_max_attempts", 5) or 5)
                    subject.ledger.release(
                        intent.id, "本轮未满足出站条件", max_attempts=max_attempts,
                    )
            if _candidates:
                self.persist()
                return

        if not self._circadian_gate():
            # 睡眠时段（01-05 点）：不主动发言，但生成梦境（灵魂补丁 A2）。
            # 每夜至多一次，异步调度不阻塞心跳；DreamEngine 缺失时静默跳过。
            try:
                from anima.cognitive.dream_engine import DreamEngine
                data_dir = str(self.core.config.persistence.data_dir)
                eng = DreamEngine(data_dir, self.core)
                _launch_bg(eng.maybe_generate_during_sleep(now), "dream_during_sleep")
            except Exception:
                logger.debug("梦境生成调度失败（已隔离）", exc_info=True)
            self.persist()
            return

        # A3 早晨分享梦境：08:00「醒来」，读昨晚最后一条梦境，10% 概率主动在群里分享
        # （并让 mood_tag 覆盖次日 soul 初始情绪）。每「天」至多尝试一次。
        # config.morning_dream_share_enabled=False 时彻底关闭此类模板式主动分享。
        try:
            if (getattr(getattr(self.core.config, "autonomy", None), "morning_dream_share_enabled", True)
                    and self._maybe_morning_dream_share(now)):
                # 深审 P1-3：抽中即标记（日期进 persist），先于挑群——
                # 挑群失败/重启不再重复重抽与建议程
                self._last_morning_dream_date = self._bj_date_str(now)
                self.persist()
                gid = self._pick_group(now)
                if gid is not None:
                    from anima.cognitive.dream_engine import DreamEngine
                    eng = DreamEngine(str(self.core.config.persistence.data_dir), self.core)
                    dream = eng.get_last_dream()
                    if dream and dream.get("dream"):
                        _share = (
                            f"昨晚本喵做了一个好奇怪的梦…{dream['dream']}"
                            f"（抖耳朵）"
                        )
                        # #6 早晨统一（2026-08-28）：梦境分享改走 agenda（与早安同纪律轨道），
                        # 消除绕过 30min 全局冷却的旁路；not_before_at=次日 8 点。
                        _now_bj = datetime.fromtimestamp(now, _BJ)
                        _next8 = (_now_bj + timedelta(days=1)).replace(
                            hour=8, minute=0, second=0, microsecond=0)
                        _next8_ts = _next8.timestamp()
                        if hasattr(self.core, "propose_agenda"):
                            self.core.propose_agenda(
                                int(gid), f"梦境分享：{_share}",
                                priority=0.35, not_before_at=_next8_ts,
                                expires_at=_next8_ts + 3600,
                                source="morning_dream_share",
                            )
                            logger.info("[梦境] 梦境分享已入 agenda group=%s not_before=次日8点 mood_tag=%s",
                                        gid, dream.get("mood_tag"))
                        else:
                            # 无 agenda 能力时 fail-closed：静默跳过，不改生成本身
                            logger.debug("[梦境] propose_agenda 不可用，跳过梦境分享")
                        # FIX-1：梦境情绪回灌统一入口（soul 能量 + mood 底色一起）
                        eng.apply_dream_sweep(dream.get("mood_tag", "calm"))
                        self._last_morning_dream_date = _bj_date_str(now)
                        self.persist()
                        return
        except Exception:
            logger.debug("早晨梦境分享失败（已隔离）", exc_info=True)

        # 选择发言目标群（已含沉默门控 + 冷却 + 预算 + 占比）
        group_id = self._pick_group(now)
        if group_id is None:
            self.persist()
            return

        # 立场传播（danger.stance_propagation）：复用已通过门控的群，由高强度
        # 外向欲望触发主动发言。成功则更新冷却/预算并优先于随机接话返回。
        dcfg = getattr(self.core.config, "danger", None)
        if dcfg and getattr(dcfg, "stance_propagation", False) \
                and getattr(dcfg, "stance_propagation_confirm", False):
            try:
                from anima.cognitive.danger import get_danger_behaviors
                danger = get_danger_behaviors(core=self.core, cfg=dcfg)
                if danger is not None:
                    spoke = await danger.maybe_stance_propagation(group_id)
                    if spoke:
                        self._group_last_speak[group_id] = now
                        self._last_active_speak = now
                        self._group_speak_count[group_id] = self._group_speak_count.get(group_id, 0) + 1
                        self.persist()
                        return
            except Exception:
                logger.exception("立场传播调度失败")

        # 全局冷却：限制主动发言整体频率，防止多群同时刷屏
        if now - self._last_active_speak < self.active_global_cooldown_sec:
            self.persist()
            return

        # 主动发言概率门控（用户要求 0.1）：
        # 通过沉默门控的群，每个 eligible tick 仅以 active_reply_prob 概率真正
        # 开口；其余 tick 做轻量内省（不产生回复），保持「活的」但不打扰。
        if random.random() >= self.active_reply_prob:
            try:
                from anima.cognitive.simulated_life import get_life
                await get_life(self.core).flow_thought()
            except Exception:
                logger.exception("心跳 think 异常")
            self.persist()
            return

        # 主动“可接可不接”：无真实接话点时不开口（省一次 LLM、不刷“尝试即失败”账本）。
        has_hook = self._active_hook_present(group_id, now)
        subject = getattr(self.core, "subject", None)
        proactive_intent = None
        # T1.1: Constitution Drive 门（默认关，fail-closed）——主动表达须源于趋近 Drive 才开口，可 trace。
        drive_origin = None
        # 注：情感喂料已在 `_tick` 开头无条件执行（见那里的说明）。
        # 这里只做闸门判定 —— 职责分离：喂料归 tick，闸门归主动发言路径。
        if getattr(self.core.config.autonomy, "drive_gate", False):
            drive_origin = self._constitution_drive_snapshot(group_id)
            if drive_origin and not _drive_gate_ok(drive_origin):
                logger.info(
                    "[宪法Drive] 群%s 无趋近驱动(activated=%s approach=%s avoid=%s "
                    "mem=%s traits=%s)，压制主动说话",
                    group_id, drive_origin["activated"], drive_origin["approach"],
                    drive_origin["avoid"], drive_origin.get("memory_source", "?"),
                    drive_origin.get("identity_traits", "?"),
                )
                has_hook = False
        if subject is not None and has_hook:
            try:
                proposed = subject.propose_expression(
                    group_id, "主动接话", source="heartbeat",
                    priority=_pf("heartbeat", _pro_cfg),
                    expires_at=now + 3600,
                )
                proactive_intent = subject.ledger.claim(proposed.id, now)
                if proactive_intent:
                    subject.ledger.mark_dispatched(proactive_intent.id, "主动接话出站中")
                if drive_origin:
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        try:
                            audit.emit(
                                "proactive_expression_drive", group_id=group_id, source="heartbeat",
                                activated=bool(drive_origin["activated"]),
                                approach=round(drive_origin["approach"], 3),
                                avoid=round(drive_origin["avoid"], 3),
                                intensity=round(drive_origin.get("intensity", 0.0), 3),
                                # 2026-09-18（B1）：把链首来源与可达 trait 数一并留痕，
                                # 使"接线是否真的接上"可审计（D-13 未闭合时 traits 恒 0）。
                                memory_source=drive_origin.get("memory_source", "?"),
                                identity_traits=int(drive_origin.get("identity_traits", 0) or 0),
                                record_counts=drive_origin.get("record_counts", {}),
                            )
                        except Exception:
                            logger.exception("Drive trace 审计失败（已隔离）")
            except Exception:
                logger.exception("主动发言意图登记失败")
                proactive_intent = None

        # 多 Bot 社交：每日最多一次「主动找其他 Bot 说话」意图（rivalry>0.3 才触发，config 开启）
        # 仅入议程，最终是否出站仍受 Agenda 冷却/预算/发送确认约束，不会绕过闸门。
        try:
            scfg = getattr(self.core.config, "social", None)
            if scfg and getattr(scfg, "bot_talk_enabled", False):
                from anima.cognitive import bot_social
                _bsd = str(self.core.config.persistence.data_dir)
                _target = bot_social.pick_bot_talk_target(_bsd, now)
                if _target and subject is not None:
                    _intent_text = (
                        f"在群里和 {_target['name']} 聊两句（它是另一个 Bot，你们竞争度较高，"
                        f"可以带刺或调侃，但别真的人身攻击，也别刷屏）。"
                    )
                    subject.propose_expression(
                        group_id, _intent_text, source="bot_social",
                        priority=_pf("bot_social", _pro_cfg),
                        expires_at=now + 3600,
                    )
                    logger.info("[多Bot] 今日主动找 Bot %s 说话已入议程", _target["name"])
        except Exception:
            logger.exception("多 Bot 主动意图失败（已隔离）")

        spoke = await self._speak(group_id, now) if has_hook else False
        if subject and proactive_intent:
            if spoke:
                subject.ledger.complete(proactive_intent.id, "主动接话出站成功")
            else:
                max_attempts = int(getattr(self, "active_intent_max_attempts", 5) or 5)
                subject.ledger.release(
                    proactive_intent.id, "主动接话未满足出站条件", max_attempts=max_attempts,
                )
        # 2026-09-18（D-13 验收 (a)）：真实 producer —— 只在**真的发起过**一次承载
        # （claim 到了 proactive_intent）时把承载结果沉淀进宪法记忆；开关默认关。
        if proactive_intent is not None and bool(getattr(
            getattr(getattr(self.core, "config", None), "autonomy", None),
            "constitution_reflection_loop", False,
        )):
            try:
                self._constitution_sediment_carrying(group_id, spoke)
            except Exception:
                logger.exception("宪法反思沉淀失败（已隔离）")
        self.persist()

    # ------------------------------------------------------------------
    # 频率闸门
    # ------------------------------------------------------------------

    def _bj_date_str(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, _BJ).strftime("%Y-%m-%d")

    async def _bot_discovery_tick(self, now: float) -> bool:
        """每日至多一次：扫描群成员发现其他 bot。返回本次是否调度。

        让 `bot_social.scan_group_bots` 真正接进生产调用链（此前它是零调用点的孤儿）。
        边界（不可混淆）：
        - 平台 `role=='bot'` ⇒ 自动确认（平台已标记，安全）
        - 昵称命中 ⇒ **只记建议日志**，绝不写入 known_bots.json
        2026-10-14 实测 NapCat 不标 role ⇒ 自动确认当前恒空，收益主要来自建议日志。
        """
        # 🔴 开关在 **social 段**（与 bot_talk_enabled 同族），不在 qq 段。
        #    2026-10-14 曾误挂 qq 段（那里没有这个字段 ⇒ 恒 False ⇒ 哑开关）。
        _scfg = getattr(self.core.config, "social", None)
        if not (_scfg and getattr(_scfg, "bot_discovery_enabled", False)):
            return False
        today = self._bj_date_str(now)
        if getattr(self, "_last_bot_discovery_date", "") == today:
            return False
        self._last_bot_discovery_date = today
        self.persist()
        try:
            from anima.cognitive.bot_social import scan_group_bots

            _launch_bg(scan_group_bots(self.core), "bot_discovery")
            logger.info("[多Bot发现] 每日扫描已调度")
            return True
        except Exception:  # noqa: BLE001
            logger.exception("bot 发现扫描调度失败（已隔离）")
            return False

    def _maybe_morning_dream_share(self, now: float) -> bool:
        """A3：仅 08:00、每天至多一次、10% 概率主动分享梦境。返回是否本次应当分享。"""
        today = self._bj_date_str(now)
        last = getattr(self, "_last_morning_dream_date", "")
        should_share, attempted = _decide_morning_dream_share(datetime.now(_BJ).hour, last, today)
        if attempted and not should_share:
            # 抽不中：标记今天已尝试，避免每 tick 重复抽
            self._last_morning_dream_date = today
        return should_share

    def _circadian_gate(self) -> bool:
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        hour = datetime.now(tz).hour
        # 硬静默：凌晨 1-5 点绝不主动发言（避免深夜骚扰）
        if hour in (1, 2, 3, 4, 5):
            return False
        # P3-3: quiet_hours 时段同样抑制主动发言（用户可配置免打扰窗口，独立于睡眠模式）
        if self._in_quiet_hours():
            return False
        base = HOURLY_ACTIVITY.get(hour, 0.5)
        jitter = random.uniform(0.5, 1.5)
        prob = base * jitter
        # H3: 情绪驱动主动社交权重
        try:
            from anima.cognitive.mood import get_mood_system
            data_dir = str(self.core.config.persistence.data_dir)
            mood_label = get_mood_system(data_dir).get_state().label
            if mood_label in ("excited", "happy"):
                prob *= 1.3
            elif mood_label in ("tired", "bored"):
                prob *= 0.5
        except Exception:
            pass
        return random.random() < prob

    def _in_quiet_hours(self, now=None) -> bool:
        """P3-3: 是否处于用户可配置的 quiet_hours 免打扰窗口（主动发言抑制用）。

        读取 config.autonomy.quiet_hours_*（enabled/start_hour/end_hour，默认 23-7 跨午夜）。
        quiet_hours 关闭、配置缺失或读取异常一律返回 False（不擅自压制主动）。
        ``now`` 可注入（需带时区 datetime）便于测试。
        """
        try:
            acfg = self.core.config.autonomy
            if not getattr(acfg, "quiet_hours_enabled", False):
                return False
            from anima.cognitive.sleep_state import is_sleep_time
            start = int(getattr(acfg, "quiet_hours_start_hour", 23) or 23)
            end = int(getattr(acfg, "quiet_hours_end_hour", 7) or 7)
            return is_sleep_time(
                now if now is not None else datetime.now(_BJ),
                start_hour=start, end_hour=end,
            )
        except Exception:  # noqa: BLE001
            return False

    def _pick_group(self, now: float) -> Optional[int]:
        """选择一个符合冷却和预算条件的群。"""
        from anima.memory.chat_memory import get_chat_memory
        memory = get_chat_memory()

        # 优先选有活跃对话的群
        candidates = []
        gids: set[int] = set(memory.group_recent_ids())
        for key in list(memory._store.keys()):
            if not key.startswith("group_"):
                continue
            # 修正（P0-1）：key 形如 group_{gid}_{uid}，若用
            # int(key.split("_", 1)[1]) 会得到 "gid_uid"，而 Python 允许整数字面量
            # 带下划线（int("727_100") == 727100），会把群号错误地放大成巨大数，
            # 导致主动发言发到错误/不存在的群。改为全拆分取第二段（parts[1]）。
            parts = key.split("_")
            try:
                gid = int(parts[1])
            except (ValueError, IndexError):
                continue

            gids.add(gid)

        for gid in gids:
            core = getattr(self, "core", None)
            permissions = getattr(getattr(getattr(core, "config", None), "permissions", None), "group_policies", {})
            policy = permissions.get(str(gid), {}) if isinstance(permissions, dict) else {}
            if isinstance(policy, dict) and policy.get("autonomy") is False:
                continue
            feedback_gate = getattr(self, "_feedback_allows_group", None)
            if callable(feedback_gate) and not feedback_gate(gid):
                continue
            # 冷却检查
            last = self._group_last_speak.get(gid, 0)
            if now - last < self.cooldown_sec:
                continue

            # 沉默门控：仅当该群曾有过人类发言，且最近一次发言距今
            # ≥ active_silence_min_sec，才考虑主动插话。这样 bot 会在
            # 「对话冷场后」自然地接一句，而不是在聊得正嗨时硬插。
            last_msg = self._group_last_msg.get(gid, 0)
            if last_msg <= 0:
                continue
            if now - last_msg < self.active_silence_min_sec:
                continue

            # 日预算
            count = self._group_speak_count.get(gid, 0)
            active_daily_max = getattr(self, "active_daily_max_per_group", self.daily_max)
            if count >= self.daily_max or count >= active_daily_max:
                continue

            # 占比检查 + 群成熟度：日内计数经日切会清空，回退群在场累计，
            # 避免重启/跨日后「明明是老群却永远凑不够 min_group_messages」。
            total = self._group_total_msgs.get(gid, 0)
            min_group_messages = getattr(self, "active_min_group_messages", 0)
            established = total
            if established < min_group_messages and core is not None:
                try:
                    presence = getattr(getattr(core, "group_presence", None), "items", {}) or {}
                    item = presence.get(int(gid))
                    established = max(established, int(getattr(item, "message_count", 0) or 0))
                except Exception:
                    pass
            if established < min_group_messages:
                continue
            if total > 0 and count / max(total, 1) > self.ratio_max:
                continue

            candidates.append(gid)

        if not candidates:
            return None

        return random.choice(candidates)

    def _group_only_avoided_speakers(self, group_id: int, now: float) -> bool:
        """群内近期人类发言者若全部是被回避用户，则跳过主动发言。

        避免 bot 在 24h 回避期内还去「关心 / care」曾经攻击自己的人。
        """
        try:
            data_dir = str(self.core.config.persistence.data_dir)
            from anima.memory.chat_memory import get_chat_memory
            from anima.cognitive.scar import get_scar_system
            cm = get_chat_memory()
            scar = get_scar_system(data_dir)
            spoken_uids: set[int] = set()
            for key in list(cm._store.keys()):
                if not key.startswith(f"group_{group_id}_"):
                    continue
                parts = key.split("_")
                if len(parts) < 3:
                    continue
                try:
                    uid = int(parts[2])
                except (ValueError, IndexError):
                    continue
                hist = cm.get_context("group", group_id, recent_rounds=3, user_id=uid)
                if any(m.get("role") == "user" for m in hist):
                    spoken_uids.add(uid)
            if not spoken_uids:
                return False
            return all(scar.is_avoiding(uid, now) for uid in spoken_uids)
        except Exception:
            logger.debug("回避发言检查失败", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 发言
    # ------------------------------------------------------------------

    def _constitution_drive_snapshot(self, group_id: int):
        """汇集群级 today 信号 → 轻量宪法桥 → 带 gate_ok 的 Drive，供主动表达 trace。失败返回 None。

        2026-09-18（B1）：链首改为读**真实水合记忆** ``core.memory_authority.snapshot_memory()``；
        取不到时才退回合成的替身（并在 ``memory_source`` 里如实标注）。mood 标量仍作为
        Perception(novelty/avoid) 输入——它们本来就是"此刻感知"，不是记忆。
        """
        data_dir = str(getattr(self.core.config.persistence, "data_dir", "data"))
        arousal, valence = 0.2, 0.1
        try:
            from anima.cognitive.mood import get_mood_system
            _m = get_mood_system(data_dir).get_state()
            arousal = float(getattr(_m, "arousal", arousal))
            valence = float(getattr(_m, "valence", valence))
        except Exception:
            logger.debug("Drive 桥 mood 读取失败，用默认", exc_info=True)
        threat = max(0.0, -valence)
        memory = None
        try:
            _auth = getattr(self.core, "memory_authority", None)
            if _auth is not None:
                # 🔴 2026-10-23：**情感 → 宪法记忆桥接**（把断掉的边接上）。
                #   生产实测：宪法 Memory 全空（episodes/semantic/relations 全 0）
                #   ⇒ identity.traits={} ⇒ need.urgency=0 ⇒ drive.activated 恒 False
                #   ⇒ 它从来没有任何「内在驱动力」可以驱动行动。
                #   而真实情感数据是充分的（mood/desires 10 条/relation 93 条）。
                #   本桥接**只读**这些数据、镜像成 semantic/relation 记录，
                #   绝不写回任何情感文件 ⇒ 人格数据所有权不变。
                #   开关默认关（`autonomy.emotion_bridge_enabled`）：关闭时行为
                #   与改动前完全一致。
                try:
                    if bool(getattr(getattr(self.core.config, "autonomy", None),
                                    "emotion_bridge_enabled", False)):
                        from anima.constitution.emotion_bridge import hydrate_emotion_memory
                        hydrate_emotion_memory(
                            _auth.engine,
                            getattr(getattr(self.core.config, "persistence", None),
                                    "data_dir", "data"),
                            core=self.core,
                        )
                except Exception:
                    logger.debug("情感→宪法记忆桥接失败（已隔离）", exc_info=True)
                memory = _auth.snapshot_memory()
        except Exception:
            memory = None
            logger.debug("Drive 桥 memory_authority 读取失败，退回合成替身", exc_info=True)
        d = _constitution_drive(arousal, valence, 0.2, threat, memory=memory)
        if d:
            d["gate_ok"] = _drive_gate_ok(d)
        return d

    def _constitution_sediment_carrying(self, group_id: int, spoke: bool) -> None:
        """D-13 验收 (a) 的生产 producer：把一次**真实**主动承载的结果沉淀进宪法记忆。

        `ExecutionResult → reflect() → MemoryAuthority.ingest()`（RFC-0107/0109/0108）。
        此前 ``MemoryAuthority.ingest`` 全库无生产调用者，闭环只活在测试里（台账 D-13）。

        三条纪律：
        - **不伪造经验**：输入是真实发生的承载结果（``spoke`` 真假），不是现场合成的常量
          替身 —— 与 B1 从 ``_constitution_drive`` 摘掉替身 Memory 是同一条纪律；
        - **不落盘**：只写进程内 ``MemoryEngine``，不新建 data/ 文件、不驱动 F5 runtime（D-14）；
        - **失败隔离**：沉淀失败绝不回滚或影响已经完成的出站。

        ⚠️ 该闭环不产生数值 trait（``reflect()`` 只产文本 lesson）⇒ ``identity_traits`` 仍为 0，
        不可据此翻转 ``drive_gate``。
        """
        _auth = getattr(self.core, "memory_authority", None)
        if _auth is None:
            return
        from anima.constitution.reflection_loop import ReflectionSedimenter
        _s = getattr(self, "_reflection_sedimenter", None)
        if _s is None or _s.authority is not _auth:
            _s = ReflectionSedimenter(_auth)
            self._reflection_sedimenter = _s
        _r = _s.sediment(
            plan_ref=f"heartbeat#{group_id}",
            succeeded=bool(spoke),
            outcome=("主动接话出站成功" if spoke else "主动接话未满足出站条件"),
        )
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit(
                "constitution_reflection_sedimented",
                group_id=group_id, status=_r["status"], records=_r["records"],
                identity_traits=_r["identity_traits"], evidence_ref=_r["evidence_ref"],
            )

    def _active_hook_present(self, group_id: int, now: float) -> bool:
        """是否存在值得主动接住的近期群语接话点（短时上下文评分 >= 0.35，2026-08-28 收紧）。"""
        try:
            from anima.memory.chat_memory import get_chat_memory
            memory = get_chat_memory()
            recent_context = getattr(memory, "get_recent_group_context", None)
            history = recent_context(group_id, limit=12) if callable(recent_context) else []
            if not history:
                return False
            return score_active_context(history) >= 0.35
        except Exception:
            # 评分失败保守放行：回退给原模型 [skip] 判断，不削弱主动发言。
            return True

    async def _speak(self, group_id: int, now: float, intent: str = "") -> bool:
        """主动发言入口（M3 防 TOCTOU）：锁内串行化 token 预算检查→LLM→扣账，
        消除并发检查-花费竞态，且保留"成功发送才扣账"的账本语义。"""
        lock = getattr(self, "_speech_lock", None)
        if lock is None:
            lock = self._speech_lock = asyncio.Lock()  # 防御式：测试桩/未构造实例也安全
        async with lock:
            return await Heartbeat._speak_locked(self, group_id, now, intent)

    async def _speak_locked(self, group_id: int, now: float, intent: str = "") -> bool:
        """生成并发送主动发言。"""
        if self.core.llm is None:
            return False

        # 自我保护：被伤过两次的用户，24h 内不主动 care / 接话
        try:
            if not intent and self._group_only_avoided_speakers(group_id, now):
                logger.info("[回避] 群 %d 近期发言者均被回避，跳过主动发言", group_id)
                return False
        except Exception:
            logger.debug("回避门控异常", exc_info=True)

        from anima.cognitive.time_sense import time_description
        from anima.cognitive.desire import get_desire_system
        from anima.memory.chat_memory import get_chat_memory
        from anima.core.reply_splitter import filter_inner_monologue

        desire = get_desire_system()
        memory = get_chat_memory()
        # 主动接话只看 15 分钟 TTL 的群短时缓冲，避免拿持久化旧对话突然续聊。
        # 明确议程/提醒可以在没有近期上下文时照常发送。
        recent_context = getattr(memory, "get_recent_group_context", None)
        history = recent_context(group_id, limit=12) if callable(recent_context) else []
        topic = None  # A1（队列③）：欲望选题结果；仅无议程路径会被覆盖为真实选题
        if not intent:
            threshold = float(getattr(self, "active_context_score_threshold", 0.0) or 0.0)
            context_score = score_active_context(history)
            # N2: 话题兴趣匹配（群聊话题命中活跃欲望时 +0.15）
            try:
                from anima.cognitive.desire import get_desire_system
                desire_sys = get_desire_system()
                active_desires = desire_sys.get_active(0.3)
                if active_desires and history:
                    # 检查最近消息是否命中欲望关键词
                    recent_text = " ".join(
                        str(m.get("content", "")) for m in history[-5:]
                    ).lower()
                    for d in active_desires[:3]:
                        # 简单关键词匹配
                        keywords = ["吃", "美食", "饭", "游戏", "动漫", "旅行", "玩"]
                        if any(kw in recent_text and kw in d.content for kw in keywords):
                            context_score += 0.15
                            logger.debug("话题兴趣匹配：欲望'%s'命中，+0.15", d.content[:20])
                            break
            except Exception:
                pass
            # 沉默已过门控但缓冲偏空时略降阈值，避免「想说却永远出站失败」卡死意图。
            if not history:
                logger.debug("主动发言跳过：群 %d 无短时群上下文", group_id)
                return False
            effective_threshold = threshold
            last_msg_at = float(self._group_last_msg.get(group_id, 0) or 0)
            silence = now - last_msg_at if last_msg_at > 0 else 0.0
            if silence >= float(self.active_silence_min_sec) * 1.5:
                effective_threshold = max(0.28, threshold - 0.12)
            if context_score < effective_threshold:
                logger.debug(
                    "主动发言跳过：群 %d 上下文评分 %.2f < %.2f (silence=%.0fs)",
                    group_id, context_score, effective_threshold, silence,
                )
                return False

            # A1 欲望深化（队列③）：最强欲望优先选题；无欲望退回普通接话。
            topic = None
            try:
                topic = _select_desire_topic(
                    desire, group_id, history, now,
                    min_intensity=self.desire_speak_min_intensity,
                    freshness_window_sec=self.desire_freshness_window_sec,
                    event_match_bonus=self.desire_event_match_bonus,
                    repeat_cooldown_sec=self.desire_repeat_cooldown_sec,
                    expressed=self._recent_expressed_desires.get(group_id, []),
                )
            except Exception:
                logger.debug("欲望选题失败（退回普通接话）", exc_info=True)

        # 构建自主发言 prompt（P0-2：必须与哈基米人格一致，禁止「你是 Anima…AI」）
        time_ctx = time_description()
        desire_ctx = desire.prompt_fragment(group_id)
        # 2026-09-22：补齐内容类人格片段（自我笔记 + 当前情绪 + 世界观），与被动回复对齐。
        self_notes_ctx, mood_note_ctx, world_ctx = _active_speech_persona_fragments(self.core)

        if intent:
            speaking_rules = """你现在正在执行一项已经通过系统门控的明确表达议程，不是在随机找话题。
- 用自然、日常的语气说 1-2 句话，就像平时聊天一样
- 不要复读议程原文，不要复述指令，不要照抄之前的回复
- 如果议程内容是之前说过的话，用平和、随意的语气自然带过，不要重复激烈情绪或威胁性内容
- 即使群里暂时安静，也要完成这次表达
- 不要输出「[skip]」，不要解释内部流程
- 不要用「作为 AI」开头，也别自称 Anima / Antigravity
- 只输出要发到群里的正文；绝不输出说话人标签或引用标记
- 如果议程内容本身含乱码或无法理解的内容，直接输出 [skip]，不要编造解释"""
            # 🔴 2026-10-18（D-34）：这里**曾经**追加「你最近在这个群说过的话」防自相矛盾。
            # 已撤回：一是它同样把**原句**贴进 prompt（与 cognition 的 `self_recent_lines`
            # 同病 —— U2 定律：给原句 = 给模板）；二是矛盾的真病灶在 `COMMITMENT_RE`
            # 把「本喵才不帮你看」判成承诺 ⇒ 议程 `text_intent` 里塞的就是那句拒绝，
            # 模型只能把"没帮"改写成"帮了"。病根修掉后（constants.D-35 + 清存量），
            # 跟进输入里不再有拒绝原文，此处无需再喂历史。
            user_request = f"（明确议程）请用自然、日常的语气完成这项表达：\n{intent[:500]}"
        else:
            speaking_rules = """你现在可以考虑在群里说一句话，但「不打扰」优先于开口。
开口前先自问一句：现在这句是不是真的非说不可、确实接得住？不是就闭嘴。
只有最近对话有明确、还没接住的梗、问题、情绪或自然收尾时才说。
- 必须具体接住最近一两句人类内容（可点名梗/问题），禁止空转刷存在感
- 禁止：感叹群里安静、骂乱码/问号、梦游键盘、催人买罐头、喊名字没下文类水话
- **短**：群里真人平均 14 个字。你最多 25 字，超过就是不像人。
- **一次只说一件事**：不要连环追问（「你该不会…吧？快说…你到底…」一次问三句）
- **不要表演**：不用「金口玉言 / 说到底 / 话说回来」这类书面语，
  不自夸（本大爷/本喵多厉害），不铺垫。
- 语气软：可以困、好奇、撒娇、小脾气；不居高临下、不训人。
- 不要用「作为 AI」开头，也别自称 Anima / Antigravity
- 只输出正文；绝不输出「[你]:」「[哈基米]:」等说话人标签，
  也绝不输出「当前表达倾向：」「当前内心状态：」这类内部推理
- 拿不准、上下文太旧、没有明确接话点、觉得不值当开口时，一律只输出「[skip]」

参考（真人怎么说话）：
  好：「啊？」「我也想看喵」「笑死，那也太惨了」「猪就猪吧，反正可爱」
  不好：「哼，本大爷金口玉言，说出去的话当然算数喵！不过话说回来…」（又长又演）
  不好：「注意到您辛苦啦，记得多喝水哦」（客服腔，不是猫）"""
            # 把最近人类句子摘要进请求，降低“瞎接旧话题/空转”概率。
            hooks = []
            for message in reversed(history or []):
                if message.get("role") != "user":
                    continue
                content = str(message.get("content") or "").strip()
                if not content or _NOISE_ONLY_RE.match(content):
                    continue
                name = str(message.get("name") or "群友")
                hooks.append(f"{name}: {content[:80]}")
                if len(hooks) >= 3:
                    break
            hooks_text = "\n".join(reversed(hooks)) if hooks else "(无有效人类短句)"
            if topic is not None:
                logger.info(
                    "[主动发言] 欲望驱动选题 group=%d：%s（强度 %.2f）",
                    group_id, topic.content[:40], topic.intensity,
                )
                user_request = (
                    f"（主动发言）你心里正惦记着一件事：{topic.content}。"
                    "结合下面的最近人类消息找一个自然的开口点，把这桩心事说 1 句；"
                    "若和现场完全不搭，宁可输出 [skip]。\n"
                    f"最近人类消息：\n{hooks_text}"
                )
            else:
                user_request = (
                    "（主动发言）若值得开口，请只接下面最近人类内容说 1 句；"
                    f"否则输出 [skip]。\n最近人类消息：\n{hooks_text}"
                )

        system = _build_active_speech_system(
            time_ctx, desire_ctx, speaking_rules,
            self_notes=self_notes_ctx, mood_note=mood_note_ctx,
            world_context=world_ctx,
        )

        messages = [{"role": "system", "content": system}]
        # 明确议程是 Owner/日历已经确认的表达目标。近期群聊曾让模型转而接
        # “群里很安静”等现场话题，导致议程被标为 consumed 却没有真正表达。
        # 随机主动接话仍使用短时上下文；明确议程只保留人格和议程本身。
        if history and not intent:
            messages.extend(history)
        messages.append({"role": "user", "content": user_request})

        # Token 预算检查（粗略估算：1 token ≈ 4 字符）
        max_tokens = 200
        estimated_tokens = sum(len(m.get("content", "")) for m in messages) // 4 + max_tokens
        if self.token_budget and self._tokens_today + estimated_tokens > self.token_budget:
            logger.warning("Token 预算不足，跳过自主发言")
            return False

        try:
            from anima.llm.usage import llm_scope
            with llm_scope("active_speech", "active_speech"):
                reply = await self.core.llm.chat(messages, temperature=0.85, max_tokens=200)
        except Exception:
            self._consecutive_failures += 1
            self._check_degrade()
            return False

        reply = _clean_active_reply(reply)
        # P0 纵深防御：主动发言同样过滤内心独白（_speak 不经 generation 的过滤）
        reply = filter_inner_monologue(reply)
        # 🔴 r28 N1：这三个出口此前是裸 return，「触发了但没发」在 journal 里
        # 完全不可见（10-06 三次未发的主动发言只剩一行人格片段日志）。
        # 原因码必须落盘，否则「配额烧完了」和「模型不想说话」无法区分。
        _drop_reason = _active_reply_drop_reason(reply)
        if _drop_reason:
            logger.info(
                "自主发言丢弃：reason=%s group=%d reply_len=%d reply=%s",
                _drop_reason, group_id, len(reply or ""), (reply or "")[:60],
            )
            return False
        if _is_low_value_active_reply(reply):
            logger.info("自主发言丢弃低质水话 group=%d text_len=%d", group_id, len(reply))
            return False
        if len(reply) > 120:
            reply = reply[:120].rstrip("，,。；;、 ") + "…"

        # ── 去重 Layer 1：与群内近期所有 bot 消息比对（含被动回复）──
        # 防止自主发言照搬刚刚被动回复说过的话（12:05 事件的根因）。
        _BOT_LABEL_RE = re.compile(r"^\[哈基米\]\s*[:：]?\s*")
        bot_msgs_recent = [
            _BOT_LABEL_RE.sub("", str(m.get("content", ""))).strip()
            for m in (history or [])
            if m.get("role") == "assistant"
        ]
        current = _normalized_active_reply(reply)
        if len(current) >= 10 and any(
            SequenceMatcher(None, current, _normalized_active_reply(bm)).ratio() >= 0.70
            for bm in bot_msgs_recent
            if len(_normalized_active_reply(bm)) >= 10
        ):
            logger.info("自主发言丢弃：与群内近期 bot 回复过于相似 group=%d", group_id)
            return False

        # ── 去重 Layer 2：与本群近期自主发言比对（4h 窗口，跨重启持久化）──
        # 0.76 能挡住同主题的换词复读，又不会干扰普通短句或正常的后续对话。
        recent_by_group = getattr(self, "_recent_active_replies", {})
        recent = [item for item in recent_by_group.get(group_id, []) if now - item[0] < 14400]
        recent_by_group[group_id] = recent
        if len(current) >= 12 and any(
            SequenceMatcher(None, current, _normalized_active_reply(previous)).ratio() >= 0.76
            for _, previous in recent
        ):
            logger.info("自主发言去重：群 %d 的候选回复与近期自主内容过于相似", group_id)
            return False

        # ── 接地网关：主动发言含排障/索要话术一律丢弃（体验测试 B1）──
        _autonomy_cfg = getattr(getattr(self.core, "config", None), "autonomy", None)
        if (getattr(_autonomy_cfg, "proactive_grounding", True)
                and _is_ungrounded_proactive(reply)):
            logger.warning("[接地网关] 主动发言含未接地排障/索要话术，丢弃 group=%d", group_id)
            return False

        try:
            message_id = await self.core.gateway.send(OutboundMsg("group", group_id, reply))
        except Exception:
            self._consecutive_failures += 1
            self._check_degrade()
            logger.exception("自主发言出站异常 group=%d", group_id)
            return False
        if message_id is None:
            self._consecutive_failures += 1
            self._check_degrade()
            logger.warning("自主发言出站失败 group=%d", group_id)
            return False

        # 只有 OneBot 返回成功后才消耗预算、更新冷却和写入群记忆。
        self._consecutive_failures = 0
        self._tokens_today += estimated_tokens
        self._group_last_speak[group_id] = now
        self._last_active_speak = now
        self._group_speak_count[group_id] = self._group_speak_count.get(group_id, 0) + 1
        recent.append((now, reply))
        recent_by_group[group_id] = recent[-4:]
        self._recent_active_replies = recent_by_group
        # A1（队列③）：表达成功才记录冷却 + 释放欲望强度
        if topic is not None:
            expressed_list = self._recent_expressed_desires.setdefault(group_id, [])
            expressed_list.append((topic.content, now))
            self._recent_expressed_desires[group_id] = expressed_list[-20:]
            try:
                desire.mark_expressed(topic)
            except Exception:
                logger.debug("欲望表达释放失败（忽略）", exc_info=True)
        feedback_pending = getattr(self, "_active_feedback_pending", None)
        if feedback_pending is None:
            feedback_pending = self._active_feedback_pending = {}
        feedback_pending[group_id] = {
            "sent_at": now,
            "message_id": str(message_id),
            "source": "agenda" if intent else "heartbeat",
        }
        logger.info("自主发言成功 group=%d text_len=%d", group_id, len(reply))

        # 记录到记忆：主动发言是群级可见的，只写入短时群上下文（15min TTL），
        # 不广播进每个用户的个人持久桶——个人桶只应存一对一交互，避免跨用户污染与 N 倍存储冗余。
        memory.observe_group_bot_reply(group_id, reply)
        return True

    # ------------------------------------------------------------------
    # 消息计数（由外部调用，每收到一条群消息 +1）
    # ------------------------------------------------------------------

    def count_message(self, group_id: int) -> None:
        """每收到一条群消息调用：累加计数并记录最近发言时间（供沉默门控）。"""
        self._record_active_engagement(int(group_id), time.time())
        self._observe_reply_feedback(int(group_id))
        self._group_total_msgs[group_id] = self._group_total_msgs.get(group_id, 0) + 1
        self._group_last_msg[group_id] = time.time()

    def _observe_reply_feedback(self, group_id: int, now: float | None = None) -> None:
        """被动回复的「被接话」观察点。

        与 `_record_active_engagement` 共用同一个触发源（memory 层的群消息回调，
        天然在回复闸门**之前**），但落在**独立的池子**（`reply_learning.json`）——
        主动发言与被动回复的开口效果必须分开统计，否则归因又会糊在一起。
        """
        try:
            from anima.cognitive.reply_learning import get_reply_learning
            get_reply_learning(str(self._data_dir)).observe(int(group_id), now=now)
        except Exception:
            logger.debug("被动回复效果观察失败（已隔离）", exc_info=True)

    def _expire_reply_feedback(self) -> None:
        """心跳驱动的窗口结算：窗口过了仍无人接话 ⇒ ignored。"""
        try:
            from anima.cognitive.reply_learning import get_reply_learning
            get_reply_learning(str(self._data_dir)).expire()
        except Exception:
            logger.debug("被动回复效果过期结算失败（已隔离）", exc_info=True)

    def _feedback_stats(self, group_id: int) -> dict[str, int]:
        return self._active_feedback_stats.setdefault(int(group_id), {"engaged": 0, "ignored": 0})

    def _record_active_engagement(self, group_id: int, now: float) -> None:
        pending = self._active_feedback_pending.get(int(group_id))
        if not pending:
            return
        sent_at = float(pending.get("sent_at", 0) or 0)
        if sent_at <= 0 or now - sent_at > self.active_feedback_window_sec:
            return
        stats = self._feedback_stats(group_id)
        stats["engaged"] += 1
        self._active_feedback_pending.pop(int(group_id), None)
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("active_speech_feedback", group_id=int(group_id), outcome="engaged",
                       source=str(pending.get("source") or ""))

    def _expire_active_feedback(self, now: float) -> None:
        expired = []
        for group_id, pending in self._active_feedback_pending.items():
            sent_at = float(pending.get("sent_at", 0) or 0)
            if sent_at > 0 and now - sent_at >= self.active_feedback_window_sec:
                expired.append((group_id, pending))
        for group_id, pending in expired:
            self._feedback_stats(group_id)["ignored"] += 1
            self._active_feedback_pending.pop(group_id, None)
            audit = getattr(self.core, "audit", None)
            if audit:
                audit.emit("active_speech_feedback", group_id=int(group_id), outcome="ignored",
                           source=str(pending.get("source") or ""))
            # S3 冷落信号（蓝图）：真状态计数达阈值 → mood 短暂温和低落；
            # 不扣 relation 信任、不记 scar。计数仍来自 feedback stats 本体。
            try:
                from anima.cognitive.inner_state.neglect import apply_neglect
                apply_neglect(self.core, group_id, now=now)
            except Exception:
                logger.exception("S3 冷落信号施加失败（已隔离）")

    def active_acceptance(self, group_id: int) -> dict[str, float | int | bool]:
        stats = self._feedback_stats(group_id)
        engaged = int(stats.get("engaged", 0) or 0)
        ignored = int(stats.get("ignored", 0) or 0)
        samples = engaged + ignored
        score = (engaged + 1) / (samples + 2)  # Beta(1,1) 平滑，避免零样本极端值。
        limited = samples >= self.active_feedback_min_samples and score < self.active_feedback_min_acceptance
        return {"engaged": engaged, "ignored": ignored, "samples": samples,
                "score": round(score, 3), "limited": limited}

    def _feedback_allows_group(self, group_id: int) -> bool:
        return not bool(self.active_acceptance(group_id)["limited"])

    # ------------------------------------------------------------------
    # 模拟生活节律
    # ------------------------------------------------------------------

    async def _life_checkpoints(self) -> None:
        """在每次心跳 tick 中检查模拟生活 + 反刍 + 遗忘 + 情绪。"""
        try:
            from anima.cognitive.simulated_life import get_life
            from anima.cognitive.forgetting import get_forgetting
            from anima.memory.chat_memory import get_chat_memory

            life = get_life(self.core)
            forgetting = get_forgetting(self.core.config.persistence.data_dir)
            memory = get_chat_memory()

            # 情绪系统更新（时间段影响 + 衰减）
            try:
                from anima.cognitive.mood import get_mood_system
                mood = get_mood_system(self.core.config.persistence.data_dir)
                # 每次心跳推进情绪时间：只让 30min 衰减**有机会执行**。
                # 🔴 不可改回 trigger("mentioned", 0.05)：trigger() 每次都会重置
                # last_updated，30s 一次的心跳会把衰减时钟永久压在 now 附近
                # ⇒ elapsed 永远 < 1800s ⇒ 衰减从未执行（2026-09-21 实证）。
                mood.touch_time()
            except Exception:
                logger.debug("情绪系统更新失败，已隔离")

            # 晨间觉醒
            await life.morning_routine()

            # 碎片心流
            await life.flow_thought()

            # 晚间反思
            await life.evening_reflection()

            # M2: 晚安行为
            await life.goodnight_behavior()

            # S3: 群活跃度影响欲望生成
            try:
                from anima.cognitive.group_presence import get_group_presence
                from anima.cognitive.desire import get_desire_system
                gp = get_group_presence(self.core.config.persistence.data_dir)
                desire_sys = get_desire_system(self.core.config.persistence.data_dir)
                # 统计总群活跃度
                total_msgs = sum(
                    p.message_count for p in gp.items.values()
                    if hasattr(p, "message_count")
                )
                if total_msgs >= 15:
                    # 群很活跃 → 社交欲上升
                    _d = desire_sys.add("想跟大家聊天", intensity=0.35, kind="chat")
                    logger.info("[欲望] 群活跃(%d 条) → 社交欲 add 返回 %s (intensity=%s)",
                                total_msgs,
                                "new" if _d is not None else "None",
                                getattr(_d, "intensity", "?"))
                elif total_msgs <= 2:
                    # 群冷清 → 独处欲/好奇心上升
                    _d = desire_sys.add("想自己探索点新东西", intensity=0.3, kind="chat")
                    logger.info("[欲望] 群冷清(%d 条) → 探索欲 add 返回 %s",
                                total_msgs, "new" if _d is not None else "None")
            except Exception:
                # 🔴 2026-10-23：原来用 debug ⇒ 失败完全不可见。
                #   生产实测：群 25257 条消息、条件满足，但欲望从未产生，
                #   而日志 0 条 —— 全部被这里吞掉。
                logger.warning("S3 群活跃度欲望生成失败", exc_info=True)

            # 反刍（将近期经历沉淀为自我笔记）
            if self._rumination:
                await self._rumination.maybe_ruminate()

            # 遗忘调度
            await forgetting.run(chat_memory=memory, vector_memory=self.core.vector_memory)

        except Exception:
            logger.exception("模拟生活 checkpoints 异常")

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------

    def _check_degrade(self) -> None:
        if self._consecutive_failures >= self._max_failures:
            self._degraded = True
            logger.warning("熔断: 连续 %d 次 LLM 失败，切换为纯应答模式", self._consecutive_failures)

    async def _check_recovery(self) -> None:
        """每 5 分钟尝试恢复。"""
        if self._consecutive_failures == 0:
            self._degraded = False
            logger.info("熔断恢复: 自主模式已重新启用")
            return

        # 尝试探测 LLM
        if self.core.llm:
            try:
                from anima.llm.usage import llm_scope
                with llm_scope("active_speech", "health_probe"):
                    probe = await self.core.llm.chat(
                        [{"role": "user", "content": "ping"}],
                        temperature=0, max_tokens=5
                    )
                if probe:
                    self._consecutive_failures = 0
                    self._degraded = False
                    logger.info("熔断恢复: LLM 探测成功")
            except Exception:
                self._consecutive_failures += 1
