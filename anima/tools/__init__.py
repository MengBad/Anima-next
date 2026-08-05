"""Anima 工具系统：Tool / ToolRegistry 核心框架。

内置工具实现位于 anima.tools.builtin 子包；
能力工具工厂位于 anima.tools.capability。

权限：
- 每个工具可设置 min_level（0-4，对应 banned/readonly/user/admin/owner）。
- ReAct 调用时会根据当前用户权限等级过滤 schema，并在执行前二次校验。
"""

from __future__ import annotations
import asyncio
import contextvars
import json
import re
import time
import logging
from typing import Any, Callable, Optional

from anima.capability.index import CapabilityIndex
from anima.capability.scopes import PermissionScopes

logger = logging.getLogger("anima.tools")
_CALL_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("anima_tool_context", default={})
_TOOL_FAILURE_PREFIXES = (
    "工具执行错误", "错误:", "[沙箱]", "联网搜索失败", "天气查询失败",
    "查询失败", "暂时不可用", "工具暂时不可用",
)
_TOOL_INTERNAL_MARKERS = (
    "[stderr]", "Traceback", "Operation not permitted", "Permission denied", "unshare failed",
)
_SAFE_TOOL_FAILURE = "工具暂时不可用，未获得可验证结果。"


def get_call_context() -> dict[str, Any]:
    return _CALL_CONTEXT.get()


def tool_result_ok(result: object) -> bool:
    text = str(result or "").strip()
    lower = text.lower()
    return bool(text) and not any(lower.startswith(prefix.lower()) for prefix in _TOOL_FAILURE_PREFIXES) \
        and not any(marker.lower() in lower for marker in _TOOL_INTERNAL_MARKERS)


def normalize_tool_result(result: object) -> tuple[str, bool]:
    """统一工具出站契约：失败细节留日志，模型只得到稳定、可验证的状态。"""
    text = str(result or "").strip()
    if not tool_result_ok(text):
        return _SAFE_TOOL_FAILURE, False
    return text, True


# 工具所需最低权限等级
LEVEL_BANNED = 0
LEVEL_READONLY = 1
LEVEL_USER = 2
LEVEL_ADMIN = 3
LEVEL_OWNER = 4


class Tool:
    """一个可被 LLM 调用的工具。"""

    def __init__(self, name: str, description: str, parameters: dict,
                 handler: Callable, min_level: int = LEVEL_USER,
                 required: Optional[list] = None, danger: bool = False,
                 enabled: bool = True,
                 capability_ids: Optional[list] = None,
                 granted_scopes: Any = None):
        self.name = name
        self.description = description
        self.parameters = parameters  # JSON Schema properties
        # 显式 required 列表；为 None 时由 to_openai_schema 回退到 parameters 的 keys
        self.required = required
        self.handler = handler
        self.min_level = min_level
        self.danger = danger
        # 允许运行时由插件启用/停用；默认工具保持原有行为。
        self.enabled = enabled
        # --- M0.3 (RFC-0001) Capability 元数据：additive，默认空，不改变任何既有语义 ---
        # name 仍是唯一执行标识；capability_ids 只是「这个实现满足哪些能力」的声明。
        self.capability_ids: list[str] = [str(c) for c in (capability_ids or []) if str(c)]
        # Provider 被授予的权限域（RFC-0001 §7）；None → 空集（不授予任何 scope）。
        self.granted_scopes = PermissionScopes.from_mapping(granted_scopes)

    def to_openai_schema(self) -> dict:
        required = self.required if self.required is not None else list(self.parameters.keys())
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    """工具注册中心。"""

    def __init__(self, core=None, capability_mode: bool = False):
        self.tools: dict[str, Tool] = {}
        self.core = core
        # --- M0.3 (RFC-0001) Capability Adapter ---
        # 索引是 Registry 之上的查询视图；Registry 仍是唯一 execution owner。
        self.capability_index = CapabilityIndex()
        # feature flag：默认关闭 → get_schemas 行为 100% 不变。
        self.capability_mode = bool(capability_mode)
        self._capability_resolver = None

    @property
    def capability_resolver(self):
        """惰性构造的 Resolver（只消费索引，不执行工具）。"""
        if self._capability_resolver is None:
            from anima.capability.resolver import CapabilityResolver
            self._capability_resolver = CapabilityResolver(self.capability_index)
        return self._capability_resolver

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool
        self.capability_index.index_tool(tool)
        logger.debug("注册工具: %s (level=%d)", tool.name, tool.min_level)

    def unregister(self, name: str) -> bool:
        """移除一个工具，供插件卸载和热重载使用。"""
        if name not in self.tools:
            return False
        self.tools.pop(name, None)
        self.capability_index.remove_tool(name)
        logger.debug("移除工具: %s", name)
        return True

    def set_call_context(self, **context: Any):
        """绑定当前 asyncio 任务的会话上下文，避免并发消息串群。"""
        return _CALL_CONTEXT.set(context)

    def get_call_context(self) -> dict[str, Any]:
        """读取当前任务上下文；用于把工具生成的受控媒体回传给出站层。"""
        return _CALL_CONTEXT.get()

    def reset_call_context(self, token: contextvars.Token) -> None:
        _CALL_CONTEXT.reset(token)

    def get_schemas(self, permission_level: int = LEVEL_USER,
                    capability_mode: Optional[bool] = None) -> list[dict]:
        """根据用户权限等级过滤可使用的工具 schema。

        M0.3 双模式（RFC-0001 §5.4）：
        - ``capability_mode=False``（默认）：legacy Tool schema，行为逐字节不变；
        - ``capability_mode=True``：输出 capability schema（不含任何 Provider 名），
          未标注 capability 的 legacy Tool 仍以 Tool schema 暴露（迁移期混合）。

        参数为 None 时取实例 flag ``self.capability_mode``（默认 False）。
        """
        use_capability = self.capability_mode if capability_mode is None else bool(capability_mode)
        if not use_capability:
            return [
                t.to_openai_schema()
                for t in self.tools.values()
                if t.enabled and permission_level >= t.min_level
            ]
        from anima.capability.schema import capability_schemas, legacy_only_schemas
        return (
            capability_schemas(self.capability_index, permission_level)
            + legacy_only_schemas(list(self.tools.values()), permission_level)
        )

    async def call_capability(self, capability_ref: str, args: dict,
                              permission_level: int = LEVEL_USER) -> str:
        """capability 模式的调用入口：Resolver 选 Provider，执行仍走 ``call()``。

        ``capability_ref`` 可以是 capability id（``search.web``）或其 schema name
        （``search_web``，LLM 侧看到的名字）。``call(name, args)`` 语义不变。
        """
        cap_id = self.capability_index.capability_for_schema_name(capability_ref) or capability_ref
        resolution = self.capability_resolver.resolve(cap_id, permission_level=permission_level)
        return await self.call(resolution.provider.tool_name, args, permission_level)

    def can_call(self, name: str, permission_level: int) -> bool:
        tool = self.tools.get(name)
        if tool is None:
            return False
        return tool.enabled and permission_level >= tool.min_level

    async def call(self, name: str, args: dict,
                   permission_level: int = LEVEL_USER) -> str:
        """调用工具并返回结果字符串。"""
        tool = self.tools.get(name)
        if tool is None:
            return f"错误: 工具 '{name}' 不存在"

        if not self.can_call(name, permission_level):
            self._audit("tool_denied", tool=name, permission_level=permission_level)
            return f"错误: 你的权限不足，无法调用工具 '{name}'"

        if tool.danger and self._requires_approval():
            store = getattr(self.core, "approvals", None)
            if store is None:
                return "错误: 高危工具审批服务不可用，已拒绝执行"
            item = store.create(name, args, get_call_context(), permission_level,
                                self.core.config.security.approval_ttl_sec)
            self._audit("tool_approval_requested", approval_id=item.id, tool=name,
                        args_digest=item.args_digest, permission_level=permission_level)
            return f"等待主人审批: id={item.id}，请求将在过期前保持待执行"

        try:
            return await self._invoke(tool, args)
        except Exception as e:
            logger.exception("工具 %s 执行异常", name)
            self._audit("tool_failed", tool=name, error=type(e).__name__)
            return _SAFE_TOOL_FAILURE

    async def approve_and_execute(self, approval_id: str, owner_id: int) -> str:
        """owner 批准后才执行已持久化的危险调用，且不再进入审批分支。"""
        store = getattr(self.core, "approvals", None)
        if store is None:
            raise ValueError("审批服务不可用")
        item = store.get_pending(approval_id)
        if item is None:
            raise ValueError("未找到待审批项目或审批已过期")
        tool = self.tools.get(item.tool)
        if tool is None or not tool.danger:
            store.mark(approval_id, "rejected", owner_id)
            raise ValueError("原工具不可用或不再是高危工具，已拒绝执行")
        store.mark(approval_id, "approved", owner_id)
        token = self.set_call_context(**item.context)
        try:
            result = await self._invoke(tool, item.args)
        except Exception as exc:
            self._audit("tool_approval_execution_failed", approval_id=approval_id, tool=tool.name,
                        error=type(exc).__name__)
            return f"审批已记录，但工具执行失败: {type(exc).__name__}"
        finally:
            self.reset_call_context(token)
        self._audit("tool_approved_executed", approval_id=approval_id, tool=tool.name,
                    reviewer=int(owner_id))
        return str(result)

    def reject_approval(self, approval_id: str, owner_id: int) -> None:
        store = getattr(self.core, "approvals", None)
        if store is None:
            raise ValueError("审批服务不可用")
        item = store.mark(approval_id, "rejected", owner_id)
        self._audit("tool_approval_rejected", approval_id=item.id, tool=item.tool,
                    args_digest=item.args_digest, reviewer=int(owner_id))

    async def _invoke(self, tool: Tool, args: dict[str, Any]) -> str:
        # run_python 特殊处理：从 call context 提取入站图片注入沙箱
        if tool.name == "run_python":
            ctx = get_call_context()
            images_b64 = ctx.get("input_images_b64")
            if images_b64 and "input_images_b64" not in args:
                args = {**args, "input_images_b64": images_b64}
            # Phase 1：把当前调用上下文的权限等级透传给沙箱，驱动分级能力。
            if "permission_level" not in args:
                args = {**args, "permission_level": ctx.get("permission_level", LEVEL_USER)}
            logger.info("run_python 调用: code_len=%d has_images=%s code_preview=%.200s",
                        len(args.get("code", "")), bool(images_b64), args.get("code", ""))
        result = tool.handler(**args)
        if asyncio.iscoroutine(result):
            result = await result
        # run_python 原始输出日志（在 normalize 之前，方便排查沙箱问题）
        if tool.name == "run_python":
            logger.info("run_python raw_result (len=%d): %.500s", len(str(result)), str(result))
        text, ok = normalize_tool_result(result)
        # 检测沙箱图片输出，加入 call context 供出站使用
        if tool.name == "run_python" and "[output_image]" in text:
            import re as _re
            _img_paths = _re.findall(r"\[output_image\]\s*(\S+)", text)
            if _img_paths:
                ctx = get_call_context()
                existing = ctx.setdefault("generated_image_paths", [])
                if isinstance(existing, list):
                    existing.extend(_img_paths[:2])
        self._audit("tool_executed", tool=tool.name, ok=ok, result_len=len(text))
        return text

    def _requires_approval(self) -> bool:
        return bool(getattr(getattr(self.core, "config", None), "security", None)
                    and self.core.config.security.require_approval_for_danger)

    def _audit(self, event_type: str, **fields: Any) -> None:
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit(event_type, **fields)


# ---------------------------------------------------------------------------
# create_default_registry — 委托 builtin 子包
# ---------------------------------------------------------------------------

def create_default_registry(core=None) -> ToolRegistry:
    """创建并注册全部内置工具，返回 ToolRegistry。"""
    from anima.tools.builtin import register_all_builtin
    registry = ToolRegistry(core)
    register_all_builtin(registry, core)
    return registry


# ---------------------------------------------------------------------------
# 向后兼容导出（旧代码 from anima.tools import xxx 不需要改动）
# ---------------------------------------------------------------------------

from anima.tools.builtin.system import tool_runtime_status as _tool_runtime_status  # noqa: E402
from anima.tools.builtin.sticker import make_sticker_tools  # noqa: E402
from anima.tools.capability import (  # noqa: E402
    make_capability_tool,
    register_capability_as_tool,
    _safe_capability_tool_name,
)

# 旧名称兼容
from anima.tools.builtin.system import (  # noqa: E402
    tool_time as _tool_time,
    tool_calc as _tool_calc,
    tool_weather as _tool_weather,
)
from anima.tools.builtin.web import (  # noqa: E402
    tool_web_search as _tool_web_search,
    _strip_tags,
    _WEB_PAGE_CACHE,
    _WEB_PAGE_CACHE_LOCK,
    _WEB_PAGE_CACHE_TTL_SEC,
    _WEB_PAGE_CACHE_MAX,
)
from anima.tools.builtin.file import write_file as _write_file, make_file_tool as _make_file_tool  # noqa: E402
