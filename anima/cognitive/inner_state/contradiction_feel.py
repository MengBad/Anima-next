# -*- coding: utf-8 -*-
"""S8 矛盾感知（蓝图 2026-08-27）：矛盾从「界面摆设」接成「它心里的感知」。

已收敛判断（选 A，非 S6）：
- 检测到用户前后矛盾 → 私人记一笔（由 contradiction 模块负责）+ relation 轻微
  负向扰动（这里负责）+ 经脊在有戏派语境流露（S1/S2 底色自然承载）。
- 非矛盾不误报：只入口在有记录的矛盾事件后调用，本身不重判。
- severity：轻；只降 trust/亲密度一档内，绝不越严重档。
"""
from __future__ import annotations
import logging

logger = logging.getLogger("anima.inner_state.contradiction_feel")

TRUST_PENALTY = 0.05      # 单次轻微负向扰动（一档内，绝不过度）
REL_FLOOR = 0.2           # 信任下限：不让一次矛盾击穿既有信任


def feel_user_contradiction(data_dir: str, user_id: int) -> bool:
    """矛盾事件后，对 relation 做轻微负向扰动（擦到就行）。

    由 contradiction 模块在记录一笔矛盾后调用；这里只做扰动 + audit，
    不落私人记录（那已由 contradiction 完成），不重判是否真矛盾（入口即事实）。
    """
    try:
        from anima.cognitive.relation import get_relation_engine
        eng = get_relation_engine(data_dir)
        rel = eng.get(int(user_id))
        trust = float(getattr(rel, "trust", 0.5) or 0.5)
        try:
            rel.trust = max(REL_FLOOR, trust - TRUST_PENALTY)
        except Exception:
            rel.trust = trust                     # 只读守卫：读不回写则不改
        logger.info("矛盾感知: user=%s 信任轻微扰动(%.2f->%.2f)",
                    user_id, trust, max(REL_FLOOR, trust - TRUST_PENALTY))
        return True
    except Exception:
        logger.debug("矛盾扰动失败(无此用户/关系不存在)", exc_info=True)
        return False
