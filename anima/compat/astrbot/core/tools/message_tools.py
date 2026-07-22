"""AstrBot compatibility re-export."""
from anima.compat.astrbot import AstrMessageEvent


class SendMessageToUserTool:
    """占位：向用户发送消息工具。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def execute(self, event: AstrMessageEvent | None = None, **kwargs) -> str:
        return ""
