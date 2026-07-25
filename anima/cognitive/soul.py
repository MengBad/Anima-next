"""灵魂能量槽系统 —— 参考 astrbot_plugin_angel_memory 的 4 维灵魂能量设计。

4 维能量值（-1.0 ~ +1.0）驱动 LLM 硬参数：
- expression_desire → max_tokens（表达欲）
- creativity → temperature（创造力）
- memory_depth → vector recall top_k（记忆深度）
- social_appetite → 主动发言概率倍率（社交食欲）

能量来源：mood、relation、desire、time_sense 等认知子系统的实时状态。
使用 EMA 平滑防止参数跳变，持久化到 data/soul.json。
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 单例管理
# ---------------------------------------------------------------------------

_instances: dict[str, "SoulSystem"] = {}


def get_soul_system(data_dir: str = "data") -> "SoulSystem":
    """获取或创建 SoulSystem 单例。"""
    key = str(Path(data_dir).resolve())
    if key not in _instances:
        _instances[key] = SoulSystem(data_dir)
    return _instances[key]


# ---------------------------------------------------------------------------
# 核心数据结构
# ---------------------------------------------------------------------------

@dataclass
class SoulState:
    """4 维灵魂能量（-1.0 ~ +1.0）。"""
    expression_desire: float = 0.0   # 表达欲 → max_tokens
    creativity: float = 0.0          # 创造力 → temperature
    memory_depth: float = 0.0        # 记忆深度 → vector recall top_k
    social_appetite: float = 0.0     # 社交食欲 → 主动发言概率倍率

    def to_dict(self) -> dict[str, float]:
        return {
            "expression_desire": round(self.expression_desire, 4),
            "creativity": round(self.creativity, 4),
            "memory_depth": round(self.memory_depth, 4),
            "social_appetite": round(self.social_appetite, 4),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SoulState":
        return cls(
            expression_desire=float(d.get("expression_desire", 0.0)),
            creativity=float(d.get("creativity", 0.0)),
            memory_depth=float(d.get("memory_depth", 0.0)),
            social_appetite=float(d.get("social_appetite", 0.0)),
        )


# ---------------------------------------------------------------------------
# 参数映射（能量 → 实际 LLM 参数）
# ---------------------------------------------------------------------------

def _tanh(x: float) -> float:
    """限制在 [-1, 1] 的 tanh。"""
    return max(-1.0, min(1.0, math.tanh(x)))


def map_max_tokens(expression_desire: float) -> int:
    """表达欲 → max_tokens: 80 (低) ~ 400 (高)。"""
    # expression_desire ∈ [-1, 1] → [80, 400]
    normalized = (expression_desire + 1.0) / 2.0  # [0, 1]
    return int(80 + normalized * 320)


def map_temperature(creativity: float) -> float:
    """创造力 → temperature: 0.5 (低) ~ 1.0 (高)。"""
    normalized = (creativity + 1.0) / 2.0  # [0, 1]
    return round(0.5 + normalized * 0.5, 3)


def map_memory_top_k(memory_depth: float) -> int:
    """记忆深度 → vector top_k: 2 (低) ~ 8 (高)。"""
    normalized = (memory_depth + 1.0) / 2.0  # [0, 1]
    return int(2 + normalized * 6)


def map_social_multiplier(social_appetite: float) -> float:
    """社交食欲 → 主动发言概率倍率: 0.3 (低) ~ 2.0 (高)。"""
    normalized = (social_appetite + 1.0) / 2.0  # [0, 1]
    return round(0.3 + normalized * 1.7, 3)


# ---------------------------------------------------------------------------
# 灵魂系统
# ---------------------------------------------------------------------------

class SoulSystem:
    """灵魂能量槽管理器。

    每次 cognition_stage 后调用 update() 重新计算能量值，
    generation_stage / memory_stage / autonomy 通过 get_params() 读取映射参数。
    """

    def __init__(self, data_dir: str = "data", ema_alpha: float = 0.3):
        self._data_dir = Path(data_dir)
        self._ema_alpha = ema_alpha
        self._state = SoulState()
        self._last_update: float = 0.0
        self._history: list[dict[str, Any]] = []  # 最近 24h 快照
        self._load()

    @property
    def state(self) -> SoulState:
        return self._state

    def update(
        self,
        mood_valence: float = 0.0,
        mood_arousal: float = 0.0,
        relation_intimacy: float = 0.0,
        relation_familiarity: float = 0.0,
        desire_pressure: float = 0.0,
        desire_chat_intensity: float = 0.0,
        quirk_active: bool = False,
        flashbulb_recent: bool = False,
        topic_relevance: float = 0.0,
        group_atmosphere: float = 0.0,
        time_novelty: float = 0.0,
    ) -> SoulState:
        """根据认知子系统状态计算新的能量值（EMA 平滑）。"""
        # 原始计算
        raw_expression = _tanh(
            mood_arousal * 0.6 + relation_intimacy * 0.3 + desire_pressure * 0.1
        )
        raw_creativity = _tanh(
            mood_valence * 0.4 + (0.8 if quirk_active else 0.0) * 0.4 + time_novelty * 0.2
        )
        raw_memory = _tanh(
            relation_familiarity * 0.5 + (0.7 if flashbulb_recent else 0.0) * 0.3 + topic_relevance * 0.2
        )
        raw_social = _tanh(
            desire_chat_intensity * 0.4 + group_atmosphere * 0.3 + mood_valence * 0.3
        )

        # EMA 平滑
        alpha = self._ema_alpha
        self._state.expression_desire = (
            alpha * raw_expression + (1 - alpha) * self._state.expression_desire
        )
        self._state.creativity = (
            alpha * raw_creativity + (1 - alpha) * self._state.creativity
        )
        self._state.memory_depth = (
            alpha * raw_memory + (1 - alpha) * self._state.memory_depth
        )
        self._state.social_appetite = (
            alpha * raw_social + (1 - alpha) * self._state.social_appetite
        )

        self._last_update = time.time()

        # 记录历史快照（保留 24h，每分钟最多一条）
        self._record_snapshot()

        return self._state

    def get_params(self) -> dict[str, Any]:
        """返回映射后的实际 LLM 参数。"""
        return {
            "max_tokens": map_max_tokens(self._state.expression_desire),
            "temperature": map_temperature(self._state.creativity),
            "memory_top_k": map_memory_top_k(self._state.memory_depth),
            "social_multiplier": map_social_multiplier(self._state.social_appetite),
        }

    def describe_state(self) -> str:
        """生成当前灵魂能量的自然语言描述，注入 system prompt 让 LLM 自然调整语气。

        只在能量显著偏离中性时输出（|v| > 0.3），避免无意义噪声。
        返回空字符串表示“无需注入”。
        """
        s = self._state
        cues: list[str] = []
        # 表达欲
        if s.expression_desire > 0.35:
            cues.append("你现在表达欲很强，特别想多说几句")
        elif s.expression_desire < -0.35:
            cues.append("你现在不太想说话，回复会比较简短")
        # 创造力
        if s.creativity > 0.35:
            cues.append("脑洞很大，容易联想到奇怪的东西")
        elif s.creativity < -0.35:
            cues.append("思维比较平淡，不太想发散")
        # 社交食欲
        if s.social_appetite > 0.35:
            cues.append("特别想跟人聊天，对对话很感兴趣")
        elif s.social_appetite < -0.35:
            cues.append("有点想独处，对聊天提不起太大劲")
        # 记忆深度
        if s.memory_depth < -0.35:
            cues.append("记性不太好，想不太起来之前的事")
        if not cues:
            return ""
        return "\n## 当前内心状态\n" + "，".join(cues[:3]) + "。自然地体现这一点，不用宣告。"

    def get_status(self) -> dict[str, Any]:
        """WebUI 状态接口。"""
        params = self.get_params()
        return {
            "state": self._state.to_dict(),
            "params": params,
            "last_update": self._last_update,
            "history_count": len(self._history),
        }

    def get_history(self, hours: float = 24.0) -> list[dict[str, Any]]:
        """获取最近 N 小时的能量变化历史。"""
        cutoff = time.time() - hours * 3600
        return [h for h in self._history if h.get("ts", 0) > cutoff]

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _persist_path(self) -> Path:
        return self._data_dir / "soul.json"

    def _load(self) -> None:
        path = self._persist_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._state = SoulState.from_dict(data.get("state", {}))
                self._history = data.get("history", [])[-1440:]  # 最多保留 1440 条
                self._last_update = data.get("last_update", 0.0)
            except Exception:
                logger.exception("加载 soul.json 失败，使用默认值")

    def save(self) -> None:
        """原子持久化到磁盘（tmp + fsync + replace，防止崩溃时文件损坏）。"""
        import os
        import tempfile
        path = self._persist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "state": self._state.to_dict(),
            "last_update": self._last_update,
            "history": self._history[-1440:],
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".soul_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, str(path))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except Exception:
            logger.exception("保存 soul.json 失败")

    def _record_snapshot(self) -> None:
        """记录快照（每分钟最多一条）。"""
        now = time.time()
        if self._history and now - self._history[-1].get("ts", 0) < 60:
            return
        snapshot = self._state.to_dict()
        snapshot["ts"] = now
        self._history.append(snapshot)
        # 清理超过 24h 的
        cutoff = now - 86400
        self._history = [h for h in self._history if h.get("ts", 0) > cutoff]
        # 定期持久化
        if len(self._history) % 10 == 0:
            self.save()
