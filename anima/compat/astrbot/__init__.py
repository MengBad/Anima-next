"""AstrBot 兼容层 —— 为第三方 AstrBot 插件提供 API 垫片。

提供模块：
- astrbot.star: Star 基类 + 装饰器
- astrbot.message: AstrMessageEvent / MessageChain / Comp
- astrbot.context: Context
- astrbot.api: 占位（含 AstrBotConfig、logger、llm_tool）
- astrbot.api.event: 事件/Filter 相关
- astrbot.api.event.filter: filter 装饰器
- astrbot.api.message_components: 消息组件
- astrbot.api.provider: 占位
- astrbot.core.*: 核心类占位

设计目标：让按 AstrBot 风格编写的插件能在独立 Anima 运行时中加载，
无需安装 AstrBot 本身。本期覆盖 Tier 1：filter、message chain、事件、
Context 注入、LLM 提示钩子、AstrBotConfig、StarTools。
"""

from __future__ import annotations
import asyncio
import logging
import re
import time
import inspect
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union, get_args, get_origin, get_type_hints

from anima.core.gateway import OutboundMsg
from anima.llm.usage import llm_scope

logger = logging.getLogger("anima.compat")

# ---------------------------------------------------------------------------
# 消息段与消息链
# ---------------------------------------------------------------------------

class Comp:
    @staticmethod
    def text(content: str) -> dict:
        return {"type": "text", "data": {"text": content}}

    @staticmethod
    def at(qq: int | str) -> dict:
        return {"type": "at", "data": {"qq": str(qq)}}

    @staticmethod
    def image(file: str) -> dict:
        return {"type": "image", "data": {"file": file}}

    @staticmethod
    def file(file: str, name: str = "") -> dict:
        data = {"file": file}
        if name:
            data["name"] = name
        return {"type": "file", "data": data}

    @staticmethod
    def record(file: str) -> dict:
        return {"type": "record", "data": {"file": file}}

    @staticmethod
    def reply(msg_id: int | str) -> dict:
        return {"type": "reply", "data": {"id": str(msg_id)}}

    @staticmethod
    def xml(data: str) -> dict:
        return {"type": "xml", "data": {"data": data}}

    @staticmethod
    def json(data: str) -> dict:
        return {"type": "json", "data": {"data": data}}


class MessageChain:
    def __init__(self, segments: Optional[list] = None):
        self.chain = segments or []

    @classmethod
    def chain(cls, *segments) -> "MessageChain":
        return cls(list(segments))

    @classmethod
    def plain(cls, text: str) -> "MessageChain":
        return cls([Comp.text(text)])

    @classmethod
    def of_array(cls, segments: list[dict]) -> "MessageChain":
        return cls(segments)

    def get_plain_text(self) -> str:
        texts = []
        for seg in self.chain:
            if isinstance(seg, dict) and seg.get("type") == "text":
                texts.append(seg.get("data", {}).get("text", ""))
        return "".join(texts)

    def has_image(self) -> bool:
        return any(isinstance(seg, dict) and seg.get("type") == "image" for seg in self.chain)

    def message(self, text: str) -> "MessageChain":
        self.chain.append(Comp.text(text))
        return self

    def file_image(self, path: str) -> "MessageChain":
        self.chain.append(Comp.image(path))
        return self

    def url_image(self, url: str) -> "MessageChain":
        self.chain.append(Comp.image(url))
        return self


class MessageEventResult:
    """AstrBot 风格结果对象。"""
    def __init__(self, chain=None, message: str = "", use_t2i_: bool = False, **kwargs):
        self.chain = chain
        self.message_text = message
        self.use_t2i = use_t2i_
        self.extra = kwargs

    def message(self, text: str) -> "MessageEventResult":
        self.message_text = text
        return self


# ---------------------------------------------------------------------------
# 消息组件（astrbot.api.message_components）
# ---------------------------------------------------------------------------

class BaseMessageComponent:
    def __init__(self, type_: str, **data):
        self.type = type_
        self.data = data

    def to_dict(self) -> dict:
        return {"type": self.type, "data": self.data}

    @classmethod
    def from_dict(cls, d: dict):
        return cls(d.get("type", ""), **d.get("data", {}))

    @classmethod
    def fromURL(cls, url: str):
        return cls("image", file=url)

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls("image", file=path)


class Plain(BaseMessageComponent):
    def __init__(self, text: str = ""):
        super().__init__("text", text=text)
        self.text = text


class Image(BaseMessageComponent):
    def __init__(self, file: str = "", url: str = ""):
        super().__init__("image", file=file or url)
        self.file = file or url

    @classmethod
    def fromURL(cls, url: str):
        return cls(url=url)

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(file=path)


class Reply(BaseMessageComponent):
    def __init__(self, id: int | str = 0, text: str = ""):
        super().__init__("reply", id=str(id), text=text)


class Record(BaseMessageComponent):
    def __init__(self, file: str = ""):
        super().__init__("record", file=file)


class File(BaseMessageComponent):
    def __init__(self, file: str = "", name: str = ""):
        super().__init__("file", file=file, name=name)


class Json(BaseMessageComponent):
    def __init__(self, content: str = ""):
        super().__init__("json", data=content)


class Xml(BaseMessageComponent):
    def __init__(self, content: str = ""):
        super().__init__("xml", data=content)


class Node(BaseMessageComponent):
    """合并转发消息节点占位。"""

    def __init__(self, name: str = "", uin: str = "", content: Any = None):
        super().__init__("node", name=name, uin=uin, content=content)


class Nodes(BaseMessageComponent):
    """合并转发消息节点列表占位。"""

    def __init__(self, nodes: list = None):
        super().__init__("nodes", nodes=nodes or [])


class AtAll(BaseMessageComponent):
    """@全体成员 占位。"""

    def __init__(self):
        super().__init__("at_all")


# ---------------------------------------------------------------------------
# 消息事件包装
# ---------------------------------------------------------------------------

# 最近一次 dispatch 使用的发送回调（由 loader 设置）
_send_callback: Optional[Callable] = None


@dataclass
class SenderInfo:
    user_id: int = 0
    nickname: str = ""
    card: str = ""
    role: str = "member"


@dataclass
class MessageObject:
    """兼容 event.message_obj。"""
    message: list = field(default_factory=list)
    raw_message: dict = field(default_factory=dict)
    message_id: int = 0
    message_str: str = ""

    def get_plain_text(self) -> str:
        return self.message_str


@dataclass
class AstrMessageEvent:
    raw: dict = field(default_factory=dict)
    user_id: int = 0
    group_id: Optional[int] = None
    message: MessageChain = field(default_factory=MessageChain)
    message_id: int = 0
    sender_name: str = ""
    message_str: str = ""
    unified_msg_origin: str = ""
    sender_info: Optional[SenderInfo] = None
    message_obj: Optional[MessageObject] = None

    @classmethod
    def from_onebot(cls, event: dict) -> "AstrMessageEvent":
        raw = event.get("raw", event)
        sender = raw.get("sender", {})
        msg_data = raw.get("message", [])
        if isinstance(msg_data, str):
            msg_data = [Comp.text(msg_data)]
        msg_str = raw.get("raw_message", "")
        if not msg_str and isinstance(msg_data, list):
            msg_str = "".join(
                seg.get("data", {}).get("text", "") if seg.get("type") == "text" else ""
                for seg in msg_data
            )
        sender_info = SenderInfo(
            user_id=sender.get("user_id", 0),
            nickname=sender.get("nickname", ""),
            card=sender.get("card", ""),
            role=sender.get("role", "member"),
        )
        return cls(
            raw=raw,
            user_id=raw.get("user_id", 0),
            group_id=raw.get("group_id"),
            message=MessageChain(msg_data),
            message_id=raw.get("message_id", 0),
            sender_name=sender.get("nickname", ""),
            message_str=msg_str,
            unified_msg_origin=raw.get("unified_msg_origin", ""),
            sender_info=sender_info,
            message_obj=MessageObject(
                message=msg_data,
                raw_message=raw,
                message_id=raw.get("message_id", 0),
                message_str=msg_str,
            ),
        )

    def get_platform_name(self) -> str:
        return "qq"

    def get_message_type(self) -> str:
        return self.raw.get("message_type", "private")

    def is_private(self) -> bool:
        return self.get_message_type() == "private"

    def is_group(self) -> bool:
        return self.get_message_type() == "group"

    def get_sender_id(self) -> str:
        return str(self.user_id)

    def get_group_id(self) -> Optional[int]:
        return self.group_id

    def plain_result(self, text: str) -> dict:
        return {"type": "text", "text": text}

    def image_result(self, file: str) -> dict:
        return {"type": "image", "file": file}

    def chain_result(self, chain: MessageChain) -> dict:
        return {"type": "chain", "chain": chain.chain}

    def _send(self, result: Any) -> None:
        """将插件结果发送出去。"""
        if _send_callback is None:
            return
        detail_type = self.get_message_type()
        target_id = self.group_id if detail_type == "group" else self.user_id
        _send_callback(detail_type, target_id, result)

    def send(self, result: Any) -> None:
        """AstrBot 风格：主动发送。"""
        self._send(result)

    def send_result(self, result: Any) -> None:
        """AstrBot 风格：插件返回 result 后主动发送。"""
        self._send(result)

    def set_result(self, result: Any) -> "AstrMessageEvent":
        """AstrBot 风格：链式设置返回结果并发送。"""
        self._send(result)
        return self

    def stop_event(self) -> None:
        """占位：停止事件传播。"""
        pass


class AiocqhttpMessageEvent(AstrMessageEvent):
    """aiocqhttp 平台事件占位。"""
    pass


# ---------------------------------------------------------------------------
# AstrBotConfig 配置对象
# ---------------------------------------------------------------------------

class AstrBotConfig:
    """兼容 AstrBot 的 config，支持 dict 风格访问和保存。"""
    def __init__(self, data: Optional[dict] = None, path: Optional[str] = None):
        self._data = dict(data or {})
        self._path = path

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def pop(self, key: str, default: Any = None) -> Any:
        return self._data.pop(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self._data.setdefault(key, default)

    def update(self, *args, **kwargs):
        self._data.update(*args, **kwargs)

    def save_config(self) -> None:
        """占位：保存配置。如提供 path 则写入。"""
        if self._path:
            try:
                Path(self._path).write_text(str(self._data), encoding="utf-8")
            except Exception:
                logger.exception("保存 AstrBotConfig 失败")


# ---------------------------------------------------------------------------
# Context 注入
# ---------------------------------------------------------------------------

class Context:
    def __init__(self, llm_client=None, channel=None, config=None, provider=None,
                 core=None, plugin_id: str = "compat"):
        self.llm_client = llm_client
        self.channel = channel
        self.config = config
        self.provider = provider
        self.core = core
        self.plugin_id = plugin_id or "compat"
        self._llm_tools: dict[str, str] = {}
        self._registered_tool_names: list[str] = []
        self._active_llm_tools: set[str] = set()
        self._web_apis: dict[str, dict[str, Any]] = {}

    def get_llm(self):
        return self.llm_client

    def get_channel(self):
        return self.channel

    def get_config(self):
        return self.config

    def get_using_provider(self):
        return self.provider

    def add_llm_tools(self, *tools) -> None:
        """把 AstrBot 函数工具桥接到 Anima ToolRegistry。

        工具名使用 ``compat_<plugin>_<name>`` 命名空间，避免第三方插件覆盖
        Anima 内置工具；插件卸载时会精确移除自己注册的工具。
        """
        if len(tools) == 1 and isinstance(tools[0], (list, tuple, set)):
            tools = tuple(tools[0])
        registry = getattr(self.core, "tools", None)
        for func in tools:
            candidates = func.values() if isinstance(func, dict) else (func,)
            for candidate in candidates:
                if not callable(candidate):
                    logger.warning("AstrBot 插件 %s 忽略不可调用的 LLM 工具", self.plugin_id)
                    continue
                metadata = dict(getattr(candidate, "_anima_llm_tool", {}) or {})
                public_name = str(metadata.get("name") or getattr(candidate, "__name__", "tool"))
                full_name = self._compat_tool_name(public_name)
                self._llm_tools[public_name] = full_name
                if registry is None:
                    continue
                try:
                    from anima.tools import LEVEL_USER, Tool
                    parameters, required = _build_tool_parameters(candidate)
                    description = str(metadata.get("description") or inspect.getdoc(candidate) or public_name)
                    min_level = _compat_permission_level(metadata.get("min_level", LEVEL_USER))
                    registry.register(Tool(
                        full_name, description, parameters,
                        self._wrap_llm_tool(candidate), min_level=min_level,
                        required=required, danger=bool(metadata.get("danger", False)),
                        enabled=True,
                    ))
                    if full_name not in self._registered_tool_names:
                        self._registered_tool_names.append(full_name)
                    logger.info("AstrBot 插件工具已接入: %s -> %s", public_name, full_name)
                except Exception:
                    logger.exception("AstrBot 插件 %s 注册 LLM 工具失败: %s", self.plugin_id, public_name)

    def activate_llm_tool(self, *tool_names) -> None:
        """显式启用插件工具；兼容层默认启用，调用此方法可用于热启用。"""
        registry = getattr(self.core, "tools", None)
        for name in tool_names:
            full_name = self._llm_tools.get(str(name), str(name))
            self._active_llm_tools.add(full_name)
            if registry is not None and full_name in registry.tools:
                registry.tools[full_name].enabled = True
        logger.debug("activate_llm_tool 被调用: %s", tool_names)

    def _compat_tool_name(self, name: str) -> str:
        def slug(value: str) -> str:
            value = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_")
            return value or "plugin"
        return f"compat_{slug(self.plugin_id)}_{slug(name)}"

    def _wrap_llm_tool(self, func: Callable) -> Callable:
        signature = inspect.signature(func)
        context_params = {p.name for p in signature.parameters.values()
                          if p.name in {"context", "ctx"}}

        async def invoke(**kwargs: Any) -> Any:
            call_kwargs = dict(kwargs)
            for param in context_params:
                call_kwargs.setdefault(param, self)
            result = func(**call_kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

        return invoke

    async def llm_generate(self, *args, **kwargs) -> str:
        """兼容 AstrBot 的 llm_generate 调用。

        当兼容层绑定了真实 LLM 客户端（core.llm）时，转发到该客户端；
        否则返回空串并记录警告（兼容层未接入 LLM 的降级行为）。
        """
        if self.llm_client is None:
            logger.warning("Context.llm_generate: 未绑定 LLM 客户端，返回空串")
            return ""
        # AstrBot 签名：llm_generate(prompt, ...)
        prompt = args[0] if args else kwargs.get("prompt", "")
        try:
            try:
                from anima.pipeline.stages import load_persona_template
                system_prompt = load_persona_template()
            except Exception:
                system_prompt = "你叫哈基米，是一只住在 QQ 群里的猫娘。"
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": str(prompt)},
            ]
            with llm_scope("compat", "llm_generate"):
                return await self.llm_client.chat(messages)
        except Exception:
            logger.exception("Context.llm_generate 调用 LLM 失败")
            return ""

    async def llm_chat(self, *args, **kwargs) -> str:
        """兼容 AstrBot 的 llm_chat 调用（转发到绑定客户端）。"""
        if self.llm_client is None:
            logger.warning("Context.llm_chat: 未绑定 LLM 客户端，返回空串")
            return ""
        # AstrBot 签名：llm_chat(messages, ...)
        messages = args[0] if args else kwargs.get("messages", [])
        try:
            with llm_scope("compat", "llm_chat"):
                return await self.llm_client.chat(messages)
        except Exception:
            logger.exception("Context.llm_chat 调用 LLM 失败")
            return ""

    def register_web_api(self, path: str, handler, methods=None, description: str = "") -> None:
        """注册插件 Web API 元数据与处理器，供 WebUI/API 网关统一托管。"""
        normalized = "/" + str(path or "").lstrip("/")
        if ".." in normalized.split("/"):
            raise ValueError("插件 Web API 路径不允许目录穿越")
        allowed_methods = methods or ["GET"]
        if isinstance(allowed_methods, str):
            allowed_methods = [allowed_methods]
        method_set = {str(method).upper() for method in allowed_methods}
        if not method_set.issubset({"GET", "POST", "PUT", "PATCH", "DELETE"}):
            raise ValueError("插件 Web API 包含不支持的 HTTP 方法")
        entry = {
            "path": normalized,
            "methods": sorted(method_set),
            "description": description or normalized,
            "plugin_id": self.plugin_id,
            "handler": handler,
            "context": self,
        }
        self._web_apis[normalized] = entry
        if self.core is not None:
            registry = getattr(self.core, "compat_web_apis", None)
            if registry is None:
                registry = []
                setattr(self.core, "compat_web_apis", registry)
            registry[:] = [item for item in registry
                           if not (item.get("context") is self and item.get("path") == normalized)]
            registry.append(entry)
        logger.info("AstrBot 插件 Web API 已注册: %s %s", ",".join(sorted(method_set)), normalized)

    def web_api_manifest(self) -> list[dict[str, Any]]:
        """返回不含处理器对象的可观测清单。"""
        return [{key: value for key, value in item.items() if key not in {"handler", "context"}}
                for item in self._web_apis.values()]

    def close(self) -> None:
        registry = getattr(self.core, "tools", None)
        if registry is not None:
            for name in self._registered_tool_names:
                registry.unregister(name)
        if self.core is not None and hasattr(self.core, "compat_web_apis"):
            self.core.compat_web_apis[:] = [item for item in self.core.compat_web_apis
                                            if item.get("context") is not self]
        self._registered_tool_names.clear()
        self._web_apis.clear()


def _compat_permission_level(value: Any) -> int:
    from anima.tools import LEVEL_ADMIN, LEVEL_OWNER, LEVEL_READONLY, LEVEL_USER
    if isinstance(value, str):
        return {"readonly": LEVEL_READONLY, "user": LEVEL_USER,
                "admin": LEVEL_ADMIN, "owner": LEVEL_OWNER}.get(value.lower(), LEVEL_USER)
    try:
        return max(LEVEL_READONLY, min(LEVEL_OWNER, int(value)))
    except (TypeError, ValueError):
        return LEVEL_USER


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation in (inspect.Parameter.empty, Any, object):
        return {"type": "string"}
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        choices = [item for item in get_args(annotation) if item is not type(None)]
        return _annotation_schema(choices[0] if choices else str)
    if origin in (list, set, tuple):
        item = get_args(annotation)
        return {"type": "array", "items": _annotation_schema(item[0] if item else str)}
    if origin is dict:
        return {"type": "object"}
    return {"type": {str: "string", int: "integer", float: "number",
                      bool: "boolean", list: "array", dict: "object"}.get(annotation, "string")}


def _build_tool_parameters(func: Callable) -> tuple[dict[str, dict[str, Any]], list[str]]:
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = getattr(func, "__annotations__", {}) or {}
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for name, parameter in inspect.signature(func).parameters.items():
        if name in {"self", "context", "ctx"}:
            continue
        schema = _annotation_schema(hints.get(name, parameter.annotation))
        if parameter.default is not inspect.Parameter.empty:
            schema["default"] = parameter.default
        else:
            required.append(name)
        properties[name] = schema
    return properties, required


# ---------------------------------------------------------------------------
# StarTools
# ---------------------------------------------------------------------------

class StarTools:
    @staticmethod
    def get_data_dir(name: str = "anima", plugin_name: str = "", **kwargs) -> str:
        """返回插件数据目录。兼容 AstrBot 的 plugin_name 参数。"""
        key = plugin_name or name
        base = Path.home() / ".anima" / "astrbot_compat_data" / key
        base.mkdir(parents=True, exist_ok=True)
        return str(base)


# ---------------------------------------------------------------------------
# 占位类 / 函数
# ---------------------------------------------------------------------------

class ProviderRequest:
    """LLM 请求占位。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class LLMResponse:
    """LLM 响应占位。"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FunctionToolManager:
    """函数工具管理器占位。"""
    pass


class SessionServiceManager:
    """会话服务管理器占位。"""
    pass


class TextPart:
    """核心文本组件占位。"""
    def __init__(self, text: str = ""):
        self.text = text


class AssistantMessageSegment:
    """助手消息段占位。"""
    def __init__(self, text: str = ""):
        self.text = text


class UserMessageSegment:
    """用户消息段占位。"""
    def __init__(self, text: str = ""):
        self.text = text


class ImageURLPart:
    """图片 URL 组件占位。"""
    def __init__(self, url: str = ""):
        self.url = url


class HtmlRenderer:
    """HTML 渲染器占位。"""
    async def render_custom_template(self, tmpl_str: str, tmpl_data: dict, return_url: bool = False) -> str:
        return ""


def GreedyStr(value: str = "") -> str:
    """贪婪字符串参数占位。"""
    return str(value)


def get_astrbot_temp_path() -> str:
    p = Path.home() / ".anima" / "astrbot_temp"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def get_astrbot_data_path() -> str:
    p = Path.home() / ".anima" / "astrbot_data"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


# ---------------------------------------------------------------------------
# Star 基类与装饰器
# ---------------------------------------------------------------------------

class Star:
    """AstrBot 插件基类。"""
    def __init__(self, context: Context):
        self.context = context

    def on_unload(self) -> None:
        pass

    def create_background_task(self, coro):
        """AstrBot 风格：创建后台任务。"""
        return asyncio.create_task(coro)


# 注册装饰器

def register(*args, **meta):
    """类注册装饰器。"""
    def decorator(cls):
        cls._anima_plugin_meta = {"args": args, **meta}
        return cls
    return decorator


# 存储所有插件注册项：[(star_cls, filter_info, handler_name, priority), ...]
_REGISTRY: list[dict] = []


def _register_filter(filter_info: dict, priority: int = 0):
    def decorator(func_or_cls):
        if isinstance(func_or_cls, type):
            cls = func_or_cls
            _REGISTRY.append({
                "type": "class",
                "cls": cls,
                "filter": filter_info,
                "priority": priority,
            })
            return cls
        func = func_or_cls
        if not hasattr(func, "_anima_filters"):
            func._anima_filters = []
        func._anima_filters.append({"filter": filter_info, "priority": priority})
        return func
    return decorator


class EventMessageType:
    ALL = "ALL"
    PRIVATE_MESSAGE = "PRIVATE_MESSAGE"
    GROUP_MESSAGE = "GROUP_MESSAGE"


class PermissionType:
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class PlatformAdapterType:
    """平台适配器类型占位。"""
    AIOCQHTTP = "AIOCQHTTP"
    ALL = "ALL"


class _CommandGroup:
    """命令组：支持 @group.command('sub') 子命令注册。"""
    def __init__(self, name: str, priority: int = 0):
        self.name = name
        self.priority = priority

    def __call__(self, func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        # 注册组入口：以组名开头的命令
        wrapper._anima_filters = [{"filter": {"type": "command", "cmd": self.name}, "priority": self.priority}]
        wrapper._anima_command_group = self.name
        wrapper.command = self.command
        return wrapper

    def command(self, sub: str, priority: int = 0, **kwargs):
        cmd = f"{self.name}{sub}"
        return _register_filter({"type": "command", "cmd": cmd}, priority)


class _Filter:
    # 部分插件通过 filter.EventMessageType 访问，暴露别名
    EventMessageType = EventMessageType
    PermissionType = PermissionType
    PlatformAdapterType = PlatformAdapterType

    # 部分插件通过 filter.on_llm_request 使用钩子
    @staticmethod
    def on_llm_request(func: Callable | None = None) -> Callable | Any:
        def decorator(f: Callable) -> Callable:
            if not hasattr(f, "_anima_hooks"):
                f._anima_hooks = []
            f._anima_hooks.append("on_llm_request")
            return f
        if func is None:
            return decorator
        return decorator(func)

    @staticmethod
    def on_llm_response(func: Callable | None = None) -> Callable | Any:
        def decorator(f: Callable) -> Callable:
            if not hasattr(f, "_anima_hooks"):
                f._anima_hooks = []
            f._anima_hooks.append("on_llm_response")
            return f
        if func is None:
            return decorator
        return decorator(func)

    @staticmethod
    def on_decorating_result(func: Callable | None = None, priority: int = 0) -> Callable | Any:
        def decorator(f: Callable) -> Callable:
            if not hasattr(f, "_anima_hooks"):
                f._anima_hooks = []
            f._anima_hooks.append("on_decorating_result")
            return f
        if func is None:
            return decorator
        if callable(func):
            return decorator(func)
        return decorator

    @staticmethod
    def llm_tool(name: str | None = None, **kwargs):
        def decorator(func: Callable) -> Callable:
            if not hasattr(func, "_anima_llm_tool"):
                func._anima_llm_tool = {}
            func._anima_llm_tool.update(kwargs)
            func._anima_llm_tool["name"] = name if isinstance(name, str) and name else func.__name__
            return func
        if callable(name):
            return decorator(name)
        return decorator

    @staticmethod
    def on_group_message(priority: int = 0, **kwargs):
        return _register_filter({"event": "group_message"}, priority)

    @staticmethod
    def on_private_message(priority: int = 0, **kwargs):
        return _register_filter({"event": "private_message"}, priority)

    @staticmethod
    def on_all_message(priority: int = 0, **kwargs):
        return _register_filter({"event": "all_message"}, priority)

    @staticmethod
    def on_command(cmd: str, priority: int = 0, **kwargs):
        return _register_filter({"type": "command", "cmd": cmd}, priority)

    @staticmethod
    def command(cmd: str, priority: int = 0, **kwargs):
        return _register_filter({"type": "command", "cmd": cmd}, priority)

    @staticmethod
    def regex(pattern: str, priority: int = 0, **kwargs):
        return _register_filter({"type": "regex", "pattern": pattern}, priority)

    @staticmethod
    def event_message_type(event_type: Any, priority: int = 0, **kwargs):
        mapping = {
            EventMessageType.ALL: "all_message",
            EventMessageType.PRIVATE_MESSAGE: "private_message",
            EventMessageType.GROUP_MESSAGE: "group_message",
        }
        ev = mapping.get(event_type, "all_message")
        return _register_filter({"event": ev}, priority)

    @staticmethod
    def on_using_llm_tool(func: Callable | None = None) -> Callable | Any:
        def decorator(f: Callable) -> Callable:
            if not hasattr(f, "_anima_hooks"):
                f._anima_hooks = []
            f._anima_hooks.append("on_using_llm_tool")
            return f
        if func is None:
            return decorator
        return decorator(func)

    @staticmethod
    def on_llm_tool_respond(func: Callable | None = None) -> Callable | Any:
        def decorator(f: Callable) -> Callable:
            if not hasattr(f, "_anima_hooks"):
                f._anima_hooks = []
            f._anima_hooks.append("on_llm_tool_respond")
            return f
        if func is None:
            return decorator
        return decorator(func)

    @staticmethod
    def command_group(name: str, priority: int = 0, **kwargs):
        """命令组装饰器：支持 @group.command('sub') 子命令。"""
        return _CommandGroup(name, priority)

    @staticmethod
    def platform_adapter_type(adapter_type: Any, priority: int = 0, **kwargs):
        """占位：平台适配器类型过滤。默认放行。"""
        return _register_filter({"platform_adapter": adapter_type}, priority)

    @staticmethod
    def permission_type(perm: Any, priority: int = 0, **kwargs):
        return _register_filter({"permission": perm}, priority)


class _Hooks:
    @staticmethod
    def on_llm_request(func: Callable) -> Callable:
        if not hasattr(func, "_anima_hooks"):
            func._anima_hooks = []
        func._anima_hooks.append("on_llm_request")
        return func

    @staticmethod
    def on_llm_response(func: Callable) -> Callable:
        if not hasattr(func, "_anima_hooks"):
            func._anima_hooks = []
        func._anima_hooks.append("on_llm_response")
        return func

    @staticmethod
    def on_message(func: Callable) -> Callable:
        if not hasattr(func, "_anima_hooks"):
            func._anima_hooks = []
        func._anima_hooks.append("on_message")
        return func


filter = _Filter()
hook = _Hooks()

# 顶层函数别名，兼容 astrbot.api.event.filter 的直接导入
command = filter.command
command_group = filter.command_group
regex = filter.regex
on_command = filter.on_command
event_message_type = filter.event_message_type
permission_type = filter.permission_type
platform_adapter_type = filter.platform_adapter_type
on_group_message = filter.on_group_message
on_private_message = filter.on_private_message
on_all_message = filter.on_all_message
on_llm_request = filter.on_llm_request
on_llm_response = filter.on_llm_response
on_llm_tool_respond = filter.on_llm_tool_respond
on_using_llm_tool = filter.on_using_llm_tool
on_decorating_result = filter.on_decorating_result
llm_tool = filter.llm_tool


def llm_tool(name: str | None = None, **kwargs):
    """标记一个可由 Context.add_llm_tools 注册的函数工具。"""
    def decorator(func: Callable):
        func._anima_llm_tool = {
            "name": name if isinstance(name, str) and name else getattr(func, "__name__", "tool"),
            **kwargs,
        }
        return func
    if callable(name):
        return decorator(name)
    return decorator


# ---------------------------------------------------------------------------
# 适配层内部集成
# ---------------------------------------------------------------------------

def _match_filter(filter_info: dict, event: AstrMessageEvent, text: str) -> bool:
    """判断 filter 是否匹配事件。"""
    event_type = event.get_message_type()
    ftype = filter_info.get("type")

    if "event" in filter_info:
        ev = filter_info["event"]
        if ev == "all_message":
            return True
        if ev == "group_message" and event_type == "group":
            return True
        if ev == "private_message" and event_type == "private":
            return True
        return False

    if "permission" in filter_info:
        # 权限检查：兼容层默认放行，真实权限由 Anima 权限阶段控制
        return True

    if ftype == "command":
        return text.strip().startswith(filter_info["cmd"])
    if ftype == "regex":
        return re.search(filter_info["pattern"], text) is not None

    return False


async def _dispatch_plugin_event(core, event: dict) -> Optional[Any]:
    """将内部事件分发给已加载的 AstrBot 插件。

    如果插件返回结果，会立即发送并返回结果摘要；否则返回 None。
    """
    from anima.compat.loader import _loaded_plugins
    from anima.core.gateway import OutboundMsg

    global _send_callback

    def send_callback(detail_type: str, target_id: int, result: Any) -> None:
        if core is None or core.gateway is None:
            return
        message = ""
        extra = {}
        if isinstance(result, dict):
            if result.get("type") == "text":
                message = result.get("text", "")
            elif result.get("type") == "chain":
                message = result.get("chain", [])
            elif result.get("type") == "image":
                message = [{"type": "image", "data": {"file": result.get("file", "")}}]
            elif result.get("type") == "file":
                extra["file_path"] = result.get("file")
                extra["name"] = result.get("name")
                message = result.get("caption", f"文件: {result.get('name', '')}")
                asyncio.create_task(core.gateway.send_file(OutboundMsg(detail_type, target_id, message, extra=extra)))
                return
            elif result.get("type") == "MessageEventResult":
                if result.get("chain"):
                    message = result.get("chain")
                else:
                    message = result.get("message", "")
        elif isinstance(result, MessageEventResult):
            if result.chain:
                message = result.chain if isinstance(result.chain, list) else result.chain.chain
            else:
                message = result.message_text
        elif isinstance(result, MessageChain):
            message = result.chain
        elif isinstance(result, str):
            message = result

        asyncio.create_task(core.gateway.send(OutboundMsg(detail_type, target_id, message)))

    _send_callback = send_callback

    compat_event = AstrMessageEvent.from_onebot(event)
    text = compat_event.message.get_plain_text()
    results = []

    for plugin in _loaded_plugins:
        for method_name, filter_info, priority in getattr(plugin, "_anima_handlers", []):
            if not _match_filter(filter_info, compat_event, text):
                continue
            method = getattr(plugin, method_name)
            try:
                result = await method(compat_event)
                if result is not None:
                    results.append((priority, result))
            except Exception:
                logger.exception("插件 %s.%s 执行异常", plugin.__class__.__name__, method_name)

    _send_callback = None

    if not results:
        return None
    results.sort(key=lambda x: -x[0])
    return results[0][1]


def _build_llm_hook_stage(core):
    """将插件的 on_llm_request 钩子构建为 Pipeline 阶段。

    AstrBot 插件 hook 原型为 on_llm_request(self, req)（req 为 AstrBot
    LLMRequest）。原生 Anima 运行时无此对象，钩子调用失败（签名不兼容等）
    即永久跳过该插件并记一次告警，避免每条消息刷全量 traceback
    （见日志可观测性维度，P1-7）。
    """
    from anima.compat.loader import _loaded_plugins

    _skipped: set = set()  # (plugin_id, method_name) 已确认不兼容、永久跳过

    async def hook_stage(ctx: Any) -> Any:
        if not _loaded_plugins:
            return None
        for plugin in _loaded_plugins:
            pid = getattr(plugin, "name", None) or plugin.__class__.__name__
            hooks = getattr(plugin, "_anima_hooks", {})
            for method_name, hs in hooks.items():
                if "on_llm_request" not in hs or (pid, method_name) in _skipped:
                    continue
                method = getattr(plugin, method_name, None)
                if method is None:
                    continue
                try:
                    await method(ctx)
                except TypeError:
                    # 签名不兼容（如期望 AstrBot LLMRequest 而非 ctx）——永久跳过
                    _skipped.add((pid, method_name))
                    logger.warning("插件 %s 的 %s 签名不兼容，已跳过（不再重试）",
                                   pid, method_name)
                except Exception:
                    logger.exception("插件 LLM hook 异常（单次）")
        return None

    return hook_stage
