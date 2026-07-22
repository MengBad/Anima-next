"""伤痕系统 —— 记录负面经验，影响信任、情绪与行为边界。

设计：
- 记录伤害事件：谁、什么时候、做了什么、伤害类型
- 伤痕会降低对相关用户的信任，影响关系阶段
- 高权重伤痕会在 system prompt 中体现为「敏感点」
- 随时间淡化，但严重伤痕会留下长期印记
- 持久化：data/scars.json
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.scar")


class Scar:
    """单条伤痕记录。"""

    __slots__ = ("user_id", "event", "severity", "created_at",
                 "last_triggered", "faded", "context")

    def __init__(self, user_id: int, event: str, severity: float = 0.5,
                 context: str = ""):
        self.user_id = user_id
        self.event = event
        self.severity = max(0.1, min(1.0, severity))  # 0.1-1.0
        self.created_at = time.time()
        self.last_triggered = self.created_at
        self.faded = False
        self.context = context


class ScarSystem:
    """伤痕管理器。"""

    SEVERITY_LABELS = {
        (0.0, 0.3): "轻微",
        (0.3, 0.6): "中等",
        (0.6, 0.9): "严重",
        (0.9, 1.1): "很深",
    }

    def __init__(self, data_dir: str = "data", max_scars: int = 20):
        self._scars: list[Scar] = []
        self.max_scars = max_scars
        self._persist = PersistenceProvider(data_dir)
        self._path = "scars.json"
        self._restore()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _restore(self) -> None:
        data = self._persist.read_json(self._path, {"scars": []})
        for item in data.get("scars", []):
            s = Scar(
                user_id=item.get("user_id", 0),
                event=item.get("event", ""),
                severity=item.get("severity", 0.5),
                context=item.get("context", ""),
            )
            s.created_at = item.get("created_at", time.time())
            s.last_triggered = item.get("last_triggered", s.created_at)
            s.faded = item.get("faded", False)
            self._scars.append(s)

    def persist(self) -> None:
        data = {
            "scars": [
                {
                    "user_id": s.user_id,
                    "event": s.event,
                    "severity": s.severity,
                    "created_at": s.created_at,
                    "last_triggered": s.last_triggered,
                    "faded": s.faded,
                    "context": s.context,
                }
                for s in self._scars
            ]
        }
        self._persist.write_json(self._path, data)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def add(self, user_id: int, event: str, severity: float = 0.5,
            context: str = "") -> Scar:
        """记录一条伤痕。"""
        # 合并同用户同类事件
        for s in self._scars:
            if s.user_id == user_id and s.event == event and not s.faded:
                s.severity = min(1.0, s.severity + 0.15)
                s.last_triggered = time.time()
                self.persist()
                return s

        scar = Scar(user_id, event, severity, context)
        self._scars.append(scar)
        self._prune()
        self.persist()
        logger.info("新增伤痕: user=%d event=%s severity=%.2f",
                    user_id, event, severity)
        return scar

    def heal(self, user_id: int, event_keyword: str = "",
             amount: float = 0.1) -> int:
        """治愈伤痕（通过正面互动）。返回治愈的条数。"""
        healed = 0
        for s in self._scars:
            if s.user_id != user_id:
                continue
            if event_keyword and event_keyword not in s.event:
                continue
            s.severity = max(0.0, s.severity - amount)
            if s.severity < 0.15:
                s.faded = True
            healed += 1
        self.persist()
        return healed

    def fade_over_time(self) -> None:
        """伤痕随时间淡化。"""
        now = time.time()
        for s in self._scars:
            if s.faded:
                continue
            days_since = (now - s.last_triggered) / 86400
            if days_since > 7:
                # 每周淡化 0.05
                s.severity = max(0.0, s.severity - 0.05)
                if s.severity < 0.1:
                    s.faded = True

    def get_for_user(self, user_id: int) -> list[Scar]:
        """获取某用户的所有未淡化伤痕。"""
        self.fade_over_time()
        return [s for s in self._scars
                if s.user_id == user_id and not s.faded]

    def get_active(self, min_severity: float = 0.3) -> list[Scar]:
        """获取所有未淡化的严重伤痕。"""
        self.fade_over_time()
        return [s for s in self._scars
                if not s.faded and s.severity >= min_severity]

    def prompt_fragment(self, user_id: int) -> str:
        """注入 prompt 的伤痕提醒。"""
        scars = self.get_for_user(user_id)
        if not scars:
            return ""

        # 按严重度排序，只取最相关的 3 条
        scars.sort(key=lambda s: -s.severity)
        lines = ["## 你对这个人的敏感点"]
        for s in scars[:3]:
            label = self._severity_label(s.severity)
            lines.append(f"- {label}：{s.event}")
        return "\n".join(lines)

    def trust_penalty(self, user_id: int) -> float:
        """返回对该用户的信任惩罚值 (0-1)。"""
        scars = self.get_for_user(user_id)
        if not scars:
            return 0.0
        return min(1.0, sum(s.severity for s in scars) / len(scars))

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _severity_label(self, severity: float) -> str:
        for (low, high), label in self.SEVERITY_LABELS.items():
            if low <= severity < high:
                return label
        return "很深"

    def _prune(self) -> None:
        """保留最严重的 scars，移除已淡化的旧伤痕。"""
        # 先按严重度和时间排序
        self._scars.sort(key=lambda s: (-s.severity, -s.last_triggered))
        survivors = [s for s in self._scars[:self.max_scars] if not s.faded]
        # 已淡化超过 30 天的彻底删除
        now = time.time()
        faded_survivors = [s for s in self._scars[self.max_scars:]
                           if s.faded and (now - s.last_triggered) < 86400 * 30]
        self._scars = survivors + faded_survivors


# 全局单例（按 data_dir 隔离）
_scar_systems: dict[str, ScarSystem] = {}


def get_scar_system(data_dir: str = "data") -> ScarSystem:
    global _scar_systems
    if data_dir not in _scar_systems:
        _scar_systems[data_dir] = ScarSystem(data_dir)
    return _scar_systems[data_dir]
