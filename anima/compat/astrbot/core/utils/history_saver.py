"""AstrBot compatibility re-export."""
from typing import Any


class HistorySaver:
    """占位：历史记录保存器。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def save(self, data: Any) -> None:
        pass

    async def load(self) -> Any:
        return []


def persist_agent_history(history: Any, **kwargs) -> None:
    """占位：持久化 Agent 历史。"""
    pass
