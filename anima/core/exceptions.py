"""统一异常层级 —— 所有子系统的异常均继承自 AnimaError。

使用规范：
- 模块内部可抛出具体子类，便于上层精确捕获
- Pipeline 阶段捕获 AnimaError 后可安全降级（日志 + 跳过）
- WebUI API 捕获 AnimaError 后返回结构化 JSON 错误
- 未预期异常仍使用原生 Exception，由顶层 try/except 兜底
"""

from __future__ import annotations
from typing import Any, Optional


class AnimaError(Exception):
    """所有 Anima 子系统异常的根基。"""

    def __init__(self, message: str = "", *, detail: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class ConfigError(AnimaError):
    """配置加载/校验/变更失败。"""


class ConfigValidationError(ConfigError):
    """配置值不合法（pydantic 校验失败等）。"""


# ---------------------------------------------------------------------------
# 渠道 / 通道
# ---------------------------------------------------------------------------

class ChannelError(AnimaError):
    """QQ 渠道（OneBot11 / NapCat）通信异常。"""


class ChannelNotConnected(ChannelError):
    """WS 未连接或已断开。"""


class SendMessageError(ChannelError):
    """出站消息发送失败。"""

    def __init__(self, message: str = "", *, target_id: int = 0, **kwargs):
        super().__init__(message, **kwargs)
        self.target_id = target_id


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

class LLMError(AnimaError):
    """LLM 调用异常（超时/网关错误/解码失败等）。"""

    def __init__(self, message: str = "", *, status_code: Optional[int] = None, **kwargs):
        super().__init__(message, **kwargs)
        self.status_code = status_code


class LLMTimeoutError(LLMError):
    """LLM 请求超时。"""


class LLMCircuitOpen(LLMError):
    """熔断器开启，拒绝请求。"""


class LLMRateLimited(LLMError):
    """上游 429 限流。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

class ToolExecutionError(AnimaError):
    """工具执行失败。"""

    def __init__(self, message: str = "", *, tool_name: str = "", **kwargs):
        super().__init__(message, **kwargs)
        self.tool_name = tool_name


class ToolPermissionDenied(ToolExecutionError):
    """用户权限不足，工具调用被拒绝。"""


class SandboxError(ToolExecutionError):
    """沙箱执行异常（bwrap 启动失败/超时/资源限制）。"""


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class PipelineStageError(AnimaError):
    """Pipeline 阶段内部异常。"""

    def __init__(self, message: str = "", *, stage_index: int = -1, stage_name: str = "", **kwargs):
        super().__init__(message, **kwargs)
        self.stage_index = stage_index
        self.stage_name = stage_name


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

class PersistenceError(AnimaError):
    """数据读写异常（磁盘满/JSON 损坏/权限不足）。"""


# ---------------------------------------------------------------------------
# 认知模块
# ---------------------------------------------------------------------------

class CognitiveError(AnimaError):
    """认知子系统内部异常。"""

    def __init__(self, message: str = "", *, module_name: str = "", **kwargs):
        super().__init__(message, **kwargs)
        self.module_name = module_name


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------

class PermissionError(AnimaError):
    """权限校验失败（注意：与内置 PermissionError 同名，导入时需用完整路径）。"""

    def __init__(self, message: str = "", *, user_id: int = 0, required_level: int = 0, **kwargs):
        super().__init__(message, **kwargs)
        self.user_id = user_id
        self.required_level = required_level
