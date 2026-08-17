"""P4 —— Long-Term Memory（长期记忆：事件 → 知识 → 行为模式）。

让 ANIMA 真正「积累经验」，对应 Owner 想要的长期记忆三层：

1. **事件层（Event）**：原始信号 —— Owner 偏好/指令、做出的决策、报错、模块设计原因。
2. **知识层（Knowledge）**：从事件**确定性抽取**的事实/规则（"Owner 偏好 X"、"某配置意为 Z"）。
3. **行为模式层（Behavior Pattern）**：从大量事件**归纳**的反复出现的行为
   （"Owner 总是拒绝触碰 rbac.py 的提案"、"只在周末部署"）。

外加 **why-described（为何这样设计）** 专项：记录「某个模块/决策当初为何这样设计」，
直接服务后续 P5 自治维护（避免 AI 在重构时误删有意为之的护栏）。

设计纪律（延续 R6–R15 / P0–P3，尤其 Audit Everything / Fail Closed）：
- **只读抽取 + 追加落盘**：所有写都是 append 到 JSONL（坏行隔离），不修改历史、不删源。
- **无 LLM 依赖**：抽取/归纳用确定性规则 + 关键词，可单测、可复现、零外部调用。
- **诚实**：数据缺失/为空 → 返回空列表，绝不编造「已知 Owner 习惯」。
- **不授权**：记忆层只存不执行；行为模式仅供 Owner / 上层闭环参考，不改变权限或运行态。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 事件种类
EVENT_OWNER_PREFERENCE = "owner_preference"
EVENT_DECISION = "decision"
EVENT_ERROR = "error"
EVENT_WHY_DESIGNED = "why_designed"
EVENT_BEHAVIOR = "behavior"

# 知识抽取指示词
_PREF_HINTS = ("我习惯", "我喜欢", "我偏好", "owner 偏好", "owner偏好", "本喵喜欢", "别用",
               "不要用", "记得用", "总是用", "从来不用")
_DECISION_HINTS = ("决定", "采用", "拒绝", "否决", "批准", "选用", "选定", "采用方案")
_WHY_HINTS = ("因为", "设计原因", "为何", "为什么", "当初", "有意为之", "故意", "为了")


@dataclass
class MemoryEvent:
    """一条原始记忆事件。"""

    kind: str
    text: str
    tags: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "tags": list(self.tags),
                "ts": self.ts, "id": self.id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEvent":
        return cls(kind=d.get("kind", ""), text=d.get("text", ""),
                   tags=list(d.get("tags", [])), ts=float(d.get("ts", 0.0)),
                   id=d.get("id", ""))


@dataclass
class Knowledge:
    """从事件抽取的一条知识（事实/规则）。"""

    statement: str
    source_kind: str
    source_event_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"statement": self.statement, "source_kind": self.source_kind,
                "source_event_ids": list(self.source_event_ids),
                "confidence": round(self.confidence, 4), "ts": self.ts}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Knowledge":
        return cls(statement=d.get("statement", ""), source_kind=d.get("source_kind", ""),
                   source_event_ids=list(d.get("source_event_ids", [])),
                   confidence=float(d.get("confidence", 1.0)),
                   ts=float(d.get("ts", 0.0)))


@dataclass
class BehaviorPattern:
    """从事件归纳的一条反复出现的行为模式。"""

    pattern: str
    occurrences: int
    examples: list[str] = field(default_factory=list)
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "occurrences": self.occurrences,
                "examples": list(self.examples), "last_seen": self.last_seen}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BehaviorPattern":
        return cls(pattern=d.get("pattern", ""), occurrences=int(d.get("occurrences", 0)),
                   examples=list(d.get("examples", [])),
                   last_seen=float(d.get("last_seen", 0.0)))


# ---------------------------------------------------------------------------
# JSONL 读写（坏行隔离 + 原子追加）
# ---------------------------------------------------------------------------
def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # 先读现有内容，再整体重写（保证原子性与坏行隔离并存）
    existing: list[str] = []
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        existing.append(line)
        except OSError:
            existing = []
    existing.append(json.dumps(obj, ensure_ascii=False))
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# 抽取 / 归纳（确定性规则，无 LLM）
# ---------------------------------------------------------------------------
def extract_knowledge(event: MemoryEvent) -> list[Knowledge]:
    """从单条事件确定性抽取知识。返回 0..n 条 Knowledge。"""
    text = event.text
    out: list[Knowledge] = []
    low = text.lower()

    if event.kind == EVENT_OWNER_PREFERENCE or any(h in text for h in _PREF_HINTS):
        out.append(Knowledge(
            statement=f"Owner 偏好：{text}",
            source_kind=event.kind, source_event_ids=[event.id], confidence=1.0,
        ))
    if event.kind == EVENT_DECISION or any(h in text for h in _DECISION_HINTS):
        out.append(Knowledge(
            statement=f"历史决策：{text}",
            source_kind=event.kind, source_event_ids=[event.id], confidence=1.0,
        ))
    if event.kind == EVENT_WHY_DESIGNED or any(h in text for h in _WHY_HINTS):
        out.append(Knowledge(
            statement=f"设计原因：{text}",
            source_kind=event.kind, source_event_ids=[event.id], confidence=1.0,
        ))
    return out


def learn_patterns(events: list[MemoryEvent], *, min_occurrences: int = 3) -> list[BehaviorPattern]:
    """从事件流归纳反复出现的行为模式（按标签组合聚类 + 频次阈值）。

    启发式：以事件 ``tags`` 的**排序组合**为聚类键，使相同多标签事件归为一种模式
    （避免单标签重复计数产生冗余模式）；同一组合出现 >= min_occurrences 次即视为模式。
    """
    by_combo: dict[tuple, list[MemoryEvent]] = {}
    for ev in events:
        key = tuple(sorted(ev.tags))
        by_combo.setdefault(key, []).append(ev)

    patterns: list[BehaviorPattern] = []
    for combo, evs in by_combo.items():
        if len(evs) < min_occurrences:
            continue
        tag_str = ",".join(combo) if combo else "(无标签)"
        examples = [e.text for e in evs[:3]]
        last_seen = max(float(e.ts) for e in evs)
        patterns.append(BehaviorPattern(
            pattern=f"反复出现（标签组合={tag_str}）：{len(evs)} 次",
            occurrences=len(evs), examples=examples, last_seen=last_seen,
        ))
    # 按频次降序
    patterns.sort(key=lambda p: -p.occurrences)
    return patterns


# ---------------------------------------------------------------------------
# 长期记忆引擎
# ---------------------------------------------------------------------------
class LongTermMemory:
    """三层长期记忆引擎：事件 / 知识 / 行为模式（追加落盘，坏行隔离）。"""

    def __init__(self, data_dir: str = "data") -> None:
        self._root = Path(data_dir) / "long_term"
        self._events_path = self._root / "events.jsonl"
        self._knowledge_path = self._root / "knowledge.jsonl"
        self._patterns_path = self._root / "patterns.jsonl"

    # ---- 事件层 ----
    def record_event(self, kind: str, text: str, *, tags: list[str] | None = None,
                     ts: float | None = None) -> MemoryEvent:
        ev = MemoryEvent(kind=kind, text=text, tags=tags or [], ts=ts or time.time())
        # 简单 id（确定性 + 唯一）
        ev.id = f"ev-{int(ev.ts * 1000)}-{abs(hash(text)) % 100000:05d}"
        _append_jsonl(self._events_path, ev.to_dict())
        return ev

    def record_why(self, key: str, reason: str, *, ts: float | None = None) -> MemoryEvent:
        """记录「某模块/决策为何这样设计」（服务 P5 自治维护的核心上下文）。"""
        return self.record_event(
            EVENT_WHY_DESIGNED, f"{key}：{reason}", tags=["why_designed", key], ts=ts,
        )

    def events(self) -> list[MemoryEvent]:
        return [MemoryEvent.from_dict(d) for d in _read_jsonl(self._events_path)]

    # ---- 知识层 ----
    def extract_and_store(self, event: MemoryEvent) -> list[Knowledge]:
        """对一条事件抽取知识并追加落盘。返回抽到的知识。"""
        ks = extract_knowledge(event)
        for k in ks:
            _append_jsonl(self._knowledge_path, k.to_dict())
        return ks

    def knowledge(self) -> list[Knowledge]:
        return [Knowledge.from_dict(d) for d in _read_jsonl(self._knowledge_path)]

    def why_designed(self, key: str | None = None) -> list[Knowledge]:
        """查询设计原因知识；给定 key 则按标签过滤。"""
        all_k = self.knowledge()
        out = [k for k in all_k if k.source_kind == EVENT_WHY_DESIGNED]
        if key is not None:
            out = [k for k in out if key in k.statement]
        return out

    # ---- 行为模式层 ----
    def rebuild_patterns(self, *, min_occurrences: int = 3) -> list[BehaviorPattern]:
        """从全部事件重新归纳行为模式并落盘（幂等：先清空再写）。"""
        pats = learn_patterns(self.events(), min_occurrences=min_occurrences)
        # 重写 patterns.jsonl（清空旧归纳）
        self._patterns_path.parent.mkdir(parents=True, exist_ok=True)
        with self._patterns_path.open("w", encoding="utf-8") as fh:
            for p in pats:
                fh.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
        return pats

    def patterns(self) -> list[BehaviorPattern]:
        return [BehaviorPattern.from_dict(d) for d in _read_jsonl(self._patterns_path)]

    # ---- 查询 ----
    def query(self, text: str, *, limit: int = 10) -> dict[str, Any]:
        """跨知识层 + 行为模式层做关键词检索（确定性、无向量依赖）。

        fail-closed：无数据 → 空结果。
        """
        tokens = [t for t in re.split(r"\s+", text.lower()) if t]
        def _score(s: str) -> int:
            s_low = s.lower()
            return sum(1 for t in tokens if t in s_low)
        know = self.knowledge()
        pats = self.patterns()
        scored_k = sorted(
            ((_score(k.statement), k) for k in know), key=lambda x: -x[0]
        )
        def _pattern_score(p: BehaviorPattern) -> int:
            s = _score(p.pattern)
            # 行为模式的可搜索内容含示例文本（关键词常出现在示例中）
            for ex in p.examples:
                s = max(s, _score(ex))
            return s
        scored_p = sorted(
            ((_pattern_score(p), p) for p in pats), key=lambda x: -x[0]
        )
        top_k = [k.to_dict() for s, k in scored_k if s > 0][:limit]
        top_p = [p.to_dict() for s, p in scored_p if s > 0][:limit]
        return {
            "query": text,
            "knowledge_matches": top_k,
            "pattern_matches": top_p,
            "has_data": bool(know or pats),
        }
