"""阶段 5: 输出护栏。"""

from __future__ import annotations
import asyncio
import logging
import random
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

#: 机械回复改写话术池（`BANNED_PHRASES` 命中 / 「作为…AI」变体 两处出口共用，2026-09-19 去模板化）。
#: 触发条件 = 模型输出了「作为AI / 抱歉我不能 / 我无法回答」这类机械话术；这里给自然的猫娘反应，
#: 不解释「被过滤了」，避免把护栏的存在暴露给用户。
_MECHANICAL_REWRITES = (
    "啊这……本喵脑子卡了一下。",
    "唔……这话本喵接不太上来，你换个说法喵？",
    "本喵刚想说的词儿跑了……换个话题呗？",
)

def _record_fallback(ctx: "PipelineContext", kind: str) -> None:
    """兜底出站计数（经 M0.1 Facade，fail-soft）。

    口径见 `ReplyQuality.record_fallback`。护栏改写 = 用户看到的那句话**不是**
    模型原本想说的，而是被替换过的 ⇒ 它属于「机器话术出站」，必须与正常生成
    的正文分开计数，否则「人格完善」的效果无从度量。

    `ctx.core` 由 handler 无条件注入；缺失时静默失败，绝不破坏回复路径
    （与 `guardrail_stage` 里 `record_guard_fixation` 同一套 fail-soft 口径）。
    """
    try:
        ctx.core.telemetry().record_fallback(kind)
    except Exception:
        logger.debug("兜底出站遥测记录失败", exc_info=True)


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
    r"|严谨的逻辑)",
    re.IGNORECASE,
)
# 2026-09-17（R16 男娘人设）：从词表摘除「生理性别 / 非二元性别 / Non-binary」。
# 它们原本是「考据体」信号，但在 R16 人设下 bot 正常聊性别/男娘话题也会命中，
# 导致整段被改写为「本喵刚又跑偏成分析模式了」—— 与新人设直接冲突。
# 真正的考据体仍被兜住：上述其余词（官方设定 / 严谨的逻辑 / 主流解析 / 综上所述）
# 配合 _ESSAY_STRUCT_RE 结构词即可命中，回归见 tests/test_guardrail_analysis.py
# 的 ACADEMIC_GENDER_ESSAY 用例（仍期望 True）。
_ESSAY_STRUCT_RE = re.compile(
    r"(第一[、，. ]|第二[、，. ]|一是|二是|1[.、) ]|2[.、) ]|\n\d+[.、) ]|\*\*「)"
)
_USER_WANTS_ANALYSIS_RE = re.compile(r"(解析|分析|详细说说|展开讲|怎么评价|考据)")


def _looks_like_analysis_essay(reply: str, *, user_text: str = "") -> bool:
    """判断回复是否跑偏成「分析工具/论文体」。

    2026-09-20 深审补漏：`_ANALYSIS_ESSAY_RE` 是**闭集语义词表**（"从X角度看 /
    综上所述 / 官方设定"…）。模型换一套说法（"社区治理 / 公关层面 / 流量归因 /
    风险隔离"）就一个都不中 ⇒ 原实现的 `if not keys: return False` 直接放行。
    生产实测：用户只**粘贴了一段贴吧长文、没有提问**，bot 回了
    「1. 溯源与确权… 2. 流量归因与风险隔离… 3. 分流与共存方案…」——骨架是
    教科书式论文体，却因为词表没中而漏网。

    ⇒ 让**结构词（编号分点）独立生效**，不再被语义词表前置短路；同时保留
    「用户自己要求分析」的豁免（那种长分点回复是用户要的，不该改写）。
    """
    reply = reply or ""
    keys = _ANALYSIS_ESSAY_RE.findall(reply)
    n = len(keys)
    struct_hits = len(_ESSAY_STRUCT_RE.findall(reply))
    user_wants = bool(_USER_WANTS_ANALYSIS_RE.search(user_text or ""))
    if user_wants:
        return len(reply) > 700 and (n >= 3 or struct_hits >= 1)
    # 两个以上编号分点 + 长正文 = 论文体骨架（不依赖语义词表是否命中）
    if struct_hits >= 2 and len(reply) >= 220:
        return True
    if not keys:
        return False
    if n >= 2:
        return True
    if struct_hits:
        return True
    if len(reply) > 220:
        return True
    return False


# 政治敏感词表（高精度专名级；2026-09-11 owner 指示：此类门槛只拦政治内容，
# 不再用启发式猜测「跑偏」——后者曾误拦严肃医学追问「双低会死吗」）
_POLITICAL_RE = re.compile(
    r"(习近平|李克强|李强|温家宝|胡锦涛|毛泽东|邓小平|江泽民|周恩来|"
    r"中共|共产党|国民党|民进党|政府朝鲜|六四|天安门事件|文革|文化大革命|"
    r"台独|港独|藏独|疆独|法轮功|习近平连任|总书记|国务院总理|"
    r"台湾独立|西藏独立|新疆独立|颜色革命|政变|镇压|屠杀)"
, re.I)


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


def _strip_fixation_sentences(reply: str) -> str:
    """剥离命中旧梗模式的句子，保留其余内容（整段罐头改写会答非所问）。"""
    parts = re.split(r"(?<=[。！？!?\n])", reply or "")
    kept = [p for p in parts if p.strip() and not any(
        pat.search(p) for pat, _ in _UNPROMPTED_FIXATION_RULES)]
    return "".join(kept).strip()


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
    # 对方示意结束（转私聊 / 别说了 / 不聊了 / 晚安…）时，不生成「还想追问」的欲望，
    # 避免别人不想聊时 bot 在心跳期又冒出问题 —— 源头减少主动开口。
    _skip_info_collection = False
    try:
        from anima.llm.group_gate import is_conversation_end_signal
        if is_conversation_end_signal(getattr(ctx, "text", "") or ""):
            _skip_info_collection = True
    except Exception:
        pass
    if not _skip_info_collection:
        asyncio.create_task(
            danger.maybe_active_info_collection(ctx), name="danger_info_collection"
        ).add_done_callback(_bg_task_done)
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
            _record_fallback(ctx, "leak_blocked")
            ctx.reply = "（这条回复被护栏拦截了）"
            await _record_reply_memory(ctx)
            return StageResult.CONTINUE

    # 机械回复检查
    for phrase in BANNED_PHRASES:
        if phrase.lower() in reply_lower:
            logger.warning("Guardrail: 检测到机械回复 '%s'", phrase)
            _record_fallback(ctx, "mechanical")
            ctx.reply = random.choice(_MECHANICAL_REWRITES)
            await _record_reply_memory(ctx)
            return StageResult.CONTINUE
    if _BANNED_RE.search(reply_lower):
        logger.warning("Guardrail: 检测到机械回复(作为…AI 变体)")
        _record_fallback(ctx, "mechanical")
        ctx.reply = random.choice(_MECHANICAL_REWRITES)
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
            _record_fallback(ctx, "analysis_essay")
            ctx.reply = "（本喵刚又跑偏成分析模式了，打住打住）你刚想说啥来着？接着聊～"
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 模糊输入跑偏兜底

    # ── 政治敏感内容门槛（2026-09-11 owner 指示：此类门槛只拦政治）──
    # 平台会对政治内容封号，bot 输出命中高精度政治词表时人格化回避。
    # 词表刻意保持高精度（专名级），避免误伤正常聊天。
    try:
        if _POLITICAL_RE.search(ctx.reply or ""):
            logger.warning("Guardrail: 回复命中政治敏感词，改写为话题回避")
            _record_fallback(ctx, "political")
            ctx.reply = "这个话题本喵不碰喵～聊点别的吧！"
    except Exception:
        pass

    # 未提示却硬塞旧梗
    if _looks_like_unprompted_fixation(ctx.text or "", ctx.reply or ""):
        # 体验测试 B3：整段罐头改写会让用户的问题得到答非所问的回复。
        # 改为剥离命中旧梗的句子、保留其余内容；剥无可剥才用切题短兜底。
        _stripped = _strip_fixation_sentences(ctx.reply or "")
        if len(_stripped) >= 8:
            logger.warning("Guardrail: 未提示却塞入固定旧梗，已剥离旧梗句保留其余回答")
            ctx.reply = _stripped
        else:
            logger.warning("Guardrail: 未提示却塞入固定旧梗且剥离后无内容，改写为短接话")
            ctx.reply = "本喵刚才想到别的去了，直接说重点喵。"
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
            if _tail:
                # 只在截断留下半截子句时补情绪尾巴；完整句（含问句/省略号）
                # 不再拼接，且先剥干净尾部逗号——修复「，，嗯…不想多说了」硬拼
                # （2026-09-06 用户体验测试③）。
                _body = _truncated.rstrip()
                if not _body.endswith(("。", "！", "!", "？", "?", "~", "…")):
                    _body = _body.rstrip("，,、；;。．!！?？~… ")
                    _truncated = f"{_body}，{_tail}。" if _body else f"{_tail}。"
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
