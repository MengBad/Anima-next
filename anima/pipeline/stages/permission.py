"""阶段 1: 权限校验。"""

from __future__ import annotations
import logging
from typing import Optional

from anima.core.config import AnimaConfig
from anima.pipeline import StageResult, PipelineContext
from ._shared import _clean_transport_text  # noqa: F401 (re-export for compat)

logger = logging.getLogger("anima.pipeline.stages")

PERMISSION_LEVELS = {
    "owner": 4,
    "admin": 3,
    "user": 2,
    "readonly": 1,
    "banned": 0,
}


def _check_permission(
    user_id: int,
    group_id: Optional[int],
    config: AnimaConfig,
) -> tuple[int, str]:
    """返回 (level, role_name)。

    授权模型（M0.4.3 S1 锁定）：
        QQ UID -> config.permissions -> explicit grant -> level

    唯一授权主体是 QQ UID，且必须由主人显式授予。
    QQ 群角色（sender.role = owner/admin）**不参与任何授权判定**：
    群主 != bot owner，群管理员 != bot admin。群身份仅属于聊天上下文，
    由 permission_stage 单独写入 ctx.meta["group_sender_role"] 供人格/
    对话层参考，不得回流到 permission_level。

    群维度配置只能**收紧**（banned_groups / readonly_groups），
    永远不能提升权限。
    """
    # 用户维度权限优先于群维度
    if user_id in config.permissions.banned:
        return 0, "banned"
    if user_id in config.permissions.readonly:
        return 1, "readonly"
    if user_id in config.permissions.owners:
        return 4, "owner"
    if user_id in config.permissions.admins:
        return 3, "admin"

    # 群维度封禁 / 只读（仅收紧，不提升）
    if group_id is not None:
        if group_id in config.permissions.banned_groups:
            return 0, "banned"
        if group_id in config.permissions.readonly_groups:
            return 1, "readonly"

    # 未显式授权的 UID 一律为普通用户，无论其 QQ 群角色为何
    return 2, "user"


def _group_policy_for(
    config: AnimaConfig, group_id: Optional[int]
) -> dict:
    """获取指定群的策略；未配置则返回空 dict（使用全局默认值）。"""
    if group_id is None:
        return {}
    return config.permissions.group_policies.get(str(group_id), {})


async def permission_stage(ctx: PipelineContext) -> StageResult:
    """权限校验 —— 检查用户是否有发言权限（含 QQ 群角色与群策略）。"""
    from anima.cognitive.time_sense import get_time_sense

    core = ctx.meta.get("core")
    if core is None:
        return StageResult.CONTINUE

    level, role = _check_permission(ctx.user_id, ctx.group_id, core.config)

    if role == "banned":
        ctx.stop_reason = f"用户 {ctx.user_id} 被封禁"
        logger.info("Permission: BLOCK banned user=%d", ctx.user_id)
        return StageResult.STOP

    if role == "readonly":
        ctx.should_reply = False
        ctx.stop_reason = f"用户 {ctx.user_id} 只读"
        logger.info("Permission: readonly user=%d, 不回复", ctx.user_id)
        return StageResult.SKIP_REPLY

    ctx.meta["permission_level"] = level
    ctx.meta["permission_role"] = role
    ctx.meta["group_policy"] = _group_policy_for(core.config, ctx.group_id)

    # QQ 群角色：仅作为聊天上下文元数据保留（人格/称呼/社交图谱可用），
    # 绝不参与授权。命名刻意与 permission_role 区分，防止误用。
    ctx.meta["group_sender_role"] = (
        ctx.event.get("raw", {}).get("sender", {}).get("role", "")
    )

    # 更新 time sense
    time_sense = get_time_sense()
    time_sense.touch(ctx.user_id)

    # E1: 群内社交图谱——检测 @和引用回复，记录互动关系
    if ctx.detail_type == "group" and ctx.group_id is not None:
        try:
            from anima.cognitive.social_graph import get_social_graph
            _sg = get_social_graph()
            _raw = ctx.event.get("raw", {})
            _msg = _raw.get("message", "")
            # 检测 @ 某人的 QQ 号
            import re as _re_at
            _at_uids = _re_at.findall(r"\[CQ:at,qq=(\d+)\]", _msg)
            for target_uid in _at_uids:
                if str(target_uid) != str(ctx.user_id):
                    _sg.observe_interaction(
                        str(ctx.group_id), str(ctx.user_id), str(target_uid))
            # 检测引用回复
            reply_uid = _raw.get("reply_to", {}).get("sender", {}).get("user_id")
            if reply_uid and str(reply_uid) != str(ctx.user_id):
                _sg.observe_interaction(
                    str(ctx.group_id), str(ctx.user_id), str(reply_uid), weight=1.5)
        except Exception:
            logger.debug("社交图观测失败", exc_info=True)

    return StageResult.CONTINUE
