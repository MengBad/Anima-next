"""阶段 2: 记忆注入。"""

from __future__ import annotations
import logging
import time

from anima.pipeline import StageResult, PipelineContext
from ._shared import _clean_transport_text, _PROMISE_RE

logger = logging.getLogger("anima.pipeline.stages")


async def memory_stage(ctx: PipelineContext) -> StageResult:
    """注入短期对话记忆 + 长期向量记忆到 LLM 上下文。"""
    from anima.memory.chat_memory import get_chat_memory

    core = ctx.meta.get("core")

    ctx.text = _clean_transport_text(ctx.text)
    if not ctx.text and ctx.inbound_images:
        ctx.text = "[用户发送了图片]"
    if not ctx.text and not ctx.inbound_images:
        return StageResult.SKIP_REPLY

    memory = get_chat_memory()
    chat_id = ctx.group_id if ctx.detail_type == "group" else ctx.user_id
    history = memory.get_context(
        ctx.detail_type, chat_id, recent_rounds=8, user_id=ctx.user_id
    )
    if ctx.detail_type == "group" and ctx.group_id is not None:
        # 群短时缓冲只用于当前群近期可见消息；与个人档案去重后共同构成上下文。
        group_history = memory.get_recent_group_context(
            ctx.group_id, limit=12, exclude_message_id=ctx.event.get("message_id"),
        )
        seen = {(m.get("role"), m.get("content")) for m in group_history}
        history = [*group_history, *[m for m in history if (m.get("role"), m.get("content")) not in seen]]

    if history:
        ctx.messages.extend(history)

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
                for h in hits:
                    memory_lines.append(f"- {h['text'][:600]}")
                    # Q2: 检查记忆时间戳
                    h_ts = h.get("ts") or h.get("created_at") or 0
                    if h_ts and (time.time() - float(h_ts)) > 7 * 86400:
                        _has_old_memory = True
                # Q2: 久远记忆追加"想起"口吻提示
                if _has_old_memory:
                    memory_lines.append(
                        "（这是比较久远的记忆了，提起时可以用'本喵记得之前…'这种回忆口吻）"
                    )
                ctx.meta["vector_recall"] = "\n".join(memory_lines)
        except Exception:
            logger.exception("向量记忆召回失败")

    episodes = getattr(core, "episodes", None) if core else None
    if episodes is not None:
        try:
            episode_context = episodes.context(ctx.detail_type, ctx.group_id, ctx.user_id)
            if episode_context:
                ctx.meta["episodic_context"] = episode_context
        except Exception:
            logger.exception("情节记忆召回失败")

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
