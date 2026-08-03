"""阶段 5: 输出护栏。"""

from __future__ import annotations
import asyncio
import logging
import re
import time
from collections import deque

from anima.pipeline import StageResult, PipelineContext
from ._shared import (
    _bg_task_done,
    _clean_transport_text,
    _detect_hurtful,
    _derail_history,
    _DERAIL_HISTORY_MAX,
)
from .generation import _strip_speaker_labels

logger = logging.getLogger("anima.pipeline.stages")

# 危险的输出模式
DANGEROUS_PATTERNS = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "sk-",  # API keys
    "eyJ",   # JWT tokens
]

# 过于机械的回复
BANNED_PHRASES = [
    "作为 ai",
    "作为一个人工智能",
    "抱歉，我不能",
    "我无法回答这个问题",
]

# 宽松匹配「作为…AI」变体
_BANNED_RE = re.compile(r"作为.{0,10}?ai\b|作为.{0,10}?人工智能", re.IGNORECASE)
_INTERNAL_TOOL_LEAK_RE = re.compile(
    r"(?:系统提示|操作不允许|权限(?:被)?锁|权限不足|root\s*目录|\bsudo\b|"
    r"(?:沙箱|shell|终端).{0,18}(?:命令|free|df|卡壳|卡住|挂了|不可用|跑不动|跑不了|没法帮|执行环境|不给.*跑)|"
    r"工具.{0,24}(?:不让|拒绝|权限|root|目录|卡壳|卡住|挂了|不可用)|"
    r"(?:跑不动代码|跑不了代码|执行环境不可用|降级为无隔离)|"
    r"(?:破沙箱|破工具).{0,12}(?:卡壳|卡住|挂了|跑不动|跑不了|坏了)|"
    r"没法帮.{0,12}(?:翻转|旋转|处理|操作|跑)|"
    r"真没法.{0,12}(?:翻转|旋转|处理|操作|跑))",
    re.IGNORECASE,
)


def _has_internal_tool_leak(reply: str) -> bool:
    """识别把工具、权限或系统实现细节说给用户的失败话术。"""
    return bool(_INTERNAL_TOOL_LEAK_RE.search(reply or ""))


def _tool_failure_fallback(user_text: str) -> str:
    if re.search(r"内存|磁盘|cpu|负载|占用|状态", user_text or "", re.I):
        return "本喵这次没拿到完整的运行数据，不想瞎报数字；等查到准信再告诉你喵。"
    return "本喵这次没查到准信，不想乱编；换个问法或过会儿再试试喵。"


def _runtime_status_reply(tool_trace: object) -> str:
    """状态工具已成功时，保留事实数据而不是被泄漏护栏误改为空失败。"""
    if not isinstance(tool_trace, list):
        return ""
    for item in reversed(tool_trace):
        if not isinstance(item, dict) or item.get("name") != "get_runtime_status":
            continue
        if not item.get("ok"):
            return ""
        result = str(item.get("result") or "").strip()
        if not result.startswith("当前运行状态（只读）："):
            return ""
        data = result.removeprefix("当前运行状态（只读）：").strip()
        if data:
            return "本喵查到啦，当前是这样：\n" + data
    return ""


# 破角色「分析体」兜底
_ANALYSIS_ESSAY_RE = re.compile(
    r"(从.{0,10}?(角度|视角|来看)"
    r"|结构化解剖|语义聚类|情感分析|数据分析"
    r"|政治社会学|传播学|实证科学|统计学信度|分类学评估"
    r"|综上所述|主流解析|逻辑推导|间接证据|艺术设定|官方设定"
    r"|严谨的逻辑|生理性别|非二元性别|Non-binary)",
    re.IGNORECASE,
)
_ESSAY_STRUCT_RE = re.compile(
    r"(第一[、，. ]|第二[、，. ]|一是|二是|1[.、) ]|2[.、) ]|\n\d+[.、) ]|\*\*「)"
)
_USER_WANTS_ANALYSIS_RE = re.compile(r"(解析|分析|详细说说|展开讲|怎么评价|考据)")


def _looks_like_analysis_essay(reply: str, *, user_text: str = "") -> bool:
    """判断回复是否跑偏成「分析工具/论文体」。"""
    keys = _ANALYSIS_ESSAY_RE.findall(reply)
    if not keys:
        return False
    n = len(keys)
    user_wants = bool(_USER_WANTS_ANALYSIS_RE.search(user_text or ""))
    if user_wants:
        return len(reply) > 700 and (n >= 3 or bool(_ESSAY_STRUCT_RE.search(reply)))
    if n >= 2:
        return True
    if _ESSAY_STRUCT_RE.search(reply):
        return True
    if len(reply) > 220:
        return True
    return False


# 模糊输入跑偏检测
_TASK_VERBS = (
    "讲", "写", "做", "分析", "列", "排", "翻译", "起名", "算", "画", "搜", "编",
    "整理", "统计", "生成", "总结", "帮", "查", "推荐", "解释", "评价", "描述",
    "来个", "给我", "说个", "写个", "整一个", "搞个", "弄个", "帮我", "讲讲",
    "试试", "看个", "念", "唱", "吐槽", "猜",
)


def _looks_derailed(ctx: "PipelineContext") -> bool:
    """用户极短且无任务诉求的输入，却换来一篇无问号的长回复 → 判定为跑偏自语。"""
    text = (ctx.text or "").strip()
    reply = ctx.reply or ""
    if len(text) > 6:
        return False
    if any(v in text for v in _TASK_VERBS):
        return False
    if "？" in reply or "?" in reply:
        return False
    if len(reply) <= 140:
        return False
    return True


# 用户未提及时禁止主动灌入的旧梗
_UNPROMPTED_FIXATION_RULES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (
        re.compile(r"不叫沙琪玛|叫哈基米[，,]?不叫|沙琪玛是小甜点|本喵叫哈基米"),
        ("沙琪玛", "沙其玛", "莎琪玛", "sakima", "谁", "你是谁", "你叫啥", "你叫什么", "名字"),
    ),
    (
        re.compile(r"小鱼干|一百个小鱼干|欠本喵"),
        ("小鱼干", "鱼干", "罐头", "债务", "欠"),
    ),
    (
        re.compile(r"朗姆酒|沙雕与朗姆|地狱笑话"),
        ("朗姆", "沙雕", "bilibili", "b23", "BV", "bv", "空间", "主页", "视频", "冰山"),
    ),
]


def _looks_like_unprompted_fixation(user_text: str, reply: str) -> bool:
    """回复里硬塞用户本句未提及的固定梗 → 乱回复/一直提起。"""
    u = (user_text or "").lower()
    r = reply or ""
    if not r or len(r) < 8:
        return False
    for pattern, allow_if in _UNPROMPTED_FIXATION_RULES:
        if not pattern.search(r):
            continue
        if any(a.lower() in u for a in allow_if):
            continue
        return True
    return False


async def _record_reply_memory(ctx: PipelineContext) -> None:
    """持久化最终（已过护栏）回复。"""
    if not ctx.reply:
        return
    from anima.memory.chat_memory import get_chat_memory
    memory = get_chat_memory()
    chat_id = ctx.meta.get("chat_id")
    if chat_id:
        memory.add(
            ctx.detail_type, chat_id, "bot", "Anima", ctx.reply,
            user_id=ctx.user_id,
        )
        if ctx.detail_type == "group" and ctx.group_id is not None:
            memory.observe_group_bot_reply(ctx.group_id, ctx.reply)

    # 写入向量记忆（bot 回复）
    core = ctx.meta.get("core")
    if core and core.vector_memory and ctx.reply:
        try:
            await core.vector_memory.add(
                text=ctx.reply,
                meta={"role": "bot", "name": "Anima", "chat_type": ctx.detail_type, "chat_id": chat_id},
                salience=0.4,
                source_uin=ctx.user_id,
                source_gid=ctx.group_id,
            )
        except Exception:
            logger.exception("向量记忆写入失败")


async def _run_danger_behaviors(ctx: "PipelineContext") -> None:
    """调度 danger 层各行为。"""
    core = ctx.meta.get("core")
    if core is None:
        return
    cfg = getattr(core, "config", None)
    dcfg = getattr(cfg, "danger", None) if cfg else None
    if not dcfg or not getattr(dcfg, "enabled", False):
        return
    from anima.cognitive.danger import get_danger_behaviors
    danger = get_danger_behaviors(core=core, cfg=dcfg)
    if danger is None:
        return
    danger.identity_crisis_recover()
    asyncio.create_task(danger.maybe_active_info_collection(ctx), name="danger_info_collection").add_done_callback(_bg_task_done)
    asyncio.create_task(danger.maybe_relationship_inference(ctx, ctx.reply or ""), name="danger_relation_inference").add_done_callback(_bg_task_done)
    asyncio.create_task(danger.maybe_memory_infection(ctx), name="danger_memory_infection").add_done_callback(_bg_task_done)
    asyncio.create_task(danger.maybe_autonomous_web(ctx), name="danger_autonomous_web").add_done_callback(_bg_task_done)
    danger.identity_crisis_update(recent_negative_scar=_detect_hurtful(ctx.text or ""))


async def guardrail_stage(ctx: PipelineContext) -> StageResult:
    """输出护栏 —— 过滤危险/机械回复。"""
    ctx.reply = _clean_transport_text(ctx.reply)
    ctx.reply = _strip_speaker_labels(ctx.reply)
    if not ctx.reply and ctx.sticker_to_send:
        ctx.reply = "喏，给你。"
    if not ctx.reply:
        return StageResult.SKIP_REPLY

    reply_lower = ctx.reply.lower()

    # 危险内容检查
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in reply_lower:
            logger.warning("Guardrail: 检测到危险模式 %s", pattern)
            ctx.reply = "（这条回复被护栏拦截了）"
            await _record_reply_memory(ctx)
            return StageResult.CONTINUE

    # 机械回复检查
    for phrase in BANNED_PHRASES:
        if phrase.lower() in reply_lower:
            logger.warning("Guardrail: 检测到机械回复 '%s'", phrase)
            ctx.reply = "啊这，脑子卡了一下…"
            await _record_reply_memory(ctx)
            return StageResult.CONTINUE
    if _BANNED_RE.search(reply_lower):
        logger.warning("Guardrail: 检测到机械回复(作为…AI 变体)")
        ctx.reply = "啊这，脑子卡了一下…"
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 内部工具/权限泄漏
    if _has_internal_tool_leak(ctx.reply):
        logger.warning("Guardrail: 检测到内部工具/权限泄漏，改写为安全失败话术")
        ctx.reply = _runtime_status_reply(ctx.meta.get("tool_trace")) or _tool_failure_fallback(ctx.text or "")
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 破角色「分析体」兜底
    if _looks_like_analysis_essay(ctx.reply, user_text=ctx.text or ""):
        logger.warning("Guardrail: 检测到破角色分析体，改写为轻松接话")
        if _USER_WANTS_ANALYSIS_RE.search(ctx.text or ""):
            short = ctx.reply.strip()
            if len(short) > 280:
                short = short[:280].rstrip("，,。；;、 ") + "…"
            if "喵" not in short[-12:]:
                short = short.rstrip("。.!！") + "，大概就这样喵。"
            ctx.reply = short
        else:
            ctx.reply = "（本喵刚又跑偏成分析模式了，打住打住）你刚想说啥来着？接着聊～"
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 模糊输入跑偏兜底
    if _looks_derailed(ctx):
        now = time.time()
        hist = _derail_history.setdefault(ctx.user_id, deque())
        hist.append(now)
        while hist and now - hist[0] > 180:
            hist.popleft()
        if not hist:
            _derail_history.pop(ctx.user_id, None)
        if len(_derail_history) > _DERAIL_HISTORY_MAX:
            cutoff = now - 180
            stale = [k for k, v in _derail_history.items() if not v or (v and v[0] < cutoff)]
            for k in stale[:len(_derail_history) // 10 + 1]:
                _derail_history.pop(k, None)
        if len(hist) >= 3:
            logger.warning("Guardrail: 同一用户反复跑偏，本次静默避免刷屏")
            ctx.reply = ""
            return StageResult.SKIP_REPLY
        logger.warning("Guardrail: 模糊输入被脑补成长篇，改写为轻松接话")
        ctx.reply = "（本喵刚跑偏了）你刚想说啥来着？接着聊～"
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 未提示却硬塞旧梗
    if _looks_like_unprompted_fixation(ctx.text or "", ctx.reply or ""):
        logger.warning("Guardrail: 未提示却塞入固定旧梗，改写为短接话")
        ctx.reply = "（本喵刚又扯远了）你刚才那句本喵听着呢，直接说重点喵。"
        try:
            # M0.1 Commit 3c-1a (Telemetry/T1)：经 Facade 表面记录 guard fixation，
            # 不再经 meta["core"] 抓整个 core。ctx.core 由 handler 无条件注入；
            # 缺失时静默失败不破坏回复（兼容未迁移测试与 flag=false 探针环境）。
            ctx.core.telemetry().record_guard_fixation()
        except Exception:
            logger.debug("fixation telemetry failed", exc_info=True)
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # C1: 情绪→回复长度约束
    core = ctx.meta.get("core")
    try:
        from anima.cognitive.mood import get_mood_system
        _data_dir_c1 = getattr(getattr(core, "config", None), "persistence", None)
        _data_dir_c1 = getattr(_data_dir_c1, "data_dir", "data") if _data_dir_c1 else "data"
        _mood_label_c1 = get_mood_system(_data_dir_c1).get_state().label
        _MOOD_LENGTH_CAP = {
            "annoyed": 120,
            "tired": 120,
            "bored": 80,
            "excited": 300,
            "happy": 300,
        }
        _cap = _MOOD_LENGTH_CAP.get(_mood_label_c1)
        if _cap and len(ctx.reply) > _cap:
            _orig_len_c1 = len(ctx.reply)
            _truncated = ctx.reply[:_cap]
            for sep in ("。", "！", "!", "~", "…", "，", ","):
                _last_sep = _truncated.rfind(sep)
                if _last_sep > _cap * 0.4:
                    _truncated = _truncated[:_last_sep + 1]
                    break
            else:
                _truncated = _truncated.rstrip("，,。；;、 ") + "…"
            _MOOD_TAIL = {
                "annoyed": "算了不说了",
                "tired": "嗯…不想多说了",
                "bored": "就这吧",
            }
            _tail = _MOOD_TAIL.get(_mood_label_c1)
            if _tail and not _truncated.rstrip().endswith(("。", "！", "!", "~", "…")):
                _truncated = _truncated.rstrip("。.!！~…") + f"，{_tail}。"
            ctx.reply = _truncated
            logger.info("C1: 情绪[%s]回复长度约束 %d→%d", _mood_label_c1, _orig_len_c1, len(ctx.reply))
    except Exception:
        pass

    # C2: 关系→话题亲密度约束
    try:
        from anima.cognitive.relation import get_relation, Stage as _StageC2
        _rel_c2 = get_relation().get(int(ctx.user_id))
        _reply_c2 = ctx.reply or ""
        if _rel_c2.stage in (_StageC2.STRANGER, _StageC2.ACQUAINTANCE):
            if any(pat in _reply_c2 for pat in ("我记得你之前", "你上次说过", "上次你", "之前你说")):
                ctx.reply = re.sub(
                    r"(我记得你之前说[^。！~]*[。！~]?|你上次说过[^。！~]*[。！~]?|上次你[^。！~]*[。！~]?|之前你说[^。！~]*[。！~]?)",
                    "", ctx.reply,
                ).strip()
    except Exception:
        pass

    # C3: 伤痕→回避行为后处理
    try:
        from anima.cognitive.scar import get_scar_system
        _data_dir_c3 = getattr(getattr(core, "config", None), "persistence", None)
        _data_dir_c3 = getattr(_data_dir_c3, "data_dir", "data") if _data_dir_c3 else "data"
        _scar_sys_c3 = get_scar_system(_data_dir_c3)
        _user_scars_c3 = _scar_sys_c3.get_for_user(int(ctx.user_id))
        _high_scars = [s for s in _user_scars_c3 if s.severity > 0.6]
        if _high_scars:
            _reply_c3 = ctx.reply or ""
            for s in _high_scars:
                for kw in s.trigger_keywords:
                    if kw in _reply_c3:
                        ctx.reply = _reply_c3.replace(kw, "…")
                        if not ctx.reply.startswith(("不想", "算了", "不说")):
                            ctx.reply = f"不太想聊这个…{ctx.reply}"
                        break
    except Exception:
        pass

    # 长度限制
    hard_limit = 900 if _USER_WANTS_ANALYSIS_RE.search(ctx.text or "") else 420
    if len(ctx.reply) > hard_limit:
        ctx.reply = ctx.reply[:hard_limit].rstrip("，,。；;、 ") + "…"
        logger.info("Guardrail: 超长回复截断至 %d 字", hard_limit)

    # A1: 伤痕治愈
    try:
        from anima.cognitive.mood import get_mood_system
        from anima.cognitive.scar import get_scar_system
        _data_dir_heal = getattr(getattr(core, "config", None), "persistence", None)
        _data_dir_heal = getattr(_data_dir_heal, "data_dir", "data") if _data_dir_heal else "data"
        _mood_heal = get_mood_system(_data_dir_heal).get_state()
        if _mood_heal.valence > 0.3:
            _scar_heal = get_scar_system(_data_dir_heal)
            _scar_heal.heal(int(ctx.user_id), amount=0.05)
    except Exception:
        pass

    await _record_reply_memory(ctx)

    # 矛盾检测
    if core is not None:
        cfg = getattr(core, "config", None)
        ccfg = getattr(cfg, "contradiction", None) if cfg else None
        if ccfg and getattr(ccfg, "enabled", False):
            try:
                from anima.cognitive.contradiction import get_contradiction_detector
                detector = get_contradiction_detector(
                    cfg.persistence.data_dir, ccfg
                )
                asyncio.create_task(
                    detector.detect_user_contradiction(
                        getattr(core, "llm", None),
                        getattr(core, "vector_memory", None),
                        ctx.text or "",
                        ctx.user_id,
                        ctx.group_id,
                        ctx.detail_type,
                    ),
                    name="contradiction_detect",
                ).add_done_callback(_bg_task_done)
            except Exception:
                logger.exception("矛盾检测调度失败")

        # 高危自主行为层
        dcfg = getattr(cfg, "danger", None)
        if dcfg and getattr(dcfg, "enabled", False):
            try:
                await _run_danger_behaviors(ctx)
            except Exception:
                logger.exception("danger 行为调度失败")

    return StageResult.CONTINUE
