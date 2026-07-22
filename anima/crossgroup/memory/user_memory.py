"""跨群用户记忆：同一用户在所有群中的事实/偏好/历史互通。

关键设计：每条用户事实都携带 ``source_group``，因此记忆天然是"跨群共享"的——
在任意群都能调用该用户在其他群的交互信息，同时保留来源可追溯。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class UserFact:
    content: str
    source_group: str
    timestamp: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)


@dataclass
class UserMemory:
    user_id: str
    name: str = ""
    facts: list[UserFact] = field(default_factory=list)
    preferences: dict = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)  # group_id -> 备注/角色

    def add_fact(self, content: str, source_group: str,
                 tags: list[str] | None = None) -> UserFact:
        fact = UserFact(content=content, source_group=source_group, tags=tags or [])
        self.facts.append(fact)
        self.groups.setdefault(source_group, "成员")
        return fact

    def recall(self, k: int = 3) -> list[UserFact]:
        """召回最近 k 条事实（跨群混合排序，体现共享）。

        默认 k=3：注入 system 的跨群事实封顶 3 条，避免个别用户 17 条 facts
        把上下文 priming 得过重（见 2026-07-12 回复诊断报告 P2）。
        """
        return sorted(self.facts, key=lambda f: f.timestamp, reverse=True)[:k]

    def summary(self, max_facts: int = 3, per_fact_chars: int = 80) -> str:
        if not self.facts:
            return "（暂无已知信息）"
        # 注入封顶：最多 max_facts 条，每条截断 per_fact_chars 字，控总量
        facts = self.recall(k=max_facts)
        lines = [
            f"- {f.content[:per_fact_chars]}（来源群：{f.source_group}）"
            for f in facts
        ]
        return "\n".join(lines)
