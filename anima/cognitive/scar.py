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

# R1: 伤痕触发关键词提取（简单分词）
_HURT_KEYWORDS = [
    "骂", "无视", "骗", "讨厌", "滚", "死", "傻", "丑", "肥",
    "恶心", "垃圾", "废物", "闭嘴", "消失", "不要脸", "恶心",
    "拉黑", "踢", "禁言", "举报", "威胁", "吓", "打", "欺负",
]


def _extract_keywords(event: str) -> list[str]:
    """从事件文本中提取触发关键词（最多 5 个）。"""
    found = [kw for kw in _HURT_KEYWORDS if kw in event]
    return found[:5]


class Scar:
    """单条伤痕记录。"""

    __slots__ = ("user_id", "event", "severity", "created_at",
                 "last_triggered", "faded", "context",
                 "trigger_keywords", "avoid_until")  # R1 + 回避

    def __init__(self, user_id: int, event: str, severity: float = 0.5,
                 context: str = "", avoid_until: float = 0.0):
        self.user_id = user_id
        self.event = event
        self.severity = max(0.1, min(1.0, severity))  # 0.1-1.0
        self.created_at = time.time()
        self.last_triggered = self.created_at
        self.faded = False
        self.context = context
        self.avoid_until = avoid_until  # 回避截止时间戳（秒）；>now 表示正在回避该用户
        # R1: 从 event 中提取触发关键词（最多 5 个）
        self.trigger_keywords: list[str] = _extract_keywords(event)


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
        self._avoid_until: dict[int, float] = {}  # 用户级回避截止时间戳
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
                avoid_until=item.get("avoid_until", 0.0),
            )
            s.created_at = item.get("created_at", time.time())
            s.last_triggered = item.get("last_triggered", s.created_at)
            s.faded = item.get("faded", False)
            # R1: 恢复 trigger_keywords
            s.trigger_keywords = item.get("trigger_keywords", _extract_keywords(s.event))
            self._scars.append(s)
        # 恢复用户级回避截止
        self._avoid_until = {
            int(uid): float(ts) for uid, ts in
            (data.get("avoid_until") or {}).items()
        }

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
                    "trigger_keywords": s.trigger_keywords,  # R1
                    "avoid_until": s.avoid_until,  # 回避
                }
                for s in self._scars
            ],
            "avoid_until": {str(uid): ts for uid, ts in self._avoid_until.items()},
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
        # 回避联动：某用户累计伤痕（未淡化）>= 2 时，进入 24h 回避期
        active = [s for s in self._scars
                  if s.user_id == user_id and not s.faded]
        if len(active) >= 2 and self._avoid_until.get(user_id, 0.0) <= time.time():
            self._avoid_until[user_id] = time.time() + 86400
            logger.info("用户 %d 伤痕达 %d 条，进入 24h 回避期", user_id, len(active))
        self.persist()
        logger.info("新增伤痕: user=%d event=%s severity=%.2f",
                    user_id, event, severity)
        return scar

    # ------------------------------------------------------------------
    # 回避（自我保护）
    # ------------------------------------------------------------------

    def avoid_until_for(self, user_id: int) -> float:
        """返回该用户的回避截止时间戳（秒）；0 表示未回避。"""
        return self._avoid_until.get(user_id, 0.0)

    def is_avoiding(self, user_id: int, now: Optional[float] = None) -> bool:
        """该用户当前是否处于回避期（avoid_until > now）。"""
        now = time.time() if now is None else now
        return self._avoid_until.get(user_id, 0.0) > now

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
        # R1: 若 scar 最近 24h 内被触发过，追加提示
        now = time.time()
        recent_triggered = any(
            (now - s.last_triggered) < 86400 for s in scars[:3]
        )
        if recent_triggered:
            lines.append("- 你现在对这个人还有点在意那件事")
        return "\n".join(lines)

    def check_trigger(self, user_id: int, text: str) -> bool:
        """R1: 检查用户消息是否命中伤痕触发关键词。

        命中时刷新 last_triggered，返回 True。
        """
        triggered = False
        for s in self._scars:
            if s.user_id != user_id or s.faded:
                continue
            if any(kw in text for kw in s.trigger_keywords):
                s.last_triggered = time.time()
                triggered = True
        if triggered:
            self.persist()
        return triggered

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
        now = time.time()
        keep: list[Scar] = []
        for s in self._scars:
            # 淡化超 30 天的彻底删除
            if s.faded and (now - s.last_triggered) >= 86400 * 30:
                continue
            keep.append(s)
        # 保留最严重的 max_scars 条
        self._scars = keep[:self.max_scars]


# 全局单例（按 data_dir 隔离）
_scar_systems: dict[str, ScarSystem] = {}


def get_scar_system(data_dir: str = "data") -> ScarSystem:
    global _scar_systems
    if data_dir not in _scar_systems:
        _scar_systems[data_dir] = ScarSystem(data_dir)
    return _scar_systems[data_dir]
