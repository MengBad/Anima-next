"""用户间关系边存储（user↔user）。

与 bot↔user 的 relation.py 不同，这里刻画【群成员之间的关系】
（张三和李四是室友 / 她和王五是死对头）。

设计要点：
- 关系边按「两人名字对」建索引，键为 ``sorted(lower(a), lower(b))`` 拼接，
  与名字出现顺序无关；原始展示名保留在 a/b 字段。
- 采用**名字字符串**而非 QQ 号做键：因为关系抽取发生在「某人发的一条消息」
  上下文里，此时不一定能稳定解析出对方 QQ 号。用消息里提到的称呼直接建边，
  查询时也用同样的称呼字符串反查，规避了抽取期的 uid 解析难题。
- 单条边可携带多个关系类型（relations）、多条证据笔记（notes）、多个来源群。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("anima.crossgroup.user_relations")

_REL_MAX = 5          # 单条边最多保留几种关系类型
_NOTE_MAX = 5         # 单条边最多保留几条证据笔记


def _edge_key(name_a: str, name_b: str) -> str:
    a, b = name_a.strip().lower(), name_b.strip().lower()
    return "|".join(sorted((a, b)))


def _canonical_name(name: str) -> str:
    """把称呼解析成稳定的「主显示名」，缓解同一人多称呼碎片化。

    优先经 CrossGroupManager 把 nickname/别名映射回该用户的真实主名
    （如「小张」→ 其主名「张三」），使同一人不同称呼落到同一条边。
    解析失败则原样返回（仍保留大小写/空白归一化的键）。
    """
    n = (name or "").strip()
    if not n:
        return n
    try:
        from anima.crossgroup import get_crossgroup_manager
        cgm = get_crossgroup_manager()
        hits = cgm.search_by_name(n)
        if hits:
            uid = hits[0][0]
            u = cgm.get_user(uid)
            if u is not None:
                canonical = getattr(u, "display_name", "") or getattr(u, "name", "") or ""
                if canonical:
                    return canonical.strip()
    except Exception:
        pass
    return n


class UserRelationStore:
    def __init__(self, data_dir: str = "data/crossgroup"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "user_relations.json")
        self._edges: dict[str, dict] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as _fh:
                    self._edges = json_load(_fh.read())
        except Exception:
            logger.exception("加载 user_relations.json 失败")
            self._edges = {}

    def _save(self) -> None:
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as _fh:
                _fh.write(json_dump(self._edges))
            os.replace(tmp, self._path)
        except Exception:
            logger.exception("保存 user_relations.json 失败")

    # ---------- 读写 ----------
    def add(self, name_a: str, name_b: str, relation: str,
            note: str = "", source: str = "") -> bool:
        """新增/合并一条关系边。返回是否发生了变更。"""
        name_a, name_b, relation = name_a.strip(), name_b.strip(), relation.strip()
        # P2: 把称呼归一化到主显示名，缓解同人多称呼碎片化
        name_a, name_b = _canonical_name(name_a), _canonical_name(name_b)
        if not name_a or not name_b or not relation:
            return False
        if name_a.lower() == name_b.lower():
            return False  # 自己跟自己不算关系边
        key = _edge_key(name_a, name_b)
        now = time.time()
        edge = self._edges.get(key)
        if edge is None:
            edge = {
                "a": name_a, "b": name_b,
                "relations": [], "notes": [], "sources": [],
                "updated": now, "created": now,
            }
            self._edges[key] = edge
        changed = False
        if relation not in edge["relations"]:
            edge["relations"].append(relation)
            edge["relations"] = edge["relations"][-_REL_MAX:]
            changed = True
        if note and note not in edge["notes"]:
            edge["notes"].append(note)
            edge["notes"] = edge["notes"][-_NOTE_MAX:]
            changed = True
        if source and source not in edge["sources"]:
            edge["sources"].append(source)
            changed = True
        edge["updated"] = now
        if changed:
            self._save()
        return changed

    def get(self, name_a: str, name_b: str) -> Optional[dict]:
        return self._edges.get(_edge_key(name_a, name_b))

    def query(self, name: str) -> list[dict]:
        """返回所有涉及 name 的关系边（按更新时间倒序）。"""
        q = _canonical_name(name).strip().lower()
        if not q:
            return []
        out = []
        for edge in self._edges.values():
            if q == edge["a"].lower() or q == edge["b"].lower():
                out.append(edge)
        out.sort(key=lambda e: -e.get("updated", 0))
        return out

    def count(self) -> int:
        return len(self._edges)


# 单例：按 data_dir 区分（同一进程通常只有一个）
_STORES: dict[str, UserRelationStore] = {}


def get_user_relation_store(data_dir: str = "data/crossgroup") -> UserRelationStore:
    store = _STORES.get(data_dir)
    if store is None:
        store = UserRelationStore(data_dir)
        _STORES[data_dir] = store
    return store


# 内联轻量 JSON 读写（避免循环依赖 persistence 模块）
import json as _json  # noqa: E402


def json_load(s: str) -> dict:
    try:
        return _json.loads(s) if isinstance(s, str) else s
    except Exception:
        return {}


def json_dump(obj: Any) -> str:
    return _json.dumps(obj, ensure_ascii=False, indent=2)
