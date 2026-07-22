"""轻量欲望系统 —— 让 Anima 有自己的「想聊什么」。

设计：
- 从对话中提取可能的话题兴趣
- intensity 随时间衰减
- 满足后标记 resolved
- 高强度欲望注入 prompt 影响回复方向

持久化：data/desires.json，重启恢复。
"""

from __future__ import annotations
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider


EXPRESSION_SCOPES = frozenset({"internal", "group", "global"})


def _normalise_group_id(value: object) -> Optional[int]:
    """将来源群号规范为正整数；私聊、缺失和非法值统一为 None。"""
    try:
        group_id = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return group_id if group_id > 0 else None


class Desire:
    __slots__ = (
        "content", "intensity", "created_at", "resolved", "kind", "goal", "acted",
        "source_group_id", "expression_scope",
    )
    def __init__(self, content: str, intensity: float = 0.6, kind: str = "chat",
                 goal: Optional[str] = None, acted: bool = False,
                 source_group_id: Optional[int] = None,
                 expression_scope: str = "internal"):
        self.content = content
        self.intensity = max(0.1, min(1.0, intensity))
        self.created_at = time.time()
        self.resolved = False
        self.kind = kind
        self.goal = goal if goal is not None else content
        self.acted = acted
        self.source_group_id = _normalise_group_id(source_group_id)
        scope = expression_scope if expression_scope in EXPRESSION_SCOPES else "internal"
        # 群作用域必须有明确来源群；绝不能把缺失群号降级成全局表达。
        self.expression_scope = "internal" if scope == "group" and self.source_group_id is None else scope


class DesireSystem:
    """轻量欲望管理。"""

    def __init__(self, data_dir: str = "data", max_desires: int = 10, decay_rate: float = 0.95):
        self.desires: list[Desire] = []
        self.max_desires = max_desires
        self.decay_rate = decay_rate
        self._last_decay = time.time()
        self._persist = PersistenceProvider(data_dir)
        self._path = "desires.json"

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def restore(self) -> None:
        data = self._persist.read_json(self._path, [])
        for item in data:
            d = Desire(item.get("content", ""), item.get("intensity", 0.5),
                      kind=item.get("kind", "chat"), goal=item.get("goal"),
                      acted=item.get("acted", False),
                      source_group_id=item.get("source_group_id"),
                      # 旧数据没有作用域，按“仅供内在认知”迁移，禁止继续跨群发声。
                      expression_scope=item.get("expression_scope", "internal"))
            d.created_at = item.get("created_at", time.time())
            d.resolved = item.get("resolved", False)
            self.desires.append(d)

    def persist(self) -> None:
        data = [
            {
                "content": d.content,
                "intensity": d.intensity,
                "created_at": d.created_at,
                "resolved": d.resolved,
                "kind": d.kind,
                "goal": d.goal,
                "acted": d.acted,
                "source_group_id": d.source_group_id,
                "expression_scope": d.expression_scope,
            }
            for d in self.desires
        ]
        self._persist.write_json(self._path, data)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def add(self, content: str, intensity: float = 0.6, kind: str = "chat",
            goal: Optional[str] = None, source_group_id: Optional[int] = None,
            expression_scope: str = "internal") -> Optional[Desire]:
        """添加一个新欲望。
        - chat 类：内容与已有欲望相似（子串匹配）则不重复，仅提升 intensity。
        - self_improvement 类：按 goal 去重，避免重复生成同一自升级目标。
        """
        goal = goal if goal is not None else content
        source_group_id = _normalise_group_id(source_group_id)
        expression_scope = expression_scope if expression_scope in EXPRESSION_SCOPES else "internal"
        if expression_scope == "group" and source_group_id is None:
            expression_scope = "internal"
        for d in self.desires:
            if (d.kind == kind and d.expression_scope == expression_scope
                    and d.source_group_id == source_group_id):
                if kind == "self_improvement":
                    if d.goal == goal:
                        d.intensity = min(1.0, d.intensity + 0.1)
                        return d
                elif self._similar(d.content, content):
                    d.intensity = min(1.0, d.intensity + 0.1)
                    return d

        desire = Desire(
            content, intensity, kind, goal,
            source_group_id=source_group_id,
            expression_scope=expression_scope,
        )
        self.desires.append(desire)

        # LRU 裁剪
        while len(self.desires) > self.max_desires:
            # 移除 intensity 最低的未解决欲望
            active = [d for d in self.desires if not d.resolved]
            if len(active) > self.max_desires:
                active.sort(key=lambda d: d.intensity)
                self.desires.remove(active[0])
            else:
                # 已解决的欲望也会占用容量。此前这里没有删除分支，
                # 当 active <= max_desires 且总数超限时会持续空转，
                # 阻塞事件循环，最终触发 systemd watchdog 重启。
                resolved = [d for d in self.desires if d.resolved]
                if not resolved:
                    # 理论上 active > max_desires 已覆盖该情况；
                    # 保险起见避免任何异常数据导致无限循环。
                    break
                resolved.sort(key=lambda d: (d.created_at, d.intensity))
                self.desires.remove(resolved[0])

        return desire

    def mark_satisfied(self, content_keyword: str) -> None:
        """标记匹配的欲望为已满足。"""
        for d in self.desires:
            if not d.resolved and content_keyword in d.content:
                d.resolved = True
                d.intensity = 0.0

    def mark_resolved(self, desire: Optional[Desire]) -> None:
        """按对象精确标记欲望，避免相似内容在不同群之间被一并解决。"""
        if desire is not None and desire in self.desires:
            desire.resolved = True
            desire.intensity = 0.0

    def get_self_improvement_candidates(self, min_intensity: float = 0.7) -> list:
        """返回待处理的自升级欲望（未解决、未执行、强度达标），按强度降序。"""
        self.decay()
        cands = [
            d for d in self.desires
            if d.kind == "self_improvement" and not d.resolved
            and not d.acted and d.intensity >= min_intensity
        ]
        cands.sort(key=lambda d: -d.intensity)
        return cands

    def mark_self_improvement_done(self, desire: "Desire") -> None:
        """标记某个自升级欲望已执行，避免重复触发（防自升级死循环）。"""
        if desire is not None:
            desire.acted = True
            desire.resolved = True
            desire.intensity = 0.0

    def decay(self) -> None:
        """衰减所有活跃欲望的 intensity。"""
        now = time.time()
        # 每分钟触发一次衰减
        if now - self._last_decay < 60:
            return
        self._last_decay = now

        for d in self.desires:
            if not d.resolved and d.intensity > 0.05:
                d.intensity *= self.decay_rate

    def get_active(self, min_intensity: float = 0.3) -> list[Desire]:
        """获取活跃的欲望列表（intensity >= min_intensity）。"""
        self.decay()
        active = [
            d for d in self.desires
            if not d.resolved and d.intensity >= min_intensity
        ]
        active.sort(key=lambda d: -d.intensity)
        return active

    def get_active_for_context(
        self, group_id: Optional[int], min_intensity: float = 0.3,
    ) -> list[Desire]:
        """返回可注入当前会话的欲望，永不暴露其他群的群级欲望。"""
        group_id = _normalise_group_id(group_id)
        active = [
            d for d in self.get_active(min_intensity)
            if d.expression_scope in {"internal", "global"}
            or (d.expression_scope == "group" and d.source_group_id == group_id)
        ]
        return active

    def get_expressible(
        self, group_id: Optional[int], min_intensity: float = 0.55,
    ) -> list[Desire]:
        """返回允许在指定群主动表达的欲望；旧数据和内在欲望永远不会出站。"""
        group_id = _normalise_group_id(group_id)
        if group_id is None:
            return []
        return [
            d for d in self.get_active(min_intensity)
            if d.expression_scope == "global"
            or (d.expression_scope == "group" and d.source_group_id == group_id)
        ]

    def prompt_fragment(self, group_id: Optional[int] = None) -> str:
        """生成注入 prompt 的欲望文本片段。"""
        active = self.get_active_for_context(group_id, 0.3)
        if not active:
            return ""

        lines = ["## 你最近想聊的话题"]
        for i, d in enumerate(active[:3]):
            lines.append(f"{i+1}. {d.content} (兴趣程度: {d.intensity:.2f})")
        return "\n".join(lines)

    def _similar(self, a: str, b: str) -> bool:
        """简单的子串相似检测。"""
        if len(a) < 3 or len(b) < 3:
            return a == b
        # 较短的字符串是否被较长的包含
        short, long = (a, b) if len(a) < len(b) else (b, a)
        return short in long or any(w in long for w in short.split() if len(w) > 1)


# 全局单例（按 data_dir 隔离，避免跨数据目录串数据 / 测试污染）
_desire_systems: dict[str, DesireSystem] = {}


def get_desire_system(data_dir: str = "data") -> DesireSystem:
    global _desire_systems
    if data_dir not in _desire_systems:
        sys = DesireSystem(data_dir)
        sys.restore()
        _desire_systems[data_dir] = sys
    return _desire_systems[data_dir]
