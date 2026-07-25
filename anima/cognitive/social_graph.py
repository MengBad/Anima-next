"""E1: 群内社交图谱 —— 追踪群成员之间的互动关系。

设计：
- 追踪谁@了谁、谁引用回复了谁
- 按互动次数估计亲密度
- 注入 prompt：让 bot 理解群内人际拓扑

持久化：data/social_graph.json
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.social_graph")


class SocialGraph:
    """群内社交关系图谱。"""

    def __init__(self, data_dir: str = "data"):
        self._persist = PersistenceProvider(data_dir)
        self._path = "social_graph.json"
        # edges: {(group_id, user_a, user_b): interaction_count}
        self._edges: dict[tuple[str, str, str], float] = {}
        self._last_decay = time.time()
        self._restore()

    def _restore(self) -> None:
        data = self._persist.read_json(self._path, {"edges": {}})
        for key_str, count in data.get("edges", {}).items():
            parts = key_str.split("|")
            if len(parts) == 3:
                self._edges[tuple(parts)] = float(count)

    def persist(self) -> None:
        data = {
            "edges": {
                f"{gid}|{ua}|{ub}": count
                for (gid, ua, ub), count in self._edges.items()
            }
        }
        self._persist.write_json(self._path, data)

    def observe_interaction(self, group_id: str, user_a: str, user_b: str,
                            weight: float = 1.0) -> None:
        """记录一次 A→B 的互动（@或引用回复）。"""
        if user_a == user_b:
            return
        # 规范化顺序（保证 A-B 和 B-A 归一）
        key = (group_id, *sorted([user_a, user_b]))
        self._edges[key] = self._edges.get(key, 0) + weight
        self._decay_if_needed()

    def _decay_if_needed(self) -> None:
        """每周衰减 10%，防止无限累积。"""
        now = time.time()
        if now - self._last_decay < 86400 * 7:
            return
        self._last_decay = now
        stale = []
        for key in self._edges:
            self._edges[key] *= 0.9
            if self._edges[key] < 0.5:
                stale.append(key)
        for key in stale:
            del self._edges[key]
        self.persist()

    def get_close_pairs(self, group_id: str, min_interactions: float = 3.0
                        ) -> list[tuple[str, str, float]]:
        """获取指定群内互动频繁的对数。"""
        results = []
        for (gid, ua, ub), count in self._edges.items():
            if gid == group_id and count >= min_interactions:
                results.append((ua, ub, count))
        results.sort(key=lambda x: -x[2])
        return results[:5]

    def fragment(self, group_id: Optional[str] = None) -> str:
        """生成注入 prompt 的社交图谱片段。"""
        if group_id is None:
            return ""
        pairs = self.get_close_pairs(group_id, min_interactions=3.0)
        if not pairs:
            return ""
        lines = ["## 群内关系观察"]
        for ua, ub, count in pairs[:3]:
            if count >= 8:
                desc = f"{ua} 和 {ub} 看起来关系很好，经常互动"
            elif count >= 5:
                desc = f"{ua} 和 {ub} 比较熟"
            else:
                desc = f"{ua} 和 {ub} 偶尔会互相说话"
            lines.append(f"- {desc}")
        return "\n".join(lines)


# 全局单例
_social_graph: Optional[SocialGraph] = None


def get_social_graph(data_dir: str = "data") -> SocialGraph:
    global _social_graph
    if _social_graph is None:
        _social_graph = SocialGraph(data_dir)
    return _social_graph
