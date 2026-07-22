"""每 (群, 用户) 的即时会话缓冲，用于注入最近上下文。

与旧版"群级大桶"的根本区别：键是 (group_id, user_id)，因此：
- 同群不同用户的即时上下文互不干扰（不再串台）；
- bot 自身回复也按此键回写，不会污染他人。
"""
from __future__ import annotations

from collections import defaultdict, deque


class SessionBuffer:
    def __init__(self, max_turns: int = 8):
        self.max_turns = max_turns
        self._buf: dict[tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=max_turns)
        )

    def append(self, user_id: str, group_id: str, role: str, text: str) -> None:
        self._buf[(group_id, user_id)].append({"role": role, "content": text})

    def get(self, user_id: str, group_id: str) -> list[dict]:
        return list(self._buf[(group_id, user_id)])

    def clear(self, user_id: str, group_id: str) -> None:
        self._buf[(group_id, user_id)].clear()
