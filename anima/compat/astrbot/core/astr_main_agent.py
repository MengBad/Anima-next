"""AstrBot compatibility re-export."""
from anima.compat.astrbot import Context


class AstrMainAgent:
    """占位：AstrBot 主 Agent。"""
    def __init__(self, context: Context | None = None):
        self.context = context
