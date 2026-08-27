# -*- coding: utf-8 -*-
"""#2+#3 情绪记忆连续性 + 遗忘可感知（2026-08-28 路P+甲 同族）。

- 路P：复用 soul.get_history() 24h 能量轨迹（不新增 mood 历史存储），
  从能量角度带出「最近提不起劲 / 还挺活跃」。
- 甲：低频轻表露，点到为止、不阴沉、不反复、不叙事化。
- #3：遗忘可感知——记忆引擎不动（现状已够），只补「淡化/有点记不清」表露，
  判据 = world_model 存在 contested（被挑战）或低权重条目。
"""
from __future__ import annotations
import random
from typing import Optional

_ENERGY_LOW = -0.15
_ENERGY_HIGH = 0.15
_CONTINUITY_PROB = 0.04   # 低频，避免过度自省/矫情
_FADING_PROB = 0.03


def recent_energy_tone(data_dir: str, hours: float = 24.0) -> Optional[str]:
    """读 soul 24h 能量轨迹，返回 low/high/None(fail-closed)。纯函数可单测。"""
    try:
        from anima.cognitive.soul import get_soul_system
        hist = get_soul_system(str(data_dir)).get_history(hours)
    except Exception:
        return None
    if len(hist) < 3:
        return None   # 轨迹不足给中性
    # 社交食欲 + 表达欲两维的近期均值（能量「想不想说话/活跃度」）
    def _avg(key: str) -> float:
        vals = [float(h.get(key, 0.0) or 0.0) for h in hist[-24:]]
        return sum(vals) / len(vals) if vals else 0.0
    tone = _avg("social_appetite") * 0.6 + _avg("expression_desire") * 0.4
    if tone <= _ENERGY_LOW:
        return "low"
    if tone >= _ENERGY_HIGH:
        return "high"
    return None


def has_fading_belief(data_dir: str) -> bool:
    """#3：world_model 存在 contested 或低权重（接近下限）条目 → 可偶带「淡了」。"""
    try:
        from anima.cognitive.world_model import get_world_model
        wm = get_world_model(str(data_dir));
        entries = wm.get_entries(min_weight=0.0) if hasattr(wm, "get_entries") else []
        for e in entries:
            if getattr(e, "contested", False):
                return True
            if float(getattr(e, "weight", 0.5) or 0.5) <= 0.18:
                return True
        return False
    except Exception:
        return False


def continuity_fragment(data_dir: str, user_id=None, rng=None) -> Optional[str]:
    """低频轻表露：偶带「最近状态」一句；无则 None(fail-closed)。"""
    rng = rng or random
    if rng.random() >= _CONTINUITY_PROB:
        return None
    tone = recent_energy_tone(data_dir);
    if tone == "low":
        return "（最近好像有点提不起劲，话都少了些，但也没多大事。）"
    if tone == "high":
        return "（最近精神头还挺足，有点想跟人唠几句。）"
    return None


def fading_fragment(data_dir: str, user_id=None, rng=None) -> Optional[str]:
    """#3：偶带「有点记不清了/淡了」，体现时间流逝可感知。低频、点到为止。"""
    rng = rng or random
    if rng.random() >= _FADING_PROB:
        return None
    if not has_fading_belief(data_dir):
        return None
    return "（有件事在脑子里有点淡了，一时也说不清具体是什么，就让它慢慢模糊吧。）"