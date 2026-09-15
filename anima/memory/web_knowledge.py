"""WebKnowledgeStore —— 搜网结果独立记忆库。

设计要点（见 DESIGN_REACT_MEMORY_FIRST.md / 用户 P1 指令）：
- 搜网学来的事实存到独立的 data/web_knowledge.json，**绝不**污染 crossgroup/users.json
  （旧实现曾把 snippet 以假用户 "web_user" 写入 users.json，已废弃）。
- 可查询：ReAct 先调 query_web_memory，命中则不必重复搜网。
- 有衰减：每日心跳调用 decay()，旧知识 confidence 指数衰减，过低自动清理。
- 可溯源：每条记录来源 URL、原始 query、创建/访问时间。
- 仅依赖标准库，避免与业务模块形成循环依赖。

测试可用 data_dir= 指向临时目录，互不干扰。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class WebKnowledge:
    id: str
    query: str
    summary: str
    source_urls: List[str]
    created_at: float
    last_accessed: float
    confidence: float
    access_count: int
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WebKnowledge":
        # 容忍旧/缺字段
        return cls(
            id=d.get("id") or str(uuid.uuid4())[:8],
            query=d.get("query", ""),
            summary=d.get("summary", ""),
            source_urls=list(d.get("source_urls", [])),
            created_at=float(d.get("created_at", time.time())),
            last_accessed=float(d.get("last_accessed", time.time())),
            confidence=float(d.get("confidence", 0.7)),
            access_count=int(d.get("access_count", 0)),
            tags=list(d.get("tags", [])),
        )


# 常见主题的轻量标签规则（不依赖 LLM，零成本）
_TAG_RULES = [
    ("天气", ["天气", "气温", "降雨", "预报", "温度"]),
    ("新闻", ["新闻", "报道", "今日", "最新", "突发"]),
    ("技术", ["python", "代码", "api", "框架", "库", "编程", "bug"]),
    ("股票", ["股票", "股价", "a股", "美股", "基金", "涨跌"]),
    ("体育", ["比赛", "球队", "球员", "比分", "联赛", "奥运"]),
    ("影视", ["电影", "剧", "综艺", "导演", "演员", "票房"]),
    ("百科", ["是什么", "定义", "含义", "怎么读", "读音"]),
]


def auto_tag(query: str) -> List[str]:
    """根据 query 关键词生成轻量标签（无 LLM）。"""
    q = (query or "").lower()
    tags: List[str] = []
    for tag, kws in _TAG_RULES:
        if any(kw in q for kw in kws):
            tags.append(tag)
    if not tags:
        tags.append("其他")
    return tags


def extract_summary(results: List[dict], max_n: int = 3) -> str:
    """从搜索结果轻量提取摘要（取前 N 条标题 + 摘要拼接，不调 LLM）。"""
    parts: List[str] = []
    for r in results[:max_n]:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        line = title
        if snippet:
            line += f"：{snippet}"
        if line:
            parts.append(line)
    return "\n".join(parts)


class WebKnowledgeStore:
    """搜网知识库：独立 JSON 文件存储，支持增/查/衰减。"""

    def __init__(self, data_dir: Any = None, file_name: str = "web_knowledge.json"):
        if data_dir is None:
            data_dir = Path("data")
        self.FILE = Path(data_dir) / file_name
        self._data: List[WebKnowledge] = []
        self._load()

    # --- 持久化 ---------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.FILE.exists():
                with open(self.FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._data = [WebKnowledge.from_dict(it) for it in raw]
        except Exception as e:  # 损坏文件不致命：从空库开始
            import logging
            logging.getLogger("anima.memory.web_knowledge").warning(
                "web_knowledge 载入失败，重建空库: %s", e)
            self._data = []

    def save(self) -> None:
        self.FILE.parent.mkdir(parents=True, exist_ok=True)
        from anima.core.persistence import atomic_write_text
        atomic_write_text(self.FILE,
                          json.dumps([w.to_dict() for w in self._data],
                                     ensure_ascii=False, indent=2))

    # --- 写入 -----------------------------------------------------------
    def add(self, query: str, summary: str, source_urls: Optional[List[str]] = None,
            tags: Optional[List[str]] = None) -> WebKnowledge:
        item = WebKnowledge(
            id=str(uuid.uuid4())[:8],
            query=query,
            summary=summary,
            source_urls=list(source_urls or []),
            created_at=time.time(),
            last_accessed=time.time(),
            confidence=0.7,
            access_count=0,
            tags=tags or auto_tag(query),
        )
        self._data.append(item)
        self.save()
        return item

    # --- 查询 -----------------------------------------------------------
    def query(self, query_text: str, top_k: int = 3) -> List[WebKnowledge]:
        """简单子串匹配（后续可换 embedding）。命中即更新访问统计。"""
        q = (query_text or "").lower().strip()
        if not q:
            return []
        matches: List[WebKnowledge] = []
        for item in self._data:
            hay = (item.query + " " + item.summary).lower()
            if q in hay:
                item.last_accessed = time.time()
                item.access_count += 1
                matches.append(item)
        matches.sort(key=lambda x: x.confidence, reverse=True)
        return matches[:top_k]

    # --- 衰减 -----------------------------------------------------------
    def decay(self) -> int:
        """每日调用：confidence 按存在天数指数衰减，低于阈值清理。返回清理条数。

        注意：衰减后必须落盘——否则次日重新从原值衰减，永远到不了清理阈值。
        """
        now = time.time()
        before = len(self._data)
        for item in self._data:
            days_old = max(0.0, (now - item.created_at) / 86400.0)
            item.confidence *= (0.99 ** days_old)
        self._data = [it for it in self._data if it.confidence >= 0.2]
        removed = before - len(self._data)
        self.save()
        return removed

    # --- 测试/调试辅助 --------------------------------------------------
    def count(self) -> int:
        return len(self._data)

    def all(self) -> List[WebKnowledge]:
        return list(self._data)
