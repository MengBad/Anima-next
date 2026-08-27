# -*- coding: utf-8 -*-
"""S7 私密情绪层（蓝图 2026-08-27 选A）：secret 接进内省/自述的隐私版。

已收敛判断：
- 秘密在「目标用户 relation 够深（FRIEND/CLOSE）+ 被问（内心/秘密/心事类）
  + 语境自然」时才流露；否则维持隐式影响不直说。
- 与 S4 共享隐私边界单源：复用 qzone_life.sanitize_for_public 的敏感判定，
  不双头（不重复发明）。
"""
from __future__ import annotations
import logging
import re
from typing import Optional

logger = logging.getLogger("anima.inner_state.secret_feel")

_CLOSE_STAGES = {"FRIEND", "CLOSE"}

_ASK_RE = re.compile(
    r"你.{0,10}(秘密|心事|藏|隐私)|有什么(秘密|心事|心里话)|跟我?说说?(你的)?(心里话|心事|秘密)")


def detect_secret_inquiry(text: str) -> bool:
    return bool(_ASK_RE.search((text or "").strip()[:60]))


def _relation_stage(data_dir: str, user_id: int) -> str:
    try:
        from anima.cognitive.relation import get_relation_engine
        rel = get_relation_engine(data_dir).get(int(user_id))
        return (getattr(getattr(rel, "stage", None), "name", "") or "").upper()
    except Exception:
        return ""


def pick_shareable_secret(data_dir: str, user_id: int) -> Optional[dict]:
    """选一条可对亲近者流露的秘密：优先「针对该用户」的，其次全局轻微的。

    语境自然 + 深关系门槛在此之外由 build 判。
    """
    try:
        from anima.memory.secrets import SecretsStore
        store = SecretsStore(data_dir)
        for_target_uid = str(user_id)
        targeted = [s for s in store.for_target(for_target_uid)
                    if not getattr(s, "revealed", False)]
        if targeted:
            s = targeted[0]
            return {"content": getattr(s, "content", ""), "targeted": True}
        # 全局轻微秘密必须「无明确归属」——针对他人的秘密绝不外泄给第三者
        light = [s for s in store.all()
                if not getattr(s, "revealed", False)
                and not str(getattr(s, "target_uid", "") or "").strip()
                and str(getattr(s, "influence", "")) in ("subtle", "轻微", "")]
        if light:
            s = light[0]
            return {"content": getattr(s, "content", ""), "targeted": False}
    except Exception:
        logger.debug("选秘密失败", exc_info=True)
    return None


def build_secret_feel_fragment(data_dir: str, user_id: int, text: str):
    """S7 出口：深关系 + 被问 + 有可流露秘密 → 内省隐私版片段；否则 None。"""
    if not detect_secret_inquiry(text):
        return None
    if _relation_stage(data_dir, user_id) not in _CLOSE_STAGES:
        return None
    secret = pick_shareable_secret(data_dir, user_id)
    if not secret or not (secret.get("content") or "").strip():
        return None
    # 隐私边界单源：与 S4 同一 sanitize 判定（含敏感字面/secret提取式）
    from anima.cognitive.inner_state.qzone_life import sanitize_for_public
    safe, ok = sanitize_for_public(str(secret["content"]))
    if not ok:
        return None
    targeted = "这个人正是秘密里的主角" if secret["targeted"] else "这是你自己藏着的一件小事"
    head = "（亲近的人轻声问起了你的心事。可以流露一条真实的小秘密，第一人称、点到为止、说完就收。）"
    return head + targeted + "：" + safe[:160]


# ---------------------------------------------------------------------------
# L1 意外交心（2026-08-28 点亮）：未被问时，亲近日久 + 气氛自然 + 低频概率
# → 自然带出一句「无归属轻微秘密」。语气羞赧、点到就收。绝不外泄针对他人。
# ---------------------------------------------------------------------------
_SPONTANEOUS_PROB = 0.02       # ~2% 触发概率
_SPONTANEOUS_DAILY_MAX = 1     # 日上限 1 次
_last_spontaneous: dict[str, int] = {}   # {date_str: count}，进程内即可(不新增存储)


def pick_unattributed_secret(data_dir: str, user_id: int) -> Optional[dict]:
    """只取「无明确归属」的轻微秘密（意外交心专用）。

    针对他人的秘密（target_uid 非空）一律跳过——绝不外泄。
    """
    try:
        from anima.memory.secrets import SecretsStore
        store = SecretsStore(data_dir)
        light = [s for s in store.all()
                if not getattr(s, "revealed", False)
                and not str(getattr(s, "target_uid", "") or "").strip()
                and str(getattr(s, "influence", "")) in ("subtle", "轻微", "")]
        if light:
            s = light[0]
            return {"content": getattr(s, "content", ""), "targeted": False}
    except Exception:
        logger.debug("选无归属秘密失败", exc_info=True)
    return None


def maybe_spontaneous_confide(data_dir: str, user_id: int, rng=None):
    """意外交心被动层：返回一句自然流露片段，或 None。

    - FRIEND/CLOSE 才可能触发；
    - 概率 ~2%（rng 可注入便于测试）；
    - 日上限 1 次（进程内计数，日期切换自动归零）；
    - 只取无归属轻微秘密，语气羞赧点到就收。
    """
    import datetime
    import random
    stage = _relation_stage(data_dir, user_id)
    if stage not in _CLOSE_STAGES:
        return None
    rng = rng or random
    if rng.random() >= _SPONTANEOUS_PROB:
        return None
    today = datetime.date.today().isoformat()
    if _last_spontaneous.get(today, 0) >= _SPONTANEOUS_DAILY_MAX:
        return None
    secret = pick_unattributed_secret(data_dir, user_id)
    if not secret or not (secret.get("content") or "").strip():
        return None
    from anima.cognitive.inner_state.qzone_life import sanitize_for_public
    safe, ok = sanitize_for_public(str(secret["content"]))
    if not ok:
        return None
    _last_spontaneous[today] = _last_spontaneous.get(today, 0) + 1
    logger.info("意外交心: user=%s 自然带出一句无归属秘密", user_id)
    head = "（你们已经聊得很熟。你突然有点没忍住，轻声说了一件自己藏着的小事，说完有点不好意思地收住。）"
    return head + safe[:160]