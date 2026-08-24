"""Owner 忽略名单：群内「别理某人」的持久化 + 闸门拦截。

背景：owner 在群里说「以后别再理刚刚那两个人了」，bot 之前只口头应允，
没有落地成可执行规则。本模块提供：
- IgnoreStore：按 scope 持久化忽略的 QQ uid（data/ignore_list.json）。
  scope = str(group_id) 或 "global"（私聊）。
- matches_ignore_directive / resolve_ignore_targets：识别 owner 的自然语言
  「别理那两人」并把目标解析到最近发言的非 owner 用户，构成“真做”的动作。
- should_skip_ignored：供回复闸门判定是否直接跳过。

只读模块：add/remove 写盘，其余为查。写盘用原子替换，_lock 保证线程安全。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from typing import Any

GLOBAL = "global"

# 触发词：出现其一即可能是“别理”指令
_TRIGGER_WORDS = ("别理", "不再理", "不要理", "别鸟", "别搭理", "冷落", "别再理")
# 目标提示：出现其一表示“那（两）人/他/她”这类可指向发言者的说法
_TARGET_HINTS = ("人", "两个", "俩", "那", "这", "他", "她", "他们", "它们")
# “两人/俩”等复数词：命中则忽略最近 2 人，否则 1 人
_PLURAL_WORDS = ("两个", "俩", "两个人", "俩人", "2个", "两位")


class IgnoreStore:
    """按 scope 持久化被忽略的 QQ uid。"""

    def __init__(self, data_dir: str = "data"):
        self.path = Path(data_dir) / "ignore_list.json"
        self._lock = threading.RLock()
        self._data: dict[str, list[str]] = self._load()

    # -- 私有 --
    def _load(self) -> dict[str, list[str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): [str(x) for x in v] for k, v in raw.items() if isinstance(v, list)}
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            # 忽略名单写失败绝不阻断主流程
            pass

    # -- 查询 --
    def is_ignored(self, scope: Any, uid: Any) -> bool:
        with self._lock:
            return str(int(uid)) in self._data.get(str(scope), [])

    def list(self, scope: Any) -> list[str]:
        with self._lock:
            return list(self._data.get(str(scope), []))

    # -- 写 --
    def add(self, scope: Any, uids: list[int]) -> int:
        with self._lock:
            key = str(scope)
            s = set(self._data.get(key, []))
            for u in uids:
                s.add(str(int(u)))
            self._data[key] = sorted(s)
            self._save()
            return len(s)

    def remove(self, scope: Any, uid: Any) -> bool:
        with self._lock:
            key = str(scope)
            s = set(self._data.get(key, []))
            d = str(int(uid))
            if d not in s:
                return False
            s.discard(d)
            self._data[key] = sorted(s)
            self._save()
            return True


def matches_ignore_directive(text: str) -> bool:
    """判断一条 owner 消息是否像「别理某人」指令（保守匹配）。"""
    if not text:
        return False
    has_trigger = any(w in text for w in _TRIGGER_WORDS)
    has_target = any(w in text for w in _TARGET_HINTS)
    return bool(has_trigger and has_target)


def resolve_ignore_targets(text: str, recent_nonowner: list[int]) -> list[int]:
    """把「别理那两人」解析成要忽略的 uid。

    recent_nonowner：该群最近发言的非 owner 用户（最近在前）。
    复数提示词命中 → 取最多 2 人；否则取 1 人。
    """
    n = 2 if any(w in text for w in _PLURAL_WORDS) else 1
    return [int(u) for u in recent_nonowner[:n]]


def should_skip_ignored(store: IgnoreStore | None, detail_type: str,
                        scope: Any, uid: Any) -> bool:
    """回复闸门：该（群,用户）命中忽略名单则跳过。只对群生效。"""
    if detail_type != "group":
        return False
    if store is None or scope is None:
        return False
    try:
        return store.is_ignored(scope, uid)
    except Exception:
        return False
