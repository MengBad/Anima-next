"""AstrBot compatibility re-export."""
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class FunctionTool(Generic[T]):
    """占位：函数工具定义。"""
    def __init__(self, name: str = "", func: Callable | None = None, **kwargs):
        self.name = name
        self.func = func
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def execute(self, **params) -> Any:
        if self.func is None:
            return ""
        return await self.func(**params)

    @classmethod
    def __class_getitem__(cls, item):
        return cls


class ToolExecResult:
    """占位：工具执行结果。"""
    def __init__(self, result: Any = None, success: bool = True):
        self.result = result
        self.success = success


class ToolSet:
    """占位：工具集合。"""
    def __init__(self, tools: list | None = None):
        self.tools = tools or []

    def add(self, tool: Any) -> None:
        self.tools.append(tool)

    def get(self, name: str) -> Any | None:
        for t in self.tools:
            if getattr(t, "name", None) == name:
                return t
        return None

    def pop(self, name: str, default: Any = None) -> Any:
        for i, t in enumerate(self.tools):
            if getattr(t, "name", None) == name:
                return self.tools.pop(i)
        return default
