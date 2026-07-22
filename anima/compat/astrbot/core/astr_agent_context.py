"""AstrBot compatibility re-export."""
from anima.compat.astrbot import Context


class AstrAgentContext:
    """占位：AstrBot Agent 上下文。"""
    def __init__(self, context: Context | None = None):
        self.context = context

    def get_context(self) -> Context | None:
        return self.context
