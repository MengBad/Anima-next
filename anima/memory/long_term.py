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

P3-1（向量化）的边界：保留上面全部纪律，语义召回做成**可选增量读路径**
（``query_semantic``，需显式注入 embedder，且由 ``memory.long_term_semantic``
开关控制，默认 OFF）。理由：
- 本模块每条记录都是「Owner 偏好/决策/设计原因」这类**短陈述**，关键词检索本就
  很准，向量的边际收益有限；
- 但中文长查询在原实现下**根本命中不了**（见 ``_tokens`` 的注释），所以先修确定性
  分词，再让向量只负责「换个说法也能召回」；
- embedding 是远程调用（延迟 + 费用），绝不能悄悄塞进每条消息的认知注入路径。
  所以它只服务 Owner 主动查询（控制面 / WebUI），不进 per-message 热路径。

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


# ---------------------------------------------------------------------------
# 分词（P3-1）
# ---------------------------------------------------------------------------
_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_TOKEN_LIMIT = 96


def _tokens(text: str) -> list[str]:
    """把查询切成可比对的最小单位。

    **P3-1 修的是一处真实缺陷**：旧实现用 ``re.split(r"\\s+", text.lower())`` 分词，
    中文没有空格 → 整句变成**一个** token，于是 ``token in haystack`` 实际要求
    「整句原样出现在记忆里」，稍长一点的中文查询必然零命中。
    （``query("中文")`` 这类短查询碰巧能过，所以问题长期被掩盖。）

    现在：
    - ASCII 词：按非字母数字切分、小写（``pytest`` / ``rbac.py`` → ``rbac``、``py``）；
    - CJK 连续段：切 bigram（单字段落退化为该字本身）。
    """
    lowered = (text or "").lower()
    toks: list[str] = [w for w in _ASCII_WORD_RE.findall(lowered) if w]
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    # 去重保序 + 上限（超长查询会把「命中数」放大成噪声）
    seen: set[str] = set()
    out: list[str] = []
    for tok in toks:
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= _TOKEN_LIMIT:
            break
    return out


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """余弦相似度；维度不一致/全零/缺失一律返回 0.0（不猜、不放大）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))



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
    from anima.core.persistence import atomic_write_text
    atomic_write_text(path, "\n".join(existing) + "\n")


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
    """三层长期记忆引擎：事件 / 知识 / 行为模式（追加落盘，坏行隔离）。

    ``embedder``（可选，P3-1）：注入 ``memory.vector_memory.Embedder`` 后，
    ``query_semantic()`` 可在关键词检索之上叠加语义召回。**默认 None**——
    本模块「无 LLM / 确定性 / 可复现」的设计纪律不变：所有写入路径、所有
    抽取归纳、以及 ``query()`` 都不依赖 embedder；它只影响那条可选读路径。
    """

    def __init__(self, data_dir: str = "data", embedder: Any = None) -> None:
        self._root = Path(data_dir) / "long_term"
        self._events_path = self._root / "events.jsonl"
        self._knowledge_path = self._root / "knowledge.jsonl"
        self._patterns_path = self._root / "patterns.jsonl"
        # P3-1：可选语义读路径。运行期可由上层赋值（core 初始化向量记忆后回填）。
        self.embedder = embedder

    # ---- 事件层 ----
    def record_event(self, kind: str, text: str, *, tags: list[str] | None = None,
                     ts: float | None = None) -> MemoryEvent:
        ev = MemoryEvent(kind=kind, text=text, tags=tags or [], ts=ts or time.time())
        # 简单 id（确定性 + 唯一）
        ev.id = f"ev-{int(ev.ts * 1000)}-{abs(hash(text)) % 100000:05d}"
        _append_jsonl(self._events_path, ev.to_dict())
        return ev

    def record_why(self, key: str, reason: str, *, ts: float | None = None) -> MemoryEvent:
        """记录「某模块/决策为何这样设计」（服务 P5 自治维护的核心上下文）。

        自动抽取到知识层，使 ``why_designed()`` 立即可查（无需额外调用 extract_and_store）。
        """
        ev = self.record_event(
            EVENT_WHY_DESIGNED, f"{key}：{reason}", tags=["why_designed", key], ts=ts,
        )
        self.extract_and_store(ev)
        return ev

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
        from anima.core.persistence import atomic_write_text
        atomic_write_text(
            self._patterns_path,
            "".join(json.dumps(p.to_dict(), ensure_ascii=False) + "\n" for p in pats))
        return pats

    def patterns(self) -> list[BehaviorPattern]:
        return [BehaviorPattern.from_dict(d) for d in _read_jsonl(self._patterns_path)]

    # ---- 查询 ----
    def query(self, text: str, *, limit: int = 10) -> dict[str, Any]:
        """跨知识层 + 行为模式层做关键词检索（确定性、无向量依赖）。

        fail-closed：无数据 → 空结果。
        """
        tokens = _tokens(text)
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

    # ---- 语义召回（P3-1，可选）----
    def _semantic_candidates(self) -> list[tuple[str, str]]:
        """待做语义比对的候选文本。

        顺序即优先级：**知识层按最近写入优先**（新记下的偏好/决策通常最相关），
        行为模式与模式示例排在其后——因为候选有上限，顺序决定了谁会被截掉。
        """
        out: list[tuple[str, str]] = [
            ("knowledge", k.statement) for k in reversed(self.knowledge())
        ]
        for p in self.patterns():
            out.append(("pattern", p.pattern))
            out.extend(("pattern_example", ex) for ex in p.examples)
        return [(kind, txt) for kind, txt in out if (txt or "").strip()]

    async def _embed(self, text: str) -> list[float] | None:
        """取一条文本的向量；任何异常/缺失 → None（绝不把失败当 0 向量）。"""
        if self.embedder is None:
            return None
        try:
            vec = await self.embedder.embed(text)
        except Exception:
            return None
        return vec if vec else None

    async def query_semantic(self, text: str, *, limit: int = 10,
                             threshold: float = 0.35,
                             max_candidates: int = 60) -> dict[str, Any]:
        """关键词检索 **+ 可选**语义召回（需注入 embedder）。

        设计纪律（向量是增量，不是替代）：

        1. **关键词结果永远先算**——确定性、零外部依赖、零成本；向量挂掉它照旧返回；
        2. embedder 缺失 / 调用失败 / 空向量 → 如实标注 ``vector_used=False`` 与
           ``vector_reason``，**绝不**把「没做语义检索」伪装成「语义检索没找到」；
        3. 候选集有上限 ``max_candidates``，超出时把 ``candidates_total`` 与
           ``candidates_scanned`` 都报出来，漏扫多少一目了然；
        4. 只读：不改任何落盘内容（连访问计数都不动）。
        """
        base = self.query(text, limit=limit)
        out: dict[str, Any] = dict(base)
        out.update({
            "vector_used": False,
            "vector_reason": "",
            "semantic_matches": [],
            "candidates_total": 0,
            "candidates_scanned": 0,
        })

        cands = self._semantic_candidates()
        out["candidates_total"] = len(cands)
        if self.embedder is None:
            out["vector_reason"] = "未注入 embedder：本次仅关键词检索"
            return out
        if not cands:
            out["vector_reason"] = "无知识/行为模式可做语义比对"
            return out
        if not str(text or "").strip():
            out["vector_reason"] = "空查询：本次仅关键词检索"
            return out

        limit_n = max(1, int(max_candidates))
        scanned = cands[:limit_n]
        out["candidates_scanned"] = len(scanned)
        try:
            thr = float(threshold)
        except (TypeError, ValueError):
            thr = 0.35

        qv = await self._embed(text)
        if qv is None:
            out["vector_reason"] = "查询向量不可得（embedding 失败/未配置）：降级为关键词检索"
            return out

        hits: list[dict[str, Any]] = []
        failed = 0
        for kind, txt in scanned:
            cv = await self._embed(txt)
            if cv is None:
                failed += 1
                continue
            sim = _cosine(qv, cv)
            if sim >= thr:
                hits.append({"kind": kind, "text": txt,
                             "similarity": round(sim, 4)})
        if failed >= len(scanned):
            out["vector_reason"] = (
                f"全部 {failed} 条候选向量不可得：降级为关键词检索")
            return out
        hits.sort(key=lambda d: -d["similarity"])
        out["semantic_matches"] = hits[:max(1, int(limit))]
        out["vector_used"] = True
        if failed:
            out["vector_reason"] = f"{failed} 条候选向量不可得，已跳过（结果不完整）"
        if len(cands) > len(scanned):
            note = (f"候选 {len(cands)} 条，仅扫描最近写入的前 {len(scanned)} 条"
                    f"（阈值 {thr}）")
            out["vector_reason"] = (out["vector_reason"] + "；" + note) if out["vector_reason"] else note
        return out

