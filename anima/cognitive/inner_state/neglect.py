# -*- coding: utf-8 -*-
"""S3 冷落信号（蓝图 2026-08-27）：吸收 proactive_chat 的 unanswered 机制，
真实化为「连续主动未获回应 → 短暂 mood 低落」。

已收敛判断：
- 子判断2 选A：只造成 mood 短暂低落（几小时自然回落，走既有衰减），
  不扣 relation 长期信任、不记 scar、不污染永远记住的事实（硬事实层）。
- 子判断1：阈值偏高，N_STREAK=4（3~5 取中偏保守），避免搭话没人接就失落。
- 子判断3：只在仍允许主动的群内生效；_feedback_allows_group 判定降频/收手
  后不再叠冷落（计数保留但不施加）。
计数来自真状态（feedback stats），非扮演。表达出口=脊（S1 底色自然带出）。
"""
from __future__ import annotations
import logging
import time

logger = logging.getLogger("anima.inner_state.neglect")

N_STREAK = 4                 # 连续 N 次主动发起且未被回应才累计（阈值偏高）
IGNORE_INTENSITY = 0.18      # 单次施加的 mood 低落强度（温和、可自然回落）
COOLDOWN_SEC = 6 * 3600      # 同一群冷落施加冷却（防连环累计压垮情绪）


def maybe_neglect_dampen(group_id: int, stats: dict, allows_group: bool,
                        now: float, last_applied: dict) -> bool:
    """判定并（由调用方）施加冷落；本函数只算不写 mood，纯函数可单测。

    stats: {"engaged":.., "ignored":..}；last_applied: {gid: ts}（可变字典）。
    返回 True 表示本次应施加一次温和低落。
    """
    gid = int(group_id)
    if not allows_group:
        return False                            # 已降频/收手的群不再叠冷落
    ignored = int(stats.get("ignored", 0) or 0)
    engaged = int(stats.get("engaged", 0) or 0)
    streak = ignored - engaged                  # 近窗口连续未回应的近似
    if streak < N_STREAK:
        return False
    ts = float(last_applied.get(gid, 0.0) or 0.0)
    if ts > 0 and now - ts < COOLDOWN_SEC:
        return False                            # 冷却内不重复施加
    last_applied[gid] = now
    return True


def apply_neglect(core, group_id: int, now: float | None = None) -> bool:
    """S3 出口：读真状态 → 判定 → mood.trigger("ignored") 温和低落。

    仅短暂影响 mood（走既有衰减回路，几小时自然回落）；
    不碰 relation 长期信任、不记 scar、不写 hard_facts。
    """
    try:
        now = float(now if now is not None else time.time())
        autonomy = getattr(core, "autonomy", None)
        stats = autonomy._feedback_stats(group_id) if autonomy else None
        if stats is None:
            return False
        allows = bool(autonomy._feedback_allows_group(group_id))
        store = getattr(core, "_neglect_last_applied", None)
        if store is None:
            store = {}
            setattr(core, "_neglect_last_applied", store)
        if not maybe_neglect_dampen(group_id, stats, allows, now, store):
            return False
        from anima.cognitive.mood import get_mood_system
        data_dir = core.config.persistence.data_dir
        get_mood_system(data_dir).trigger("ignored", IGNORE_INTENSITY)
        logger.info("冷落信号: group=%s streak>= %d, mood 轻微低落(不碰信任)",
                    group_id, N_STREAK)
        audit = getattr(core, "audit", None)
        if audit:
            audit.emit("neglect_signal", group_id=int(group_id),
                       streak_baseline=N_STREAK, intensity=IGNORE_INTENSITY)
        return True
    except Exception:
        logger.exception("冷落信号施加失败（不阻塞心跳）")
        return False