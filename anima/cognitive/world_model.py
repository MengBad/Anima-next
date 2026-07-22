"""世界观 —— Anima 对世界的理解、信念与行为准则。

设计：
- 记录 Anima 认为的事实、偏好、规则
- 从对话和自学习笔记中提炼世界观条目
- 高稳定性条目影响 system prompt，形成「人格底色」
- 持久化：data/world_model.json
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.cognitive.world_model")


class WorldModelEntry:
    """单条世界观条目。"""

    __slots__ = ("text", "category", "weight", "created_at", "last_confirmed",
                 "source", "evidence_count")

    def __init__(self, text: str, category: str = "belief", weight: float = 0.5,
                 source: str = "rumination"):
        self.text = text
        self.category = category  # fact / belief / rule / preference
        self.weight = max(0.1, min(1.0, weight))
        self.created_at = time.time()
        self.last_confirmed = self.created_at
        self.source = source
        self.evidence_count = 1


class WorldModel:
    """世界观管理器。"""

    # 不设基于话题词的世界观黑名单。性别认同、群内人物和网络梗都可以正常
    # 聊；过去按词屏蔽会让无害提问也被拒答。长期记忆防污染由下方的分析腔
    # 过滤和会话隔离负责。
    BLOCKED_TOPICS: tuple[str, ...] = ()

    # 论文体/社交策略分析腔关键词：反刍常把社交互动写成"策略分析"存进世界观，
    # 与 persona「不写分析论文」冲突。命中即丢弃，防 world_model 被腌成分析报告库
    # （见 2026-07-12 回复诊断报告根因 1：18 条 belief 全是自我分析论文）。
    ANALYSIS_KEYWORDS = (
        "拟人化", "情绪价值", "以柔克刚", "顺毛", "趣味与专业",
        "社交策略", "化解冲突", "策略性", "元认知", "自我解剖",
        "结构化", "人设", "角色扮演", "情绪宣泄", "提供情绪",
        "我需要更好地平衡", "拟人化表达", "猫系",
    )

    CATEGORIES = {
        "fact": "我相信的事实",
        "belief": "我的信念",
        "rule": "我遵循的规则",
        "preference": "我的偏好",
    }

    @staticmethod
    def is_blocked_topic(text: str) -> bool:
        """兼容旧调用点；不再以主题关键词禁止正常对话。"""
        return False

    def __init__(self, data_dir: str = "data", max_entries: int = 32):
        self._entries: list[WorldModelEntry] = []
        self.max_entries = max_entries
        self._persist = PersistenceProvider(data_dir)
        self._path = "world_model.json"
        self._restore()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _restore(self) -> None:
        data = self._persist.read_json(self._path, {"entries": []})
        for item in data.get("entries", []):
            e = WorldModelEntry(
                text=item.get("text", ""),
                category=item.get("category", "belief"),
                weight=item.get("weight", 0.5),
                source=item.get("source", "rumination"),
            )
            e.created_at = item.get("created_at", time.time())
            e.last_confirmed = item.get("last_confirmed", e.created_at)
            e.evidence_count = item.get("evidence_count", 1)
            self._entries.append(e)

    def persist(self) -> None:
        data = {
            "entries": [
                {
                    "text": e.text,
                    "category": e.category,
                    "weight": e.weight,
                    "created_at": e.created_at,
                    "last_confirmed": e.last_confirmed,
                    "source": e.source,
                    "evidence_count": e.evidence_count,
                }
                for e in self._entries
            ]
        }
        self._persist.write_json(self._path, data)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def add(self, text: str, category: str = "belief", weight: float = 0.5,
            source: str = "rumination") -> WorldModelEntry:
        """添加或强化一条世界观。"""
        # 论文体/社交策略分析腔：反刍把社交互动写成"策略分析"的 belief 直接丢弃，
        # 防 world_model 被腌成分析报告库（见 2026-07-12 回复诊断报告根因 1）
        if any(k in (text or "") for k in self.ANALYSIS_KEYWORDS):
            logger.info("WorldModel: 丢弃论文体/分析腔条目（text_len=%d）",
                        len(text or ""))
            return None
        # 去重：文本相似则强化权重
        for e in self._entries:
            if self._similar(e.text, text):
                e.weight = min(1.0, e.weight + 0.08)
                e.last_confirmed = time.time()
                e.evidence_count += 1
                return e

        entry = WorldModelEntry(text, category, weight, source)
        self._entries.append(entry)
        self._prune()
        self.persist()
        return entry

    def reinforce(self, text_keyword: str, delta: float = 0.05) -> None:
        """根据后续经验强化包含关键词的条目。"""
        for e in self._entries:
            if text_keyword in e.text:
                e.weight = min(1.0, e.weight + delta)
                e.last_confirmed = time.time()

    def weaken(self, text_keyword: str, delta: float = 0.05) -> None:
        """根据反例削弱包含关键词的条目。"""
        for e in self._entries:
            if text_keyword in e.text:
                e.weight = max(0.1, e.weight - delta)

    def decay(self) -> None:
        """长时间未被确认的条目缓慢衰减。"""
        now = time.time()
        for e in self._entries:
            days_since = (now - e.last_confirmed) / 86400
            if days_since > 7:
                e.weight = max(0.1, e.weight * 0.95)

    def get_entries(self, category: Optional[str] = None,
                    min_weight: float = 0.3) -> list[WorldModelEntry]:
        """获取世界观条目，按权重降序。"""
        self.decay()
        entries = [e for e in self._entries
                   if (category is None or e.category == category)
                   and e.weight >= min_weight]
        entries.sort(key=lambda e: -e.weight)
        return entries

    def prompt_fragment(self, max_chars: int = 800) -> str:
        """注入 system prompt 的世界观片段。"""
        entries = self.get_entries(min_weight=0.4)[:8]
        if not entries:
            return ""

        lines = ["## 你对世界的看法"]
        for e in entries:
            cat_label = self.CATEGORIES.get(e.category, e.category)
            lines.append(f"- [{cat_label}] {e.text}")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + "\n…"
        return text

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _similar(self, a: str, b: str) -> bool:
        """判断两条世界观是否相似。使用字符级 SequenceMatcher。"""
        if a == b:
            return True
        if len(a) < 4 or len(b) < 4:
            return a == b

        from difflib import SequenceMatcher
        ratio = SequenceMatcher(None, a, b).ratio()
        return ratio >= 0.55

    def _prune(self) -> None:
        """移除权重最低的超限条目。"""
        while len(self._entries) > self.max_entries:
            self._entries.sort(key=lambda e: e.weight)
            self._entries.pop(0)


# 全局单例
_world_model: Optional[WorldModel] = None


def get_world_model(data_dir: str = "data") -> WorldModel:
    global _world_model
    if _world_model is None:
        _world_model = WorldModel(data_dir)
    return _world_model
