"""回复闸门前置阶段（队列④第二刀，2026-09-12）。

原 ``LLMHandler._handle_inner`` 的三大前检块整体迁入 pipeline，成为流水线的
前置 stage（注册在 permission 之前，顺序即语义）：

1. ``prefilter_stage``  —— 睡眠 / quiet_hours / 群策略 / 忽略名单 / 回复闸门 /
   被动冷却。命中拦截即 ``STOP``（置 ``should_reply=False`` + ``stop_reason``，
   不产生 reply_unavailable 遥测——与原 return-early 语义一致）；
2. ``attachment_text_stage`` —— 小文本附件 / 语音转写合并进 ``ctx.text``
   （原在闸门之后、ctx 构建之前执行，顺序保持）；
3. ``bullying_stage`` —— 霸凌检测写 ``ctx.meta`` 标记（generation 据此冷回/沉默）。

防抖（私聊/群聊消息合并）**有意保留在 handler**：它必须先于跨群观察/表情收集
执行，那些观察不属于 pipeline。
handler 实例经 ``ctx.meta["_handler"]`` 注入（stage 需要唤醒窗/冷却/忽略名单等
handler 态）；``trace`` 经 ``ctx.meta["trace"]`` 透传供日志串联。
"""
from __future__ import annotations

import logging
import re

from anima.llm.group_gate import (
    classify_group_reply_skip,
    is_conversation_continuation,
    is_direct_group_call,
    resolve_group_reply_mode,
)
from anima.pipeline import PipelineContext, StageResult

logger = logging.getLogger(__name__)


def _handler_of(ctx: PipelineContext):
    return ctx.meta.get("_handler")


async def prefilter_stage(ctx: PipelineContext) -> StageResult:
    """fail-closed 外壳：闸门自身异常时拦截（不回复），与原 return-early 一致。

    Pipeline.run 对非生成阶段异常默认「跳过继续」，若放行会让本应被闸门
    拦下的消息继续生成——因此这里显式收口为 STOP。睡眠/免打扰子块的
    「异常退回正常流程」语义由内部局部 try/except 保留。
    """
    try:
        return await _prefilter_inner(ctx)
    except Exception:
        logger.exception("[prefilter] 前置闸门异常，fail-closed 拦截")
        ctx.should_reply = False
        ctx.stop_reason = "prefilter:error"
        return StageResult.STOP


async def _prefilter_inner(ctx: PipelineContext) -> StageResult:
    """睡眠 / 免打扰 / 群策略 / 忽略名单 / 回复闸门 / 被动冷却（原样迁移）。"""
    h = _handler_of(ctx)
    event, text = ctx.event, ctx.text
    user_id, group_id = ctx.user_id, ctx.group_id
    detail_type, trace = ctx.detail_type, ctx.meta.get("trace", "-")

    # A1 睡眠模式：凌晨 1-5 点，群聊被 @ 本喵时 80% 装睡不回、20% 迷糊回一句。
    # 不走完整 Pipeline / 不调工具（ReAct），保证夜间不打扰且不会误触发复杂动作。
    # 未 @ 本喵的消息直接落到下方闸门（常态 skip），无需特殊处理。
    try:
        acfg = h.core.config.autonomy
        if getattr(acfg, "sleep_enabled", False) and detail_type == "group" and group_id:
            from anima.cognitive.sleep_state import is_sleep_time, classify_sleep_reply
            if is_sleep_time(
                start_hour=getattr(acfg, "sleep_start_hour", 1),
                end_hour=getattr(acfg, "sleep_end_hour", 5),
            ):
                _at_me = bool(event.get("mentions_self") or event.get("reply_to_self"))
                _decision = classify_sleep_reply(
                    _at_me, getattr(acfg, "sleep_silent_prob", 0.8),
                )
                if _decision == "silent":
                    logger.info(
                        "[睡眠][trace=%s] 群%s @本喵，睡眠中装睡不回", trace, group_id,
                    )
                    ctx.should_reply = False
                    ctx.stop_reason = "prefilter:sleep_silent"
                    return StageResult.STOP
                if _decision == "sleepy":
                    await h._sleepy_reply(
                        group_id, int(user_id), text, acfg,
                    )
                    ctx.should_reply = False
                    ctx.stop_reason = "prefilter:sleepy_reply"
                    return StageResult.STOP
    except Exception:
        logger.exception("[睡眠] 模式判定异常（已隔离，退回正常流程）")

    # quiet_hours 免打扰：命中时段且为群聊非点名被动消息时静默跳过。
    # 与 sleep 区分：quiet_hours 只拦非点名被动回复；点名/@/私聊默认豁免仍回。
    try:
        acfg = h.core.config.autonomy
        if getattr(acfg, "quiet_hours_enabled", False) and detail_type == "group" and group_id:
            from anima.cognitive.sleep_state import is_sleep_time
            if is_sleep_time(
                start_hour=getattr(acfg, "quiet_hours_start_hour", 23),
                end_hour=getattr(acfg, "quiet_hours_end_hour", 7),
            ):
                _direct = False
                try:
                    from anima.llm.group_gate import is_direct_group_call
                    _direct = bool(is_direct_group_call(event, text))
                except Exception:
                    _direct = bool(event.get("mentions_self") or event.get("reply_to_self"))
                _allow_direct = getattr(acfg, "quiet_hours_allow_direct", True)
                if not (_allow_direct and _direct):
                    logger.info("[quiet_hours][trace=%s] 免打扰时段，非点名被动消息静默跳过 gid=%s",
                                trace, group_id)
                    ctx.should_reply = False
                    ctx.stop_reason = "prefilter:quiet_hours"
                    return StageResult.STOP
    except Exception:
        logger.exception("[quiet_hours] 判定异常（已隔离，退回正常流程）")

    # 群聊回复闸门：仅当被 @、被回复或文字喊「哈基米」时才回复；私聊始终回复。
    # owner 命令已在 pipeline 之前处理，不受此闸门影响。
    group_policy = {}
    if detail_type == "group" and group_id is not None:
        policies = getattr(getattr(h.core.config, "permissions", None), "group_policies", {})
        candidate = policies.get(str(group_id), {}) if isinstance(policies, dict) else {}
        group_policy = candidate if isinstance(candidate, dict) else {}
    if detail_type == "group" and group_policy.get("reply_enabled") is False:
        logger.debug("[群策略][trace=%s] 群 %s 已关闭普通回复", trace, group_id)
        ctx.should_reply = False
        ctx.stop_reason = "prefilter:group_reply_disabled"
        return StageResult.STOP
    if detail_type == "group":
        # owner 忽略名单：命中直接跳过（"别再理那两人"落地成真的拦截）。
        from anima.llm.ignore_list import should_skip_ignored
        if should_skip_ignored(h._ignore_store(), detail_type, int(group_id), int(user_id)):
            from anima.llm.group_gate import resolve_group_reply_mode
            mode = resolve_group_reply_mode(group_policy, h.core.config.autonomy)
            logger.info("[回复闸门][trace=%s] skip reason=ignored_user gid=%s uid=%s mode=%s",
                        trace, group_id, user_id, mode)
            quality = getattr(h.core, "reply_quality", None)
            if quality is not None:
                try:
                    quality.record_gate_skip("ignored_user")
                except Exception:
                    logger.debug("ignore gate skip telemetry failed", exc_info=True)
            ctx.should_reply = False
            ctx.stop_reason = "prefilter:ignored_user"
            return StageResult.STOP
        # 唤醒会话窗：直接呼叫即开启/续期；窗内相关消息可自然接续。
        try:
            from anima.llm.group_gate import is_direct_group_call
            if is_direct_group_call(event, text):
                h._mark_wake(int(group_id), int(user_id))
            session_active = h._wake_session_active(int(group_id), int(user_id))
        except Exception:
            session_active = False
        data_dir = getattr(getattr(h.core.config, "persistence", None), "data_dir", "data")
        skip_reason = classify_group_reply_skip(
            event, text,
            user_id=int(user_id),
            group_policy=group_policy,
            autonomy=h.core.config.autonomy,
            data_dir=str(data_dir or "data"),
            social=getattr(h.core.config, "social", None),
            session_active=session_active,
        )
        if skip_reason:
            from anima.llm.group_gate import resolve_group_reply_mode
            mode = resolve_group_reply_mode(group_policy, h.core.config.autonomy)
            logger.info(
                "[回复闸门][trace=%s] skip reason=%s mode=%s text_len=%d",
                trace, skip_reason, mode, len(text or ""),
            )
            quality = getattr(h.core, "reply_quality", None)
            if quality is not None:
                try:
                    quality.record_gate_skip(skip_reason)
                except Exception:
                    logger.debug("gate skip telemetry failed", exc_info=True)
            ctx.should_reply = False
            ctx.stop_reason = f"prefilter:{skip_reason}"
            return StageResult.STOP

        # 每用户被动回复冷却：同一用户刚回过、这次又非直接点名时，跳过避免逐条抢答。
        # 被 @ / 回复 / 喊名（直接呼叫）不受此限制；唤醒窗内命中纠正/补充词的
        # 消息（"不对，我说的是X"）也豁免——那是对话仍在进行的信号，不是刷屏。
        if detail_type == "group" and h._passive_should_cooldown(group_id, user_id):
            try:
                from anima.llm.group_gate import is_direct_group_call
                _direct = bool(is_direct_group_call(event, text))
            except Exception:
                _direct = False
            if not _direct:
                _continuation = False
                if bool(getattr(
                        getattr(h.core.config, "autonomy", None),
                        "correction_exempt_cooldown", True)):
                    try:
                        from anima.llm.group_gate import is_conversation_continuation
                        _continuation = bool(
                            is_conversation_continuation(text)
                            and h._wake_session_active(int(group_id), int(user_id))
                            and h._continuation_exempt_ok(group_id, user_id))
                    except Exception:
                        _continuation = False
                if not _continuation:
                    from anima.llm.group_gate import resolve_group_reply_mode
                    mode = resolve_group_reply_mode(group_policy, h.core.config.autonomy)
                    logger.info("[回复闸门][trace=%s] skip reason=passive_cooldown gid=%s uid=%s",
                                trace, group_id, user_id)
                    ctx.should_reply = False
                    ctx.stop_reason = "prefilter:passive_cooldown"
                    return StageResult.STOP

    return StageResult.CONTINUE


async def attachment_text_stage(ctx: PipelineContext) -> StageResult:
    """小文本附件 / 语音转写合并进 ctx.text（原样迁移；闸门之后执行）。"""
    h = _handler_of(ctx)
    event, text = ctx.event, ctx.text
    user_id = ctx.user_id

    # 小文本附件理解：只经 OneBot get_file 读取受信任的本地/base64 返回，
    # 不跟随 URL，避免附件段成为 SSRF 或大文件读取入口。
    if event.get("files"):
        try:
            attachment_text = await h._extract_text_attachments(event)
            if attachment_text:
                text = (text + "\n" + attachment_text).strip()
                event["text"] = text
                ctx.text = text
        except Exception:
            logger.exception("文本附件提取异常（已隔离，不阻塞回复）")

    if event.get("records"):
        try:
            transcript = await h._transcribe_records(event, int(user_id))
            if transcript:
                text = (text + "\n[语音转写]\n" + transcript).strip()
                event["text"] = text
                ctx.text = text
        except Exception:
            logger.exception("语音转写异常（已隔离，不阻塞回复）")

    return StageResult.CONTINUE


async def bullying_stage(ctx: PipelineContext) -> StageResult:
    """霸凌检测（自我保护协议）：命中写 ctx.meta，generation 据此冷回/沉默。"""
    h = _handler_of(ctx)
    text, user_id = ctx.text, ctx.user_id
    group_id, detail_type = ctx.group_id, ctx.detail_type
    try:
        from anima.cognitive.bullying_detector import detect_bullying
        from anima.memory.chat_memory import get_chat_memory
        _cm = get_chat_memory()
        _recent_texts: list[str] = []
        if detail_type == "group" and group_id:
            _recent_texts = [m["content"] for m in
                             _cm.get_context("group", int(group_id), recent_rounds=8, user_id=int(user_id))
                             if m.get("role") == "user"]
        else:
            _recent_texts = [m["content"] for m in
                             _cm.get_context("private", int(user_id), recent_rounds=8, user_id=int(user_id))
                             if m.get("role") == "user"]
        _bully = detect_bullying(int(user_id), text, _recent_texts[-8:])
        # #7 霸凌统一（2026-08-28）：①playful 抵消（复用 _detect_hurtful 的
        # playful_markers 单源）——有戏派玩笑「滚，别烦本喵」不算霸凌；②单点分级
        # 喂 feel + generation（severity/recent_count 合并为轻/重）。
        # ③ 目标判定 + 主人豁免（2026-08-29 修复误判）：私聊/群里「提到骂人词但
        # 不是骂本喵」不能触发沉默（如「是不是觉得群里的人太傻逼了」是问 bot 看法，
        # 不是攻击 bot）；owner 绝不因霸凌检测被静默。
        _severity = int(_bully["severity"])
        _recent = int(_bully["recent_count"])
        # 主人豁免：owner 不会被霸凌沉默（ta 可以说狠话，是 bot 的主人）。
        _is_owner = h._is_owner(int(user_id))
        if _is_owner:
            _bully = {"is_bullying": False, "severity": 0, "recent_count": 0}
            _severity = 0
            _recent = 0
            logger.info("[霸凌] 主人豁免 user=%s 不触发沉默", user_id)
        elif _bully["is_bullying"] and _severity > 0:
            # 目标判定：问句 / 谈论第三方 / 指代别人 → 非对 bot 的攻击
            _q = (text or "").strip()
            _mention_other = re.search(
                r"(群里的人|群里的|别人|他们|那些人|大家|某位|那个谁|你说.*(?:是不是|吗|[?？]))",
                _q,
            )
            _is_question = bool(_q.endswith(("？", "?", "吗", "嘛")))
            if _is_question or _mention_other:
                _bully = {"is_bullying": False, "severity": 0, "recent_count": 0}
                _severity = 0
                _recent = 0
                logger.info("[霸凌] 非对 bot 攻击（问句/谈他者）user=%s 不沉默", user_id)
        if _bully["is_bullying"] and _severity > 0:
            try:
                from anima.pipeline.stages._shared import _detect_hurtful
                if not _detect_hurtful(text):
                    # 玩笑着说（playful 抵消）→ 压到轻级，不误判霸凌
                    _severity = 1
                    logger.info("[霸凌] playful 抵消 user=%s", user_id)
            except Exception:
                logger.debug("playful 抵消检查失败（已隔离）", exc_info=True)
        if _bully["is_bullying"]:
            from anima.cognitive.inner_state.bullying_feel import classify as _bully_classify
            _level = _bully_classify(_severity, _recent)
            ctx.meta["bullying_detected"] = True
            ctx.meta["bullying_recent_count"] = _recent
            ctx.meta["bullying_severity"] = _severity
            ctx.meta["bullying_level"] = _level   # 统一分级：mild / severe（单点）
            logger.info("[霸凌检测] user=%s level=%s recent=%d sev=%d",
                        user_id, _level, _recent, _severity)
            # L5 霸凌分级接内在（2026-08-28 点亮）：轻度只 mood 低落；
            # 严重/连续 → mood + relation 轻微降 + 记 scar。不报复不外泄。
            try:
                from anima.cognitive.inner_state.bullying_feel import apply_bullying_signal
                _dd_l5 = getattr(getattr(h.core.config, "persistence", None),
                                 "data_dir", "data")
                _l5 = apply_bullying_signal(_dd_l5, int(user_id), _severity, _recent)
                ctx.meta["bullying_feel"] = _l5["level"]
            except Exception:
                logger.debug("霸凌分级施加失败（已隔离）", exc_info=True)
    except Exception:
        logger.exception("霸凌检测异常（已隔离，不阻塞回复）")
    return StageResult.CONTINUE
