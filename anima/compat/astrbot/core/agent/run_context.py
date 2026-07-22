"""AstrBot compatibility re-export."""
from typing import Any, Generic, TypeVar
from anima.compat.astrbot import Context

T = TypeVar("T")


class ContextWrapper(Generic[T]):
    """占位：运行上下文包装器。"""
    def __init__(self, context: Context | None = None):
        self.context = context

    def get_context(self) -> Context | None:
        return self.context

    @classmethod
    def __class_getitem__(cls, item):
        return cls


class RunContext:
    """占位：运行上下文。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
