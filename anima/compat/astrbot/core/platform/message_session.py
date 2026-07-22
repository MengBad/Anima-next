"""AstrBot compatibility re-export."""
from typing import Any


class MessageSession:
    """占位：消息会话。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def send(self, *args, **kwargs) -> Any:
        return None

    async def reply(self, *args, **kwargs) -> Any:
        return None
