"""能力工具工厂（P1 自生长能力系统）。"""

from __future__ import annotations

import logging
import re
import time

from anima.tools import Tool, LEVEL_USER

logger = logging.getLogger("anima.tools.capability")


def _safe_capability_tool_name(cap: dict) -> str:
    """把能力名归一化为合法的 LLM 工具名（my_<alnum>）。

    优先用能力 id（形如 cap_1718000000，稳定唯一）；纯中文名退化为 hash 兜底。
    """
    base = (cap.get("id") or cap.get("name") or "cap")
    sanitized = re.sub(r'[^a-z0-9_]+', '_', str(base).lower()).strip('_')
    if not sanitized:
        sanitized = f"cap_{abs(hash(base)) % 10**8:08d}"
    return ("my_" + sanitized)[:48]


def make_capability_tool(cap: dict, core) -> "Tool":
    """把一个能力包装成可被 LLM 调用的 Tool。

    执行时复用 CapabilityManager.execute_capability（LLM 提示词方法，无代码 eval）。
    在 Tool 上挂 _capability_name 回指，供生成阶段做使用反馈映射。
    """
    safe_name = _safe_capability_tool_name(cap)
    raw = cap.get("parameters_schema") or {}
    default_params = {"query_or_args": {"type": "string", "description": "任务描述"}}
    # 能力存储的是「完整 JSON Schema」({type:object, properties, required})，
    # 而 Tool 约定 parameters 只收 properties 片段。若为完整 schema 则解包，
    # 否则（旧格式直接是 properties 片段 / 缺省 / 畸形 schema）当 properties 用或退化默认
    # —— 避免再次包裹成嵌套 type:object 被 Gemini function_declarations 校验拒绝（400）。
    if isinstance(raw, dict) and raw.get("type") == "object" and isinstance(raw.get("properties"), dict):
        params = raw["properties"] or default_params
        required = raw.get("required")
    elif isinstance(raw, dict) and raw and "type" not in raw:
        # 旧格式：直接是 properties 片段（如 {"foo": {"type": "string"}}）
        params = raw
        required = None
    else:
        # 畸形 / 缺省 → 退化单参数
        params = default_params
        required = None
    min_level = cap.get("min_level", LEVEL_USER)
    # 调用冷却：同一能力 5 分钟内不重复调用，避免 LLM 每条消息都调同一个 cap
    _cap_last_call: dict[str, float] = {}

    async def _handler(query_or_args: str = "") -> str:
        import time as _time
        cap_id = str(cap.get("id") or safe_name)
        now = _time.time()
        last = _cap_last_call.get(cap_id, 0)
        if now - last < 300:
            return "（这个能力刚刚用过了，先歇歇）"
        _cap_last_call[cap_id] = now
        llm = getattr(core, "llm", None)
        if llm is None:
            return "内部错误：LLM 未就绪"
        from anima.cognitive.capabilities import get_capability_manager
        mgr = get_capability_manager(core.config.persistence.data_dir)
        return await mgr.execute_capability(llm, cap, query_or_args)

    tool = Tool(
        name=safe_name,
        description=(cap.get("description") or "角色自己创造的个人能力")[:200],
        parameters=params,
        required=required,
        handler=_handler,
        min_level=min_level,
    )
    tool._capability_name = cap.get("name")  # 回指，供反馈映射
    return tool


def register_capability_as_tool(core, cap: dict, mgr: "CapabilityManager") -> bool:
    """把能力注册为 LLM 工具（即时对 LLM 可见）。

    闸门（始终生效）：
    - register_as_independent_tool 必须为 True（该能力是否愿意被独立调用）
    - 每日配额 dynamic_daily_quota（超配额仅入库不注册）
    - 同名跳过（避免重复注册）

    Args:
        core: AnimaCore（提供 .tools / .llm / .config）
        cap: 能力字典
        mgr: CapabilityManager（提供每日配额状态）
    Returns:
        是否真实新注册（同名跳过/超配额返回 False）
    """
    if not cap.get("register_as_independent_tool", False):
        return False
    if core.tools is None:
        return False

    # 每日配额
    today = time.strftime("%Y-%m-%d")
    if mgr._daily_register.get("date") != today:
        mgr._daily_register = {"date": today, "count": 0}
    if mgr._daily_register["count"] >= mgr.dynamic_daily_quota:
        logger.info("今日能力工具注册配额已满 (%d)，能力「%s」仅入库不注册",
                    mgr.dynamic_daily_quota, cap.get("name", ""))
        return False

    tool = make_capability_tool(cap, core)
    if tool.name in core.tools.tools:
        return False  # 同名跳过

    core.tools.register(tool)
    mgr._daily_register["count"] += 1
    logger.info("动态注册能力工具: %s（今日 %d/%d）",
                tool.name, mgr._daily_register["count"], mgr.dynamic_daily_quota)
    return True
