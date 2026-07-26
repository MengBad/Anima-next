"""世界观 —— Anima 对世界的理解、信念与行为准则。

设计：
- 记录 Anima 认为的事实、偏好、规则
- 从对话和自学习笔记中提炼世界观条目
- 高稳定性条目影响 system prompt，形成「人格底色」
- 持久化：data/world_model.json
"""

from __future__ import annotations
import logging
import re
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.cognitive.world_model")


def _is_world_garbled(text: str) -> bool:
    """检测世界观条目是否为乱码。"""
    if not text or not text.strip():
        return True
    s = text.strip()
    if '\ufffd' in s:
        return True
    stripped = re.sub(r'[\s\u3000]', '', s)
    if len(stripped) >= 3:
        qmarks = sum(1 for c in stripped if c in '?\uff1f')
        if qmarks / len(stripped) > 0.5:
            return True
    if len(s) >= 5:
        readable = sum(1 for c in s if '\u4e00' <= c <= '\u9fff' or c.isalnum())
        if readable / len(s) < 0.2:
            return True
    return False


class WorldModelEntry:
    """单条世界观条目。"""

    __slots__ = ("text", "category", "weight", "created_at", "last_confirmed",
                 "source", "evidence_count", "contested", "contested_with",
                 "last_contested_at")

    def __init__(self, text: str, category: str = "belief", weight: float = 0.5,
                 source: str = "rumination"):
        self.text = text
        self.category = category  # fact / belief / rule / preference
        self.weight = max(0.1, min(1.0, weight))
        self.created_at = time.time()
        self.last_confirmed = self.created_at
        self.source = source
        self.evidence_count = 1
        self.contested = False       # B3: 是否被新信念挑战
        self.contested_with: str = ""  # B3: 挑战它的信念文本
        self.last_contested_at: float = 0.0  # B3: 上次被标记 contested 的时间


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
            # B3: 恢复 contested 状态
            e.contested = item.get("contested", False)
            e.contested_with = item.get("contested_with", "")
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
                    "contested": e.contested,          # B3
                    "contested_with": e.contested_with, # B3
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
        # B3: 先做冲突检测（在去重之前，避免否定句被判为相似）
        # 冷却：同一条目 1 小时内不被重复标记为 contested，避免频繁误触发
        conflict_entry = self.detect_conflict(text)
        if conflict_entry:
            _now = time.time()
            _last_contest = getattr(conflict_entry, "last_contested_at", 0) or 0
            if _now - _last_contest >= 3600:
                conflict_entry.contested = True
                conflict_entry.contested_with = text
                conflict_entry.weight = max(0.1, conflict_entry.weight * 0.7)
                conflict_entry.last_contested_at = _now
                logger.info("WorldModel: 信念冲突，标记旧条目为 contested: '%s' vs '%s'",
                            conflict_entry.text[:30], text[:30])

        # 去重：文本相似则强化权重（已 contested 的条目不参与去重）
        for e in self._entries:
            if e.contested:
                continue
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
        """注入 system prompt 的世界观片段。

        B3: 按 category 分组注入（fact > rule > belief > preference），
        contested 条目以“曾经觉得X，现在觉得Y”形式注入。
        """
        self.decay()
        entries = [e for e in self._entries
                   if e.weight >= 0.4 and not _is_world_garbled(e.text)]
        if not entries:
            return ""

        lines = ["## 你对世界的看法"]

        # 按 category 优先级排序
        category_order = {"fact": 0, "rule": 1, "belief": 2, "preference": 3}
        entries.sort(key=lambda e: (category_order.get(e.category, 9), -e.weight))

        for e in entries[:8]:
            cat_label = self.CATEGORIES.get(e.category, e.category)
            if e.contested and e.contested_with:
                lines.append(f"- [{cat_label}] 你曾经觉得{e.text}，但现在又觉得{e.contested_with}")
            else:
                lines.append(f"- [{cat_label}] {e.text}")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + "\n…"
        return text

    def detect_conflict(self, new_text: str) -> Optional[WorldModelEntry]:
        """B3: 检测新条目是否与已有信念矛盾。

        简单规则：检测反义词/否定词对，如“喜欢”vs“不喜欢”、“应该”vs“不应该”。
        不依赖 LLM，纯文本匹配。
        """
        _CONFLICT_PAIRS = [
            ("喜欢", "不喜欢"), ("不喜欢", "喜欢"),
            ("应该", "不应该"), ("不应该", "应该"),
            ("讨厌", "不讨厌"), ("不讨厌", "讨厌"),
            ("信任", "不信任"), ("不信任", "信任"),
            ("害怕", "不怕"), ("不怕", "害怕"),
        ]
        for e in self._entries:
            if e.contested or e.weight < 0.3:
                continue
            for a, b in _CONFLICT_PAIRS:
                if a in new_text and b in e.text:
                    return e
                if b in new_text and a in e.text:
                    return e
        return None

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
