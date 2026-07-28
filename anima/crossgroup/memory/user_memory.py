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
    # P4: 跨群身份统一——不同群的不同名字
    aliases: list[str] = field(default_factory=list)  # 最多保留 5 个别名
    # 外号：由他人对该用户的称呼/昵称（如「企鹅仔」「胖虎」），由事实抽取结构化捕获
    nicknames: list[str] = field(default_factory=list)  # 最多保留 8 个外号
    # E2: 用户偏好学习
    topic_preferences: dict[str, int] = field(default_factory=dict)  # 话题 -> 出现次数
    style_hint: str = ""  # 回复风格提示（如“偏简短”“爱发表情”）
    avg_msg_length: float = 0.0  # 平均消息长度
    _msg_count: int = 0  # 统计用

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

    def recall_public(self, k: int = 3) -> list[UserFact]:
        """召回最近 k 条『公开』事实（排除私聊来源 ``source_group == 'private'``）。

        ANM-004 修复：用于会暴露给他人的场景（关系总结 / 成员事实回顾），
        避免私聊私密事实泄漏给其他用户。先过滤私有再按时间截断，保证返回
        最多 k 条公开事实（不被私有事实挤占配额）。``get_member_facts`` 与
        ``get_user_relations`` 统一收敛到此方法，避免过滤逻辑分散、再次漏写。
        """
        public = [f for f in self.facts if f.source_group != "private"]
        return sorted(public, key=lambda f: f.timestamp, reverse=True)[:k]

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

    def update_topic(self, topic: str) -> None:
        """E2: 记录用户话题偏好。"""
        if not topic or len(topic) < 2:
            return
        self.topic_preferences[topic] = self.topic_preferences.get(topic, 0) + 1
        # 保留 top 10
        if len(self.topic_preferences) > 20:
            sorted_topics = sorted(
                self.topic_preferences.items(), key=lambda x: -x[1])
            self.topic_preferences = dict(sorted_topics[:10])

    def update_style(self, msg_length: int) -> None:
        """E2: 根据消息长度更新风格提示。"""
        self._msg_count += 1
        # 指数移动平均
        alpha = 0.1
        self.avg_msg_length = (
            self.avg_msg_length * (1 - alpha) + msg_length * alpha
        ) if self._msg_count > 1 else float(msg_length)
        # 每 10 条消息更新一次 style_hint
        if self._msg_count % 10 == 0 and self._msg_count >= 10:
            if self.avg_msg_length < 15:
                self.style_hint = "偏简短"
            elif self.avg_msg_length < 40:
                self.style_hint = "适中"
            else:
                self.style_hint = "偏详细"

    def preference_fragment(self) -> str:
        """E2: 生成用户偏好提示片段。"""
        parts = []
        if self.style_hint:
            parts.append(f"回复风格{self.style_hint}")
        if self.topic_preferences:
            top_topics = sorted(
                self.topic_preferences.items(), key=lambda x: -x[1])[:3]
            if top_topics:
                topics_text = "、".join(t for t, _ in top_topics)
                parts.append(f"喜欢聊{topics_text}")
        if not parts:
            return ""
        return "这个人" + "，".join(parts)
