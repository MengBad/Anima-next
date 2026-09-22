"""阶段 2: 记忆注入。"""

from __future__ import annotations
import json as _json
import logging
import re
import time
from pathlib import Path

from anima.pipeline import StageResult, PipelineContext
from ._shared import _clean_transport_text, _PROMISE_RE

logger = logging.getLogger("anima.pipeline.stages")


# R2-A: credential-like patterns withheld from LLM context (never injected,
# never logged in plaintext). Conservative by design: prefer over-filtering
# a rare false positive over leaking a real credential into the context.
_SECRET_PATTERNS = (
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", re.I),
    re.compile(r"(?:token|secret|passwd|pwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./=]{8,}", re.I),
    re.compile(r"password\s*[:=]\s*['\"]?[A-Za-z0-9_\-./=]{8,}", re.I),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{10,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}", re.I),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}", re.I),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}", re.I),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
)


def _contains_secret(text: str) -> bool:
    """True if text carries a credential-like pattern (API key / token / password)."""
    if not text:
        return False
    return any(p.search(text) for p in _SECRET_PATTERNS)


# --- P0-2：长期记忆写入筛选 -------------------------------------------------
# 只有「偏好/决策句」和 Owner 私聊才值得沉淀进 LongTerm。若把每条闲聊都写盘，
# knowledge / patterns 会被噪音淹没，query 命中质量反而下降（这是 long_term_context
# 原先默认关闭的实际原因之一）。
_PREF_HINTS = (
    "我喜欢", "我讨厌", "我不喜欢", "我偏好", "我习惯", "我一般", "我通常",
    "我希望", "我要你", "你以后", "你别", "你不要", "你最好",
    "以后都", "以后别", "以后就", "记得我", "记住我", "别再", "不要再",
    "默认就", "默认用", "总是要", "每次都", "我的原则是", "就这样定", "定下来",
)


def _is_preference_text(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and any(h in t for h in _PREF_HINTS)


def _is_owner_private(ctx) -> bool:
    """Owner 私聊：Owner 的话权重最高，一律沉淀（无论是否像偏好句）。"""
    try:
        if str(getattr(ctx, "detail_type", "") or "") != "private":
            return False
        meta = getattr(ctx, "meta", None) or {}
        if meta.get("is_owner"):
            return True
        return int(meta.get("permission_level", 0) or 0) >= 4  # LEVEL_OWNER
    except Exception:
        return False


def _long_term_should_write(core, ctx, text: str) -> bool:
    mem_cfg = getattr(getattr(core, "config", None), "memory", None)
    mode = str(getattr(mem_cfg, "long_term_write", "preference") or "preference").strip().lower()
    if mode == "off":
        return False
    if mode == "all":
        return True
    # preference（默认）
    return _is_owner_private(ctx) or _is_preference_text(text)


def _long_term_query(core, text: str):
    """④ long_term 第三层查询：memory.long_term_context 开启且有命中时返回结果，否则 None（fail-closed）。"""
    try:
        cfg = getattr(core, "config", None)
        mem_cfg = getattr(cfg, "memory", None)
        if not (mem_cfg and getattr(mem_cfg, "long_term_context", False)):
            return None
        data_dir = str(getattr(getattr(cfg, "persistence", None), "data_dir", "data"))
        lt = getattr(core, "long_term", None)
        if lt is None:
            from anima.memory.long_term import LongTermMemory
            lt = LongTermMemory(data_dir)
            core.long_term = lt
        res = lt.query(text, limit=6)
        if res.get("has_data") and (res.get("knowledge_matches") or res.get("pattern_matches")):
            return res
        return None
    except Exception:
        logger.debug("long_term 接线读取失败，隔离")
        return None


def _memory_audit(ctx: PipelineContext, event: str, reason: str = "") -> None:
    """P10.4-R2-A: memory_read audit sink (subject hash only, never plaintext)."""
    try:
        from anima.pilot.conversation_evidence import hash_user_id

        core = ctx.meta.get("core")
        cfg = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(cfg, "data_dir", "data") if cfg else "data"
        path = Path(data_dir) / "logs" / "memory_read_audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "ts": time.time(),
                "event": f"memory_read_{event}",
                "subject_hash": hash_user_id(str(ctx.user_id)),
                "reason": reason[:120],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass  # audit must never break the pipeline


async def memory_stage(ctx: PipelineContext) -> StageResult:
    # 收敛计划 M2：统一记忆检索聚合片段（只读，flag 默认关）
    try:
        core_m2 = ctx.meta.get("core")
        if (core_m2 is not None
                and getattr(getattr(core_m2.config, "memory", None), "unified_recall_enabled", False)):
            from . import _shared as _sh
            agg = await _sh.unified_recall(
                core_m2, ctx.text or "", group_id=ctx.group_id, user_id=ctx.user_id)
            lines = [f"- [{k}] {v}" for k, vs in agg.items() for v in vs if v]
            if lines:
                ctx.meta["unified_memory_recall"] = "\n".join(lines)[:1200]
    except Exception:
        pass
    """注入短期对话记忆 + 长期向量记忆到 LLM 上下文。"""
    from anima.memory.chat_memory import get_chat_memory

    core = ctx.meta.get("core")

    ctx.text = _clean_transport_text(ctx.text)
    if not ctx.text and ctx.inbound_images:
        ctx.text = "[用户发送了图片]"
    if not ctx.text and not ctx.inbound_images:
        return StageResult.SKIP_REPLY

    _memory_audit(ctx, "requested")

    memory = get_chat_memory()
    chat_id = ctx.group_id if ctx.detail_type == "group" else ctx.user_id
    # 翻旧账防护（P1）：持久桶只注入最近 30 分钟、最多 15 条；更早的话题视为已结束，
    # 不进 LLM 上下文，Bot 不会把几小时前的旧对话当成「仍在继续」而自顾自辩护。
    history = memory.get_context(
        ctx.detail_type, chat_id, recent_rounds=8, user_id=ctx.user_id,
        max_age_minutes=30, max_messages=15,
    )
    if ctx.detail_type == "group" and ctx.group_id is not None:
        # 群短时缓冲只用于当前群近期可见消息；与个人档案去重后共同构成上下文。
        # annotate=True（本处**唯一**开启的消费者）：群缓冲是群级共享的，同群不同用户
        # 并发 @bot 时，缓冲里会留下「别人问 bot、其后没有任何 assistant 回复」的 user turn。
        # `exclude_message_id` 只排掉"自己这条"，于是它就成了一个**没有标记、与本轮同形**
        # 的待答提问 ⇒ 模型被诱导去答别人那条（2026-09-21 事故）。标注后模型能分辨"该答哪条"。
        group_history = memory.get_recent_group_context(
            ctx.group_id, limit=12, exclude_message_id=ctx.event.get("message_id"),
            annotate=True,
        )
        seen = {(m.get("role"), m.get("content")) for m in group_history}
        history = [*group_history, *[m for m in history if (m.get("role"), m.get("content")) not in seen]]

    if history:
        _secret_hits = [m for m in history if _contains_secret(m.get("content", ""))]
        if _secret_hits:
            _memory_audit(ctx, "secret_filtered", f"chat_memory:{len(_secret_hits)}")
            history = [m for m in history if not _contains_secret(m.get("content", ""))]
        if history:
            ctx.messages.extend(history)
    _memory_audit(ctx, "allowed", "chat_memory")

    # 向量记忆召回（长期记忆）—— D1: 灵魂记忆深度驱动 top_k
    if core and core.vector_memory:
        try:
            _soul_params_mem = ctx.meta.get("soul_params") or {}
            _mem_top_k = _soul_params_mem.get("memory_top_k", 3)
            hits = await core.vector_memory.search(
                ctx.text, top_k=_mem_top_k, threshold=0.70,
                source_uin=ctx.user_id,
                source_gid=ctx.group_id if ctx.detail_type == "group" else None,
                scoped=True,
            )
            if hits:
                memory_lines = ["## 相关记忆"]
                _has_old_memory = False  # Q2: 检测是否有久远记忆
                _secret_filtered_n = 0
                for h in hits:
                    _h_text = str(h.get("text", ""))
                    if _contains_secret(_h_text):
                        _secret_filtered_n += 1
                        continue
                    memory_lines.append(f"- {_h_text[:600]}")
                    # Q2: 检查记忆时间戳
                    h_ts = h.get("ts") or h.get("created_at") or 0
                    if h_ts and (time.time() - float(h_ts)) > 7 * 86400:
                        _has_old_memory = True
                if _secret_filtered_n:
                    _memory_audit(ctx, "secret_filtered", f"vector_recall:{_secret_filtered_n}")
                # Q2: 久远记忆追加"想起"口吻提示
                if _has_old_memory:
                    memory_lines.append(
                        "（这是比较久远的记忆了，提起时可以用'本喵记得之前…'这种回忆口吻）"
                    )
                if len(memory_lines) > 1:
                    ctx.meta["vector_recall"] = "\n".join(memory_lines)
        except Exception:
            _memory_audit(ctx, "blocked", "vector_search_failed")
            logger.exception("向量记忆召回失败")

    episodes = getattr(core, "episodes", None) if core else None
    if episodes is not None:
        try:
            episode_context = episodes.context(ctx.detail_type, ctx.group_id, ctx.user_id)
            if episode_context:
                ctx.meta["episodic_context"] = episode_context
        except Exception:
            logger.exception("情节记忆召回失败")

    # ④ long_term 第三层：开启时注入事件→知识→行为模式查询命中（默认关，fail-closed）
    try:
        _ltv = _long_term_query(core, ctx.text)
        if _ltv:
            ctx.meta["long_term_context"] = _ltv
    except Exception:
        logger.debug("long_term 上下文注入失败，隔离")

    # 记录当前用户消息（纯图片消息用占位文本，视觉描述由后台 _observe_vision 补写）
    # B2: 闪光灯记忆——高唤醒状态时自动标记
    _flashbulb = False
    try:
        from anima.cognitive.mood import get_mood_system
        _data_dir_fb = getattr(getattr(core, "config", None), "persistence", None)
        _data_dir_fb = getattr(_data_dir_fb, "data_dir", "data") if _data_dir_fb else "data"
        _mood_state = get_mood_system(_data_dir_fb).get_state()
        if _mood_state.arousal > 0.6:
            _flashbulb = True
    except Exception:
        logger.debug("闪光灯情绪检测失败", exc_info=True)
    ctx.meta["_flashbulb"] = _flashbulb

    # 群聊消息已在 observe_group_message 中持久化到个人桶，这里只补记私聊。
    if ctx.detail_type != "group":
        memory.add(
            ctx.detail_type, chat_id, "user", ctx.sender_name,
            ctx.text or "[用户发送了图片]",
            user_id=ctx.user_id,
        )
    if episodes is not None:
        try:
            agenda_factory = None
            if core is not None and hasattr(core, "propose_agenda") and ctx.detail_type == "group" and ctx.group_id:
                agenda_factory = core.propose_agenda
            episodes.observe(
                ctx.detail_type, ctx.group_id, ctx.user_id, "user", ctx.text,
                has_image=bool(ctx.inbound_images),
                agenda_factory=agenda_factory,
            )
        except Exception:
            logger.exception("情节记忆写入失败")

    # ④b long_term 第三层写入：仅偏好/决策句（或 Owner 私聊）沉淀，闲聊不入库
    try:
        _lt_memcfg = getattr(getattr(core, "config", None), "memory", None)
        _lt_text = (ctx.text or "").strip()
        if (_lt_memcfg is not None and getattr(_lt_memcfg, "long_term_context", False)
                and _lt_text and len(_lt_text) >= 2 and not _contains_secret(_lt_text)
                and _long_term_should_write(core, ctx, _lt_text)):
            _lt = getattr(core, "long_term", None)
            if _lt is None:
                from anima.memory.long_term import LongTermMemory
                _lt_dir = getattr(getattr(core, "config", None), "persistence", None)
                _lt_dir = getattr(_lt_dir, "data_dir", "data") if _lt_dir else "data"
                _lt = LongTermMemory(str(_lt_dir))
                core.long_term = _lt
            _tags = ["conversation", "preference"]
            if bool(ctx.inbound_images):
                _tags.append("with_image")
            _ev = getattr(_lt, "record_event")("conversation", _lt_text[:400], tags=_tags)
            # 同步抽到知识层：只写 events 的话 query() 的 knowledge_matches 永远为空
            getattr(_lt, "extract_and_store", lambda e: [])(_ev)
    except Exception:  # noqa: BLE001 - 长期记忆写入失败不阻断流水线
        logger.debug("long_term 写入失败, 隔离")
    ctx.meta["chat_id"] = chat_id

    # 写入向量记忆（用户消息）—— Q3: 动态 salience
    if core and core.vector_memory:
        try:
            # Q3: 情节记忆 salience 动态计算
            _base_salience = 0.5
            # 情绪事件加权
            try:
                from anima.cognitive.mood import get_mood_system
                _data_dir = getattr(getattr(core, "config", None), "persistence", None)
                _data_dir = getattr(_data_dir, "data_dir", "data") if _data_dir else "data"
                _mood_lbl = get_mood_system(_data_dir).get_state().label
                if _mood_lbl in ("excited", "annoyed"):
                    _base_salience += 0.15
            except Exception:
                logger.debug("情绪显著度加权失败", exc_info=True)
            # 关系重要人加权
            try:
                from anima.cognitive.relation import get_relation, Stage as _Stage
                _rel = get_relation().get(int(ctx.user_id))
                if _rel.stage in (_Stage.CLOSE, _Stage.FRIEND):
                    _base_salience += 0.1
            except Exception:
                logger.debug("关系显著度加权失败", exc_info=True)
            # 含承诺/约定加权
            if _PROMISE_RE.search(ctx.text):
                _base_salience += 0.2
            # 短消息/水聊降权
            if len(ctx.text) < 8:
                _base_salience -= 0.15
            # B2: 闪光灯记忆——高唤醒事件 salience 大幅提升
            if _flashbulb:
                _base_salience += 0.3
            _base_salience = max(0.1, min(1.0, _base_salience))

            await core.vector_memory.add(
                text=ctx.text,
                meta={"role": "user", "name": ctx.sender_name, "chat_type": ctx.detail_type, "chat_id": chat_id},
                salience=_base_salience,
                source_uin=ctx.user_id,
                source_gid=ctx.group_id,
            )
        except Exception:
            logger.exception("向量记忆写入失败")

    return StageResult.CONTINUE
