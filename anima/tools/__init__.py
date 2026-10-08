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
import threading
import json
import re
import time
import logging
from typing import Any, Callable, Optional

from anima.capability.index import CapabilityIndex
from anima.capability.scopes import PermissionScopes, exec_permitted

logger = logging.getLogger("anima.tools")
_CALL_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("anima_tool_context", default={})
_TOOL_FAILURE_PREFIXES = (
    "工具执行错误", "错误:", "[沙箱]", "联网搜索失败", "天气查询失败",
    "查询失败", "暂时不可用", "工具暂时不可用",
    # X 搜索失败前缀：不标记会误判成功，导致 LLM 以为搜到结果没图、
    # 擅自降级生图替代（X 风控时尤其关键）。
    "X 搜索失败", "X 搜索未完成", "X 搜索模块不可用", "X 浏览器未启用",
)
_TOOL_INTERNAL_MARKERS = (
    "[stderr]", "Traceback", "Operation not permitted", "Permission denied", "unshare failed",
)
_SAFE_TOOL_FAILURE = "工具暂时不可用，未获得可验证结果。"

# --- P0（2026-09-18）：失败结果的「原因保留」通道 ---------------------------------
# 问题（生产实测）：x_search 额度用尽时 `x_browser.search()` 毫秒级返回
# `note="今日搜索额度已用尽"`（不启动浏览器 → 零日志），但该串命中上面
# `_TOOL_FAILURE_PREFIXES` 里的 "X 搜索未完成" → 被 normalize 整句折叠成
# `_SAFE_TOOL_FAILURE`。模型因此**分不清**「额度用完，次日自动恢复」与
# 「接口真挂了（故障）」，只能对用户说「推特检索接口挂了调不通」——错误归因。
#
# 机制：工具用 `tool_unavailable(<原因码>)` 构造失败结果，形态固定为
# `工具暂时不可用（原因：<原因码>）`；normalize 见到**白名单内的原因码**原样放行。
#
# 语义零变更（刻意如此）：
#   * 该串仍以 "工具暂时不可用" 开头 → `tool_result_ok()` 仍为 False
#     → 审计 / 遥测 / `reply_quality.tool_failures` 口径逐字节不变；
#   * governance `OutcomeNormalizer._FAILURE_PREFIXES` 同样含该前缀 → 仍归 FAILURE。
# 即：**只增加「模型可见文本的信息量」，不改变任何成功/失败判定**。
#
# 安全边界：白名单只收「行为性、可对外」的原因码；**异常文本 / 堆栈 / 内部标识
# 永不入内**；非白名单原因一律退化为通用串（fail-closed）。
_TOOL_UNAVAILABLE_PREFIX = "工具暂时不可用（原因："
_TOOL_UNAVAILABLE_SUFFIX = "）"

TOOL_REASON_X_DISABLED = "X 浏览器未启用"
TOOL_REASON_X_QUOTA_SEARCH = "X 搜索今日额度已用尽，次日 00:00 自动恢复"
TOOL_REASON_X_QUOTA_TIMELINE = "X 时间线今日额度已用尽，次日 00:00 自动恢复"
TOOL_REASON_X_RISK_COOLDOWN = "X 触发风控冷却，稍后自动恢复"
TOOL_REASON_X_EMPTY_RENDER = "X 页面未渲染出内容，稍后重试"
TOOL_REASON_X_NOT_LOGGED_IN = "X 登录态失效，需重新登录"

_TOOL_REASON_WHITELIST = frozenset({
    TOOL_REASON_X_DISABLED,
    TOOL_REASON_X_QUOTA_SEARCH,
    TOOL_REASON_X_QUOTA_TIMELINE,
    TOOL_REASON_X_RISK_COOLDOWN,
    TOOL_REASON_X_EMPTY_RENDER,
    TOOL_REASON_X_NOT_LOGGED_IN,
})


def tool_unavailable(reason: str) -> str:
    """构造「可保留原因」的失败结果（P0，2026-09-18）。

    ``reason`` 必须取自 ``TOOL_REASON_*`` 白名单；否则（含任何异常文本）退化为
    ``_SAFE_TOOL_FAILURE``——原因只留在工具自己的日志里，不进模型上下文。
    """
    reason = str(reason or "").strip()
    if reason not in _TOOL_REASON_WHITELIST:
        return _SAFE_TOOL_FAILURE
    return _TOOL_UNAVAILABLE_PREFIX + reason + _TOOL_UNAVAILABLE_SUFFIX


# --- 09-18 fail-open 审计：失败判定「完备化」----------------------------------------
# 起因：`_TOOL_FAILURE_PREFIXES` 是**前缀允许清单**，而工具失败文本是自由散文
# （各 handler 自己拼的中文句子）。09-18 对**全部注册点**做 AST 审计（解析每个
# ``Tool(...)`` 的 handler → 扫其 return 字面量 → 与 ``tool_result_ok()`` 对表），
# 实测 **47 处「正在报告失败却被判成功」**，横跨 9 个文件（其中 45 处可解析到具体
# 工具名，共 26 个工具；另 2 处注册点的 handler 是动态表达式）。复现：
# ``.workbuddy/tmp/audit_full.py`` → 对表 ``tool_result_ok()`` → FAIL-OPEN 计数。
#
# 这不是文案问题 —— ``ok`` 是真实控制流：
#   * ``anima/agent/react.py:223`` → ``_FRESHNESS_TOOLS`` 门控 + 工具轨迹；
#   * ``anima/agent/executor.py:412`` → 判定一个 ``AgentTask`` 步骤**是否成功**。
#     被沙箱护栏**拒绝**的 ``agent_*`` 步骤此前被当作「成功结果」返回（安全相关）。
#
# 红线：``_TOOL_FAILURE_PREFIXES`` **逐字节未动**。
#   F8.11 的 R1 把该元组冻结给「治理 slice」——那是防止治理为了自家分类去改动
#   模型面契约。本次改动的起因是**工具层自身缺陷**（不是治理需要），故不改原元组，
#   新增独立元组，使两处改动各自可追溯、可回滚。
#
# 另一条不可让步的边界（P0 教训）：**判定修正不得顺带夺走模型看到的失败原因**。
# 这些串此前以「成功」身份**原样**进入模型上下文；若修判定时一并折叠成
# ``_SAFE_TOOL_FAILURE``，模型会从「知道为什么」退化为「只知道失败了」——反向回归。
# 故 ``normalize_tool_result()`` 对本文件的失败形态**原样放行**（见该函数）。
_TOOL_FAILURE_PREFIXES_EXT = (
    # 全角冒号：原表只收了 ASCII "错误:"，而 6 处 return 用全角 "错误："。
    "错误：",
    # 同一形态的前缀变体（capability.py:64 动态能力工具的 handler）
    "内部错误：",
    # 以下逐条对应一个真实可达的 return（file:line 为审计锚点）
    "计算出错",              # system.py:37    calculate
    "搜索时出错了",           # web.py:412      web_search（治理侧注册表已收，工具侧漏收）
    "生图失败",              # owner.py:64     generate_image
    "改图失败",              # owner.py:93     edit_image
    "整理失败",              # merge.py:47     merge_forward
    "查询关系失败",           # social.py:235   get_user_relations
    "创建 SkillDraft 失败",   # owner.py:23     propose_skill_draft
    "列目录失败",             # repo.py:60      repo_list
    "读取失败",              # repo.py:72      repo_read
    "搜索失败",              # repo.py:81      repo_grep
    "[self_code] 执行异常",   # owner.py:48     self_code
    "B 站视频理解功能未启用",   # bilibili.py:285 bilibili_read
    "读取 B 站视频失败",       # bilibili.py:418,475
    "下载字幕失败",           # bilibili.py:448
    "运行状态暂时不可用",      # system.py:110   get_runtime_status
    "群信息查询不可用",        # social.py:297   get_group_info
    "群成员查询不可用",        # social.py:134   get_group_members
    "戳一戳功能不可用",        # social.py:36    group_poke
    "禁言功能不可用",          # social.py:334   group_ban
    "踢人功能不可用",          # social.py:352   group_kick
    "修改名片功能不可用",      # social.py:318   set_group_card
    "表情检索暂不可用",        # sticker.py:101  sticker 检索
    "图片数据无法解析",        # sticker.py:151  sticker 偷取
    "网页探查失败",           # skills/registry.py:154
    "当前未配置 LLM",         # skills/registry.py:168
    "技能不可用",             # skills/registry.py:214
    "群荣誉榜查询不可用",      # honor.py:65     group_honor
    "群荣誉榜查询出错",        # honor.py:80
    "没能从 OneBot 取到",     # honor.py:83（后续为动态群号）
    # 🔴 r28（2026-10-06）：ReAct 单工具墙钟超时的结果串（react.py:609）。
    # 它此前**不在任何失败表里** ⇒ `tool_result_ok` 判 True ⇒ 两个真实后果：
    #   ① `tool_trace.ok=True` ⇒ `reply_quality.tool_failures` 少计全部超时，
    #      工具健康度虚高（"x_search 一直很好"的错觉就是这么来的）；
    #   ② r28 的「工具故障播报拦截」以 `ok=False` 为必要前提 ⇒ 判据在生产恒假。
    # 走 EXT 表（判定失败 + **保留文本**）：这句话是给模型的中文提示，有信息量，
    # 折叠掉会让模型从「知道为什么」退化成「只知道失败了」。
    "工具执行超时",
)

# 「必须折叠」前缀（2026-09-18，债 D-18）—— 第三类语义，别与上面两张表混用：
#   `_TOOL_FAILURE_PREFIXES`（冻结）：判定失败 + 折叠（除白名单原因码）
#   `_TOOL_FAILURE_PREFIXES_EXT`     ：判定失败 + **保留文本**（作者手写、对模型有信息量）
#   `_TOOL_COLLAPSE_PREFIXES`（本表）：判定失败 + **折叠** —— 因为文本本身是**异常信息**
#     （异常类型名 / errno / 调用栈片段），不该进模型上下文。
#
# 台账此前把 `X 搜索异常` 定性为「属产品判断、等 owner 决定」，**双重错误**：
#   (a) 它产自 `x_browser` 的 `except Exception`（:521 search / :569 timeline），是客观
#       异常信号，不是措辞偏好；
#   (b) 它不能进 EXT 表 —— EXT 语义是"保留文本"，放进去会**判定失败但仍把异常文本送进
#       上下文**，比修复前更糟。`_TOOL_INTERNAL_MARKERS` 也兜不住它（只认 `[stderr]` /
#       `Traceback` / `Permission denied` 等固定词，不认 `TypeError` 这类裸异常名）。
# 冻结表逐字节未动（F8.11 R1）：本条走独立新表，不改旧表。
_TOOL_COLLAPSE_PREFIXES = (
    "X 搜索异常",            # x_browser.py:521  search() except 分支的 note
    "X 时间线异常",          # x_browser.py:569  home_timeline() except 分支
)

# 「失败族」：整族默认失败，族内**成功形态**显式列出 —— 这是 **fail-closed**
# 写法：族内以后新增的失败串会被自动捕获，无需再逐条补清单。
#   `[agent] ` = agent_tools.py 的 6 个 OWNER 沙箱工具；实测 21 条 `[agent] ` 返回
#   里**只有** `已写入` 是成功（其余为参数缺失 / 路径拒绝 / 护栏拒绝 / 超时 / 执行失败）。
#   族内若新增成功形态（如 `已删除`）会被判失败 —— 这是刻意的保守方向：宁可少记一次
#   成功，也不放过一次「被拒绝却当成成功」。
_TOOL_FAILURE_FAMILIES: "tuple[tuple[str, tuple[str, ...]], ...]" = (
    ("[agent] ", ("[agent] 已写入",)),
)

# 固定前缀表达不了的动态失败形态（各只有一个可达点，逐条注明）
# 刻意**不**用裸 `失败` 作词尾匹配：那会把 web_search / agent_http 返回的**网页正文**
# 中恰好以「失败」结尾的片段误判为失败。只收这两个精确词尾。
_TOOL_FAILURE_SUFFIXES = ("信息失败", "列表失败")   # social.py:304 / social.py:164
_TOOL_FAILURE_INFIXES = ("接口暂时不可用",)         # bilibili.py:233 部分结果 + 明确降级指示


def _has_internal_marker(text: str) -> bool:
    """文本含内部实现细节（堆栈 / 沙箱 errno 等）→ 永不入模型上下文。"""
    lower = text.lower()
    return any(m.lower() in lower for m in _TOOL_INTERNAL_MARKERS)


def _matches_legacy_failure(text: str) -> bool:
    """命中**冻结**的旧前缀表（命中即沿用 P0-1 的折叠语义）。"""
    lower = text.lower()
    return any(lower.startswith(p.lower()) for p in _TOOL_FAILURE_PREFIXES)


def _matches_collapse_failure(text: str) -> bool:
    """命中「必须折叠」前缀：异常信息，判定失败且文本不进上下文（D-18）。"""
    lower = text.lower()
    return any(lower.startswith(p.lower()) for p in _TOOL_COLLAPSE_PREFIXES)


def _matches_extended_failure(text: str) -> bool:
    """命中 09-18 补齐的失败形态（前缀族 / 族 / 后缀 / 中缀）。"""
    lower = text.lower()
    if any(lower.startswith(p.lower()) for p in _TOOL_FAILURE_PREFIXES_EXT):
        return True
    for family, ok_forms in _TOOL_FAILURE_FAMILIES:
        if text.startswith(family) and not any(text.startswith(ok) for ok in ok_forms):
            return True
    if any(text.endswith(s) for s in _TOOL_FAILURE_SUFFIXES):
        return True
    return any(m in text for m in _TOOL_FAILURE_INFIXES)


# F8.9.1：受治理 Gateway pilot 集合（精确豁免，非泛化机制）。
# 仅此清单内的 capability 在 ``capability_gateway_mode=ON`` 时走 Line B Gateway；
# 其余（含其余 mixed-mode capability）仍走 Line A。扩展新成员须显式追加并走独立 slice，
# 禁止改为「全部 capability 默认走 Gateway」或自动迁移 mixed-mode。
# R5 PHASE 13 re-open (owner-authorized 2026-08-15)：追加 python.exec 到受治理 pilot 清单。
# 该 provider 非 capability_only（无 capability_only 属性），故不经 pilot allowlist 门，
# 直达 _governed_capability_route；其 fail-closed 四门（D1 kill_switch / D2 grant /
# D3 audit_sink / D4 isolator）在 gateway.handle 内执行。仅当 config 同时置
# capability_mode + capability_gateway_mode + python_exec_enabled 为真才被注册并可达。
_GATEWAY_PILOT_CAPABILITIES = frozenset({"code.execute", "filesystem.read", "filesystem.write", "search.web", "python.exec"})

# F9.2 Operational Activation：三条正交治理证据链的落盘文件名（固定，非配置项——
# 目录可配、文件名不可配，避免运维把三条链写进同一个文件而丢失链间边界）。
GOVERNANCE_AUDIT_FILENAME = "resolution_events.jsonl"       # 为什么选（Resolution）
GOVERNANCE_HEALTH_FILENAME = "health_evidence.jsonl"        # 执行后发生了什么（Health）
GOVERNANCE_TRANSITION_FILENAME = "lifecycle_transitions.jsonl"  # 状态迁移（Recovery）


def _run_in_thread(coro_factory: "Callable[[], Any]") -> "Any":
    """在独立线程 + 新 event loop 中运行协程，规避「事件循环已在运行」的嵌套错误。

    F8.6 R2：Line B ``Provider.invoke`` / ``Resolver.resolve`` 是 **sync** 契约
    （F5.4b I2/I3，冻结不可改），而 ``run_python`` 是 **async**。治理路径复用
    ``run_python`` 必须经此桥接；桥接体只位于 ``anima.tools`` adapter 层——
    governance 包保持纯 sync、零改动、加载期零 runtime import 边。

    R3（context fidelity）：worker 线程默认看不到调用方 contextvars，而
    ``_invoke`` 对 run_python 会**无条件用可信 ctx 覆盖** ``permission_level``
    （M0.4.3-R1 反特权注入），审计等级同样取自 ctx。若不传播，受治理路径的
    sandbox tier 与 execution 审计会被静默降级为 USER，与 legacy 语义分叉。
    故此处 ``copy_context()`` 后用 ``ctx.run(loop.run_until_complete, ...)``：
    Task 创建发生在 ctx 内，协程全程看到与主线程逐字节一致的调用上下文。

    🔴 **本函数会阻塞调用线程**（``t.join()``）。它只允许在**工作线程**里被调用；
    禁止从事件循环线程直接调用（会把 loop 卡住整段工具耗时，2026-09-23 事故）。
    受治理路由的调用点已改为 ``await asyncio.to_thread(handle, ...)``。
    """
    ctx = contextvars.copy_context()
    box: dict[str, Any] = {}
    exc: list[BaseException] = []

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = ctx.run(loop.run_until_complete, coro_factory())
        except BaseException as e:  # noqa: BLE001 - 原样上抛给主线程
            exc.append(e)
        finally:
            loop.close()

    t = threading.Thread(target=_worker, name="anima-governed-bridge", daemon=True)
    t.start()
    t.join()
    if exc:
        raise exc[0]
    return box.get("value")


# M0.4.3 S4：代码执行审计。
# 这些工具会在宿主上运行任意 payload，必须留下可追责的结构化轨迹：
# 谁（actor UID）以什么权限（permission_level / sandbox_tier）执行了什么
# （payload 摘要）、耗时多久、成功与否。
EXECUTION_TOOLS = frozenset({"run_python", "run_shell", "agent_exec"})
_EXEC_PAYLOAD_KEYS = ("code", "command", "script")
_SANDBOX_TIER_NAMES = {0: "banned", 1: "readonly", 2: "user", 3: "admin", 4: "owner"}


def _exec_payload(args: dict[str, Any]) -> str:
    for key in _EXEC_PAYLOAD_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


#: 普通工具 ``tool_executed`` 审计里结果原文的截断上限（超出部分只在标记里报总长）。
AUDIT_RESULT_PREVIEW_CHARS = 500
#: 认作「本次调用的输入」的入参名，按优先级取第一个非空字符串。
_AUDIT_PRIMARY_ARG_KEYS = ("query", "request", "q", "text", "keyword", "topic", "question")


def _audit_request(args: dict[str, Any]) -> str:
    """从调用入参里取「这次要做什么」——优先 query / request 等主参数。

    只取字符串入参，取不到就是空字符串：**不猜、不补**。执行类工具不走这里
    （它们已有 ``payload_preview``，且入参是可执行代码，不该再复制一份）。
    """
    for key in _AUDIT_PRIMARY_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    values = [value.strip() for value in args.values() if isinstance(value, str) and value.strip()]
    return values[0] if values else ""


def _audit_result_preview(text: str) -> str:
    """结果原文预览：超过上限则截断并**带截断标记**（绝不让截断后的文本冒充全文）。"""
    body = text or ""
    if len(body) <= AUDIT_RESULT_PREVIEW_CHARS:
        return body
    return body[:AUDIT_RESULT_PREVIEW_CHARS] + f"…[已截断，共 {len(body)} 字]"


def get_call_context() -> dict[str, Any]:
    return _CALL_CONTEXT.get()


def tool_result_ok(result: object) -> bool:
    """工具结果是否算「成功」= 非空 ∧ 不含内部细节 ∧ 未命中任何失败注册表。

    失败注册表共五层（历史顺序，语义互不重叠）：
      1. ``_TOOL_FAILURE_PREFIXES``          —— 冻结的旧前缀（F8.11 R1，逐字节不动）
      2. ``_TOOL_COLLAPSE_PREFIXES``         —— 异常信息前缀（判定失败 + 折叠，D-18）
      3. ``_TOOL_FAILURE_PREFIXES_EXT``      —— 09-18 审计补齐的前缀
      4. ``_TOOL_FAILURE_FAMILIES``          —— 族前缀 + 族内成功例外（fail-closed）
      5. ``_TOOL_FAILURE_SUFFIXES/_INFIXES`` —— 固定前缀表达不了的动态形态
    """
    text = str(result or "").strip()
    if not text:
        return False
    if (_has_internal_marker(text) or _matches_legacy_failure(text)
            or _matches_collapse_failure(text)):
        return False
    return not _matches_extended_failure(text)


def normalize_tool_result(result: object) -> tuple[str, bool]:
    """统一工具出站契约：内部细节留日志，模型只得到安全、**有信息量**的状态。

    分支顺序即优先级：
      1. 含内部细节（堆栈 / errno / unshare 等）→ 折叠为 ``_SAFE_TOOL_FAILURE``；
      2. 命中**冻结**的旧前缀：P0-1 白名单原因码原样放行，其余折叠
         —— 与改造前**逐字节一致**（改造不改旧语义）；
      3. 命中「必须折叠」的异常前缀（D-18）→ 折叠（异常文本不入上下文）；
      4. 命中 09-18 补齐的失败形态 → ``ok=False``，但**文本原样返回**。

    第 4 条的理由：这些是作者手写、可对模型的中文提示，此前以「成功」身份原样进入
    上下文。若修判定时顺带折叠，模型会从「知道为什么」退化为「只知道失败了」——
    等于用修 bug 换来一次信息量回归。故**只修判定，不动文本**。
    第 3 条正相反：那里的文本是异常信息，保留它就是泄漏 —— 差别在**文本性质**，
    不在严重度。
    """
    text = str(result or "").strip()
    if not text:
        return _SAFE_TOOL_FAILURE, False
    if _has_internal_marker(text):
        return _SAFE_TOOL_FAILURE, False
    if _matches_legacy_failure(text):
        kept = _kept_reason(text)
        return (kept, False) if kept is not None else (_SAFE_TOOL_FAILURE, False)
    if _matches_collapse_failure(text):
        return _SAFE_TOOL_FAILURE, False
    if _matches_extended_failure(text):
        return text, False
    return text, True


def _kept_reason(text: str) -> Optional[str]:
    """白名单原因透传；非白名单返回 ``None``（调用方折叠为通用失败串）。"""
    if not (text.startswith(_TOOL_UNAVAILABLE_PREFIX)
            and text.endswith(_TOOL_UNAVAILABLE_SUFFIX)):
        return None
    reason = text[len(_TOOL_UNAVAILABLE_PREFIX):-len(_TOOL_UNAVAILABLE_SUFFIX)]
    return text if reason in _TOOL_REASON_WHITELIST else None


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
                 granted_scopes: Any = None,
                 capability_only: bool = False,
                 priority: int = 5,
                 private_only: bool = False,
                 template: str = ""):
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
        # --- P0-1 Memory-First：schema 展示序（越小越靠前）---
        # 只影响 get_schemas() 的返回顺序，不改变调用语义。默认 5 => 未显式标注的
        # 工具按注册顺序（Python sort 稳定）保持原样，向后兼容。
        self.priority = int(priority)
        # --- M0.3 (RFC-0001) Capability 元数据：additive，默认空，不改变任何既有语义 ---
        # name 仍是唯一执行标识；capability_ids 只是「这个实现满足哪些能力」的声明。
        self.capability_ids: list[str] = [str(c) for c in (capability_ids or []) if str(c)]
        # Provider 被授予的权限域（RFC-0001 §7）；None → 空集（不授予任何 scope）。
        self.granted_scopes = PermissionScopes.from_mapping(granted_scopes)
        # M0.4.7 D-A0（F0 closure）：逐工具治理开关。True 表示本工具**只能**经 capability
        # 路由调用（call_capability 内部传 _via_capability=True），禁止经 legacy 直调，
        # 防止绕过 capability allowlist / 路由控制。默认 False 保持既有 mixed-mode 行为
        # （web_search / read_file / write_file 即此模式——仍 legacy 暴露 + capability 路由并存）。
        self.capability_only = bool(capability_only)
        # --- P1-3：私聊专属（「受控环境」类工具不进群聊）---
        # True 时该工具在群聊场景既不可见也不可调用（即便调用者是 Owner——owner 在
        # 群内同样拿到 permission_level=4，仅靠 min_level 挡不住）。判定依据是
        # call context 的 detail_type/group_id，不是 permission_level。
        self.private_only = bool(private_only)
        # --- 审计可回溯：本工具「当时生效的模板文本」---
        # 普通内置工具没有模板 → 空字符串（字段仍在，供事后恢复轨迹；不省略、不猜）。
        # 声明式 Skill 由 SkillRegistry 在 _make_tool 时填入 execution.template。
        self.template = template or ""

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


def _normalize_principals(entries) -> tuple[str, ...]:
    """allowlist 归一：去空白、丢空项、小写、去重（保序）。

    刻意**不支持任何通配符**（``*`` / ``all`` 等）——在 fail-closed 语义下，
    通配符等于一个静默后门（M0.4.7 D-A1 R2）。
    """
    if not entries:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        item = str(raw).strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out)


class ToolRegistry:
    """工具注册中心。"""

    def __init__(self, core=None, capability_mode: bool = False,
                 pilot_capability_allowlist=None,
                 capability_gateway_mode: bool = False,
                 governance_audit_sink=None,
                 health_evidence_sink=None,
                 health_observer=None,
                 transition_store=None,
                 governance_state=None):
        self.tools: dict[str, Tool] = {}
        self.core = core
        # R6.2：治理态（能力撤销 / 全局熔断）。为 None 时回退到进程单例，
        # 由 governance 包统一管理；测试可注入独立实例避免串扰。
        self.governance_state = governance_state
        # --- M0.3 (RFC-0001) Capability Adapter ---
        # 索引是 Registry 之上的查询视图；Registry 仍是唯一 execution owner。
        self.capability_index = CapabilityIndex()
        # feature flag：默认关闭 → get_schemas 行为 100% 不变。
        self.capability_mode = bool(capability_mode)
        # M0.4.7 D-A1：pilot allowlist（fail-closed）。默认空 = 拒绝全部治理能力路由。
        # 注入由 D-A2 负责（本阶段构造参数默认值保持现状，CLI / replay 行为不变）。
        self._pilot_allowlist: tuple[str, ...] = _normalize_principals(pilot_capability_allowlist)
        self._capability_resolver = None
        # F8.6：受治理 pilot 路径（默认 OFF，零开销）。gateway_mode 是 capability_mode
        # 的**子闸门**，仅作用于 code.execute；governance_audit_sink 默认 None（R4：
        # 无 sink 时治理解析零开销，不分配 trace、不改执行路径）。
        self.capability_gateway_mode = bool(capability_gateway_mode)
        self._governance_audit_sink = governance_audit_sink
        self._capability_gateway = None
        # F8.10.1：health evidence 发射（C1 默认关）。无 sink → 零开销（不建 evidence、
        # 不影响 latency / 返回值）；有 sink 时 route 经 EvidenceAdapter 投影，且
        # evidence 永不作 execution blocker（fail-open）。adapter 惰性构造（避免默认
        # 路径引入 anima.capability.health import 成本）。
        self._health_evidence_sink = health_evidence_sink
        self._health_evidence_adapter = None
        # F9.2：recovery 链的接线缝（默认 None = 休眠）。registry **不驱动**它——
        # 正常执行路径永远不调用 observer；它只是让 recovery 通道在被显式授权激活时
        # 有一个已接好的落点（本 slice 不实现任何自动 quarantine / recover 策略）。
        self.health_observer = health_observer
        self.transition_store = transition_store

    # ---------------- M0.4.7 D-A1：capability 路由治理 ----------------

    @property
    def pilot_capability_allowlist(self) -> tuple[str, ...]:
        """治理放行主体清单（已归一）。空 = 拒绝全部（fail-closed）。"""
        return self._pilot_allowlist

    @pilot_capability_allowlist.setter
    def pilot_capability_allowlist(self, value) -> None:
        self._pilot_allowlist = _normalize_principals(value)

    def _pilot_principal(self, caller: Optional[str] = None) -> Optional[str]:
        """本次调用的主体标识：显式 ``caller`` 优先，否则取可信 call context 的 user_id。

        取不到主体 → 返回 None → 治理判定按 fail-closed 拒绝。
        """
        if caller:
            explicit = str(caller).strip().lower()
            if explicit:
                return explicit
        ctx = get_call_context() or {}
        uid = ctx.get("user_id")
        if uid is None:
            return None
        uid = str(uid).strip().lower()
        return f"user:{uid}" if uid else None

    def _owner_ids(self) -> list:
        """权威 owner QQ 列表（来自 config.permissions.owners）。"""
        if not self.core:
            return []
        cfg = getattr(self.core, "config", None)
        if cfg is None:
            return []
        owners = getattr(getattr(cfg, "permissions", None), "owners", None)
        if not owners:
            owners = getattr(cfg, "owners", None)
        return list(owners or [])

    @property
    def _python_exec_enabled(self) -> bool:
        """python.exec 是否在配置中显式启用（fail-closed：默认 False）。"""
        if not self.core:
            return False
        cfg = getattr(self.core, "config", None)
        if cfg is None:
            return False
        layer = getattr(cfg, "capability_layer", None)
        if layer is None:
            return False
        return bool(getattr(layer, "python_exec_enabled", False))

    def _is_owner_principal(self, principal: Optional[str]) -> bool:
        """owner 主体判定：principal 形如 ``user:{qq}`` 且命中 config.permissions.owners。

        owner 是 python.exec 的授权权威来源（独立于 pilot allowlist）。
        格式非法 / 类型错误 / 非 user: 前缀 / 无法解析 QQ → 一律非 owner（fail-closed）。
        """
        if not principal or not isinstance(principal, str):
            return False
        if not principal.startswith("user:"):
            return False
        try:
            qid = int(principal.split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        return qid in self._owner_ids()

    def pilot_capability_permitted(self, caller: Optional[str] = None) -> bool:
        """治理 allowlist 判定（fail-closed）。

        R6.1 owner-only 策略：owner 恒放行；其余主体须命中 allowlist，空 allowlist
        拒绝全部（含非 owner）。该判定是**路由级软门控**；真正不可绕过的硬边界在
        ``PythonExecProvider.invoke`` 内（基于可信 call context 的 owner 检查）。
        """
        principal = self._pilot_principal(caller)
        if self._is_owner_principal(principal):
            return True
        if not self._pilot_allowlist:
            return False
        if principal is None:
            return False
        return principal in self._pilot_allowlist

    def _capability_is_governed(self, capability_id: str) -> bool:
        """能力是否属治理集：任一 Provider 为 capability-only（D-A0 语义）即为治理能力。

        治理能力 = 已退出 legacy 表面、只走 capability 路由的能力（当前仅
        ``code.execute``）。mixed-mode 能力（web_search / read_file / write_file）
        不在治理集内，行为不受 allowlist 影响。
        """
        return any(getattr(p.tool, "capability_only", False)
                   for p in self.capability_index.providers_for(capability_id))

    def _governed_capability_ids(self) -> set[str]:
        return {cap_id for cap_id in self.capability_index.capabilities()
                if self._capability_is_governed(cap_id)}

    def _pilot_denial_reason(self, caller: Optional[str] = None) -> str:
        if not self._pilot_allowlist:
            return "allowlist_empty"
        if self._pilot_principal(caller) is None:
            return "no_principal"
        return "allowlist_mismatch"

    def _deny_capability_route(self, capability_id: str, reason: str) -> str:
        self._audit("capability_route_denied", capability=capability_id, reason=reason)
        # M0.4.7 D-A4（telemetry）：拒绝事件同时计入 capability 路由可观测指标。
        # 仅发指标、不改执行语义；进入进程级 MetricsCollector（WebUI /api/metrics 可见）。
        from anima.core.metrics import get_metrics
        get_metrics().counter("capability.route.denied",
                               tags={"capability": capability_id, "reason": reason})
        return f"错误: capability '{capability_id}' 未获治理放行（{reason}）"

    @property
    def capability_resolver(self):
        """惰性构造的 Resolver（只消费索引，不执行工具）。"""
        if self._capability_resolver is None:
            from anima.capability.resolver import CapabilityResolver
            self._capability_resolver = CapabilityResolver(self.capability_index)
        return self._capability_resolver

    # ---------------- F8.6：Governed Pilot Path (Capability Gateway) ----------------
    @property
    def capability_gateway(self):
        """惰性构造的受治理 Gateway（仅 ``code.execute`` 已 bootstrap）。

        仅当 ``capability_gateway_mode`` 开启且请求命中 code.execute 时才构造，
        否则永不实例化——默认 OFF 路径零成本（R4）。
        """
        if self._capability_gateway is None:
            self._capability_gateway = self._build_capability_gateway()
        return self._capability_gateway

    def _build_capability_gateway(self):
        from anima.capability.governance.gateway import (
            CapabilityGateway,
            bootstrap_code_execute,
            bootstrap_python_exec,
        )
        from anima.runtime.capability_resolver import CapabilityRegistry
        # F8.9.1：filesystem.read 的 bootstrap 位于 tools 层（anima.tools.capability_fs），
        # 不改动 governance/gateway.py（Q3 裁定）。F8.9.2：filesystem.write 同构追加。
        # F8.9.3：search.web 位于 anima.tools.capability_web（C1/C2 约束见该文件）。
        from anima.tools.capability_fs import (
            bootstrap_filesystem_read,
            bootstrap_filesystem_write,
        )
        from anima.tools.capability_web import bootstrap_search_web

        registry = CapabilityRegistry()
        bootstrap_code_execute(registry, executor=self._make_code_executor())
        bootstrap_filesystem_read(registry, executor=self._make_filesystem_executor())
        bootstrap_filesystem_write(registry, executor=self._make_filesystem_write_executor())
        bootstrap_search_web(registry, executor=self._make_search_web_executor())
        # R5 PHASE 13：python.exec 受治理 provider（**代码默认** OFF / fail-closed）。
        # 仅当 capability_layer.python_exec_enabled=True 且本网关已构建（capability_gateway_mode=True）
        # 时才注册。
        # ⚠️ 2026-09-18 实测订正：旧注释称「生产两者皆 False → 本 bootstrap 不执行」，
        #    **与生产实配不符**：生产两项皆 true ⇒ 本分支**确实执行**，provider 已注册。
        #    另注意：本 provider 非 capability_only，故**不经 pilot allowlist 门**
        #    （见上方 :194-195），空 allowlist 不能阻止它。
        _pyexec_cfg = getattr(self.core.config, "capability_layer", None) if self.core else None
        _pyexec_on = bool(getattr(_pyexec_cfg, "python_exec_enabled", False)) if _pyexec_cfg else False
        if _pyexec_on:
            from anima.runtime.providers.python_exec import PythonExecPolicy
            from anima.capability.governance.state import get_governance_state
            _pyexec_policy = PythonExecPolicy(
                kill_switch=False,
                grant=True,
                require_audit=bool(getattr(_pyexec_cfg, "python_exec_require_audit", True)),
                audit_sink_ok=self._governance_audit_sink is not None,
                permission_level=int(getattr(_pyexec_cfg, "python_exec_permission_level", 2)),
                governance_state=get_governance_state(),
            )
            bootstrap_python_exec(
                registry,
                executor=self._make_python_exec_executor(),
                policy=_pyexec_policy,
                owner_ids=self._owner_ids(),
            )
        return CapabilityGateway(registry, audit_sink=self._governance_audit_sink)

    def _make_code_executor(self):
        """构造 code.execute 的 **sync** executor（F8.6 R2 桥接体）。

        桥接体只位于本 adapter 层：把 async ``run_python`` 经独立线程新 event loop
        跑完（``_run_in_thread``，携带完整 contextvars）。委托姿势与 legacy
        ``code_exec_handler`` 逐字节一致（R3）——只信可信 ctx 的 permission_level，
        绝不读调用方 args；真正的 exec gate / sandbox tier / 执行审计仍由
        ``_invoke`` 在 run_python 上行使，治理不是新的执行权限来源。
        """
        registry_self = self

        def executor(code: str, execution_key: tuple) -> str:
            ctx = get_call_context() or {}
            trusted_level = int(ctx.get("permission_level", LEVEL_USER))

            async def _run():
                return await registry_self.call(
                    "run_python", {"code": code}, permission_level=trusted_level
                )

            return _run_in_thread(_run)

        return executor

    def _make_python_exec_executor(self):
        """构造 python.exec 的 **sync** executor（R5 PHASE 13，fail-closed 桥接体）。

        与 ``_make_code_executor`` 同构：把 async ``run_python`` 经独立线程新 event loop
        跑完（``_run_in_thread``，携带完整 contextvars）。委托姿势逐字节一致——只信可信
        ctx 的 permission_level，绝不读调用方 args；真正的 exec gate / sandbox tier /
        OS 隔离仍由 ``run_python``（sandbox）行使，治理不是新的执行权限来源。
        provider 的 invoke 以 ``(code, permission_level)`` 调用本 executor。
        """
        def executor(code: str, level: int) -> str:
            from anima.tools import sandbox

            async def _run():
                return await sandbox.run_python(code, permission_level=level)

            return _run_in_thread(_run)

        return executor

    def _make_filesystem_executor(self):
        """构造 filesystem.read 的 executor（F8.9.1，第二条治理路径）。

        与 ``_make_code_executor`` 同构：read_file 经 ``self.call``（async）执行，桥接体
        只位于本 adapter 层（``_run_in_thread``，携带完整 contextvars）。委托姿势与 legacy
        ``read_file`` 调用逐字节一致——只信可信 ctx 的 permission_level，绝不读调用方 args；
        真正的文件读取 / 沙箱边界 / 路径穿越拦截仍由 ``_invoke`` 在 ``read_file`` 上行使，
        治理不是新的执行权限来源。

        「sync provider」的差异价值：filesystem.read 的 handler 本身是 **sync**
        （无 async I/O、无沙箱 event loop），桥接体内部 ``self.call`` 跑的是纯同步
        handler，不存在 code.execute 那种「嵌套 event loop 跑 run_python」的复杂度——
        这正是 F8.9.1 要证明的「Gateway 不是为某一种特殊（async）provider 定制」。
        ``ToolRegistry.call`` 的 async 是框架不变量，桥接仅用于把它从治理的 sync
        ``resolve`` 上下文里安全唤起。
        """
        registry_self = self

        def executor(filename: str, execution_key: tuple) -> str:
            ctx = get_call_context() or {}
            trusted_level = int(ctx.get("permission_level", LEVEL_USER))

            async def _run():
                return await registry_self.call(
                    "read_file", {"filename": filename}, permission_level=trusted_level
                )

            return _run_in_thread(_run)

        return executor

    def _make_filesystem_write_executor(self):
        """构造 filesystem.write 的 executor（F8.9.2，第三条治理路径）。

        与 ``_make_filesystem_executor`` 同构（F8.9.1）：write_file 经 ``self.call``
        （async）执行，桥接体只位于本 adapter 层（``_run_in_thread``，携带完整
        contextvars）。委托姿势与 legacy ``write_file`` 调用逐字节一致——只信可信 ctx 的
        permission_level，绝不读调用方 args（C2：permission 链单向，不创造第二套
        authorization）；真正的写盘 / 沙箱边界 / 目录穿越拦截仍由 ``_invoke`` 在
        ``write_file`` 上行使，治理不是新的执行权限来源。

        C1 约束（与 provider metadata pinning 呼应）：本 executor 不声明任何元语义——
        ``side_effect=True / idempotent=False`` 的诚实声明只存在于
        ``bootstrap_filesystem_write`` 的治理契约与 provider 上；executor 只负责把调用
        原样委托给 ``write_file``，不改变 handler 行为（Q3/Q4：legacy write == governed
        write，handler 不升级 filesystem API）。
        """
        registry_self = self

        def executor(filename: str, content: str, execution_key: tuple) -> str:
            ctx = get_call_context() or {}
            trusted_level = int(ctx.get("permission_level", LEVEL_USER))

            async def _run():
                return await registry_self.call(
                    "write_file",
                    {"filename": filename, "content": content},
                    permission_level=trusted_level,
                )

            return _run_in_thread(_run)

        return executor

    def _make_search_web_executor(self):
        """构造 search.web 的 executor（F8.9.3，第四条治理路径）。

        C2 async bridge：``tool_web_search`` 是 **async** handler（``asyncio.to_thread``
        内部网络），与 ``_make_code_executor`` 同构——经 ``_run_in_thread`` 桥接
        ``self.call``（携带完整 contextvars）。**不 async 化** resolver / provider
        contract / gateway（治理层保持 sync，桥接体只位于本 adapter）。

        委托姿势与 legacy ``web_search`` 调用逐字节一致——只信可信 ctx 的
        permission_level，绝不读调用方 args（不创造第二套 authorization）；真正的网络
        搜索 / 超时（handler 内硬编码 12s/15s）/ 失败吞异常语义仍由 ``_invoke`` 在
        ``tool_web_search`` 上行使，治理不是新的执行权限来源。

        Q2 失败 parity：handler 把网络异常吞为 ``搜索时出错了：<ExceptionType>``
        （web.py:202-204，非 _SAFE_TOOL_FAILURE）——本 executor 原样透传该字符串，
        不改 handler / exception model / SAFE 语义（F8.9.x 不变量：governed execution
        == legacy execution semantics）。
        """
        registry_self = self

        def executor(query: str, num_results: int, execution_key: tuple) -> str:
            ctx = get_call_context() or {}
            trusted_level = int(ctx.get("permission_level", LEVEL_USER))

            async def _run():
                return await registry_self.call(
                    "web_search",
                    {"query": query, "num_results": num_results},
                    permission_level=trusted_level,
                )

            return _run_in_thread(_run)

        return executor

    def _next_governed_request_id(self) -> str:
        """为每次受治理请求生成稳定 request_id（R5：adapter 拥有 correlation 责任）。"""
        import uuid

        return uuid.uuid4().hex

    async def _governed_capability_route(self, cap_id: str, args: dict, caller) -> str:
        """把请求经 Gateway 走完整治理闭环，并把 ``ProviderResult`` 翻译回 ``str``。

        委托顺序严格满足 F8.6 Security 门：principal authorization（pilot allowlist）
        已由 ``call_capability`` 在调用本方法**之前**完成（R1）；此处只做 capability
        级治理（contract/provider/selection/audit），不重新判定 who-can-invoke。

        🔴 2026-09-23：``gateway.handle`` 虽为 sync，但**必须经 ``asyncio.to_thread``
        离线程调用** —— 其执行链末端是 ``_run_in_thread`` 的 ``t.join()``，
        直接在 loop 线程上调用会让事件循环停转整段工具耗时（详见下方注释与
        ``tests/test_governed_route_event_loop.py``）。
        """
        from anima.runtime.capability_resolver import CapabilityNeed

        need = CapabilityNeed(capability_id=cap_id, args=args, context={})
        # R5：每次受治理请求恰好一个 execution_key，逐字节传入并原样记进 ResolutionEvent。
        request_id = self._next_governed_request_id()
        execution_key = (request_id, cap_id, 0)
        try:
            # 🔴 2026-09-23：**必须离线程**。`handle` 是 sync 契约（F5.4b I2/I3 冻结），
            # 内部的 provider executor 经 `_run_in_thread`（本模块上方）用 `t.join()`
            # 等实际执行 —— 直接在 loop 线程上调用 = **事件循环被整段阻塞**，
            # 时长等于工具调用全程（联网检索卡在 TCP connect 时可达数十秒，
            # 期间所有并发会话一起停转）。
            #
            # 生产证据（faulthandler 全线程栈，2026-09-21 09:21:15 `Timeout (0:01:15)!`）：
            #   桥接线程  anima/tools/__init__.py:253 in _worker（嵌套 loop）
            #   网络线程  web.py in _fetch_page → socket.create_connection（阻塞）
            #   24h 内 `事件循环延迟` 告警 8 条、峰值 23.099s，与 search_web 窗口重合。
            #
            # `asyncio.to_thread` 会**复制当前 context** 再在执行器线程里跑 ⇒
            # `_CALL_CONTEXT`（permission_level / group_id）逐字节保留：R3 context
            # fidelity 与 legacy 路径一致。`handle` 本体一个字未改，仍是 sync。
            presult = await asyncio.to_thread(
                self.capability_gateway.handle, need, execution_key
            )
        except Exception:  # noqa: BLE001 - fail-open：治理异常不阻断业务
            logger.exception("受治理 capability 路由异常，回退安全失败 caller=%s", caller)
            return _SAFE_TOOL_FAILURE

        # F8.10.1：唯一 health evidence 发射点（Q1 owner = 本 route）。
        # Q3：handle 返回后成功/失败分支各恰好 1 条；基础设施异常分支已提前返回 → 0 条
        # （fail-open：evidence 永不作 execution blocker）。sink 为 None（C1 默认关）时
        # 本调用零开销直接返回。
        self._emit_health_evidence(cap_id, execution_key, presult)

        if not getattr(presult, "success", False):
            return getattr(presult, "error", None) or _SAFE_TOOL_FAILURE
        return str(presult.data)

    def _emit_health_evidence(self, cap_id: str, execution_key: tuple, presult) -> None:
        """把 governed 执行事实投影为 HealthEvidence（F8.10.1，唯一发射点）。

        C1 默认关：无 sink → 零开销直接返回（不建 evidence、不影响 latency / 返回值）。

        F8.11（本方法唯一新增职责）：把执行表面事实归一化为 governance 语义——
        ``normalized_outcome``（SUCCESS / FAILURE / UNKNOWN）+ 可选 ``error_class``
        投影进 evidence.metadata（Q5：只记归一化结果，**不存 result_fragment** 原始
        文本）。``success=presult.success`` 仍是 transport 边界事实（Q1：ProviderResult
        .success 降级为 surface 字段，Recovery 未来只消费 normalized_outcome）。

        R2：归一化是纯投影（OutcomeNormalizer 纯函数）——绝不反向影响 presult /
        gateway return / tool output（方向只能 execution → classification → evidence）。
        R3：未知形态保留 UNKNOWN，不强行归失败。

        fail-open（双保险）：adapter.record 内部已对 sink 失败计数不抛；此处再包一层
        try/except —— evidence 发射失败**绝不阻断** governed 执行。
        """
        if self._health_evidence_sink is None:
            return
        try:
            if self._health_evidence_adapter is None:
                from anima.capability.health.adapter import EvidenceAdapter

                self._health_evidence_adapter = EvidenceAdapter(sink=self._health_evidence_sink)
            # F8.11：归一化（纯函数，零副作用）；失败不影响 record 本身。
            extra_metadata = None
            try:
                from anima.capability.health.normalizer import OutcomeNormalizer

                outcome, error_class = OutcomeNormalizer.normalize(presult)
                extra_metadata = {"normalized_outcome": outcome.value}
                if error_class:
                    extra_metadata["error_class"] = error_class
            except Exception:  # noqa: BLE001 - 归一化失败不阻断 evidence 发射（fail-open）
                logger.debug("outcome 归一化失败（fail-open）: cap=%s", cap_id)
            self._health_evidence_adapter.record(
                presult,  # ProviderResult 即鸭子类型 observation（success/error/metadata）
                capability_id=cap_id,
                provider_id=None,  # Q1：route 不可见 provider_id（Lifecycle 决策 capability 级）
                execution_key=execution_key,
                selected=True,  # 拿到 presult 即代表 selection 已发生
                extra_metadata=extra_metadata,  # F8.11：normalized_outcome / error_class
            )
        except Exception:  # noqa: BLE001 - fail-open：evidence 失败绝不阻断执行
            logger.debug("health evidence 发射失败（fail-open）: cap=%s", cap_id)

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

    # ------------------------------------------------------------------
    # P1-3：私聊专属工具（群聊不暴露「受控环境」能力）
    # ------------------------------------------------------------------
    @staticmethod
    def _is_group_context() -> bool:
        """当前调用上下文是否群聊。

        只看可信 ctx（generation 用 set_call_context 注入），绝不读工具入参。
        无上下文时（如单测/离线调用）按**非群聊**处理——可见性放宽只可能发生在
        没有真实会话的场景，不会让群聊会话拿到私聊工具。
        """
        ctx = _CALL_CONTEXT.get()
        if not isinstance(ctx, dict) or not ctx:
            return False
        if str(ctx.get("detail_type") or "") == "group":
            return True
        gid = ctx.get("group_id")
        return gid is not None and str(gid) != ""

    def _private_only_blocked(self, tool: "Tool") -> bool:
        return bool(getattr(tool, "private_only", False)) and self._is_group_context()

    def get_schemas(self, permission_level: int = LEVEL_USER,
                    capability_mode: Optional[bool] = None, *,
                    caller: Optional[str] = None,
                    sort_by_priority: bool = False) -> list[dict]:
        """根据用户权限等级过滤可使用的工具 schema。

        M0.3 双模式（RFC-0001 §5.4）：
        - ``capability_mode=False``（默认）：legacy Tool schema，行为逐字节不变；
        - ``capability_mode=True``：输出 capability schema（不含任何 Provider 名），
          未标注 capability 的 legacy Tool 仍以 Tool schema 暴露（迁移期混合）。

        参数为 None 时取实例 flag ``self.capability_mode``（默认 False）。

        M0.4.7 D-A1 R6（暴露面 gate）：``capability_mode=True`` **不等于**所有
        capability schema 自动公开。治理能力（capability-only Provider，当前仅
        ``code.execute``）还须 allowlist 放行才可见——否则能力名泄露给全体用户，
        且 LLM 见即调，产生持续 deny 噪音。判定式::

            schema visible = capability_mode ∧ provider available ∧ allowlist permitted

        mixed-mode 能力不属治理集，可见性不受 allowlist 影响（行为不变）。

        ``sort_by_priority``（P0-1）：为 True 时 legacy schema 按 ``Tool.priority``
        升序返回（记忆类 1 → web_search 3 → 未标注 5）。**默认 False**，因为返回
        顺序本身属于上述冻结契约；只有 ReAct 主路径会显式打开。
        """
        use_capability = self.capability_mode if capability_mode is None else bool(capability_mode)
        if not use_capability:
            visible = [
                t for t in self.tools.values()
                if t.enabled and permission_level >= t.min_level
                and not t.capability_only
                and not self._private_only_blocked(t)   # P1-3：群聊隐藏私聊专属工具
            ]
            if sort_by_priority:
                # P0-1 Memory-First：记忆类工具（priority 1）排在 web_search（3）之前。
                # sort 稳定 => 同 priority 保持注册顺序，未标注工具（默认 5）行为不变。
                #
                # 默认 **关闭**：legacy schema 的返回顺序是 M0.3（RFC-0001 §5.4）冻结契约
                # 「逐字节不变」的一部分，由 test_capability_* 系列守着。排序只对
                # 显式开启的主路径（ReAct）生效，不改动任何既有调用方的可见行为。
                visible.sort(key=lambda t: t.priority)
            return [t.to_openai_schema() for t in visible]
        from anima.capability.schema import capability_schemas, legacy_only_schemas
        exclude = None if self.pilot_capability_permitted(caller) else self._governed_capability_ids()
        return (
            capability_schemas(self.capability_index, permission_level, exclude=exclude)
            + legacy_only_schemas(list(self.tools.values()), permission_level)
        )

    async def call_capability(self, capability_ref: str, args: dict,
                              permission_level: int = LEVEL_USER, *,
                              caller: Optional[str] = None) -> str:
        """capability 模式的调用入口：Resolver 选 Provider，执行仍走 ``call()``。

        ``capability_ref`` 可以是 capability id（``search.web``）或其 schema name
        （``search_web``，LLM 侧看到的名字）。``call(name, args)`` 语义不变。

        M0.4.7 D-A1 两级治理门（gate 落在**唯一入口**，不落调用方，避免 F0 同型旁路）：

        1. ``capability_mode=False`` → 整条 capability 路由未激活，一律拒绝；
        2. 治理能力（capability-only Provider）→ 还须 pilot allowlist 放行
           （fail-closed：空 allowlist / 无主体 / 不匹配 → 拒绝）。

        mixed-mode 能力只受第 1 级约束，激活后行为与既有一致。
        拒绝一律经 ``capability_route_denied`` 审计，无静默拒绝。
        """
        cap_id = self.capability_index.capability_for_schema_name(capability_ref) or capability_ref

        # R6.2-02 / R6.2-01：治理态优先于路由（fail-closed 总闸）。
        # 全局 kill switch 或能力被撤销（REVOKED）→ 任何 capability 调用 DENY，
        # 不依赖 UI 隐藏，配置篡改无法绕过。
        from anima.capability.governance.state import get_governance_state
        _gs = self.governance_state if self.governance_state is not None else get_governance_state()
        if _gs is not None:
            if _gs.global_kill_switch():
                return self._deny_capability_route(cap_id, "global_kill_switch_active")
            if _gs.is_revoked(cap_id):
                return self._deny_capability_route(cap_id, "capability_revoked")

        if not self.capability_mode:
            return self._deny_capability_route(cap_id, "capability_mode_off")
        resolution = self.capability_resolver.resolve(cap_id, permission_level=permission_level)
        if (getattr(resolution.provider.tool, "capability_only", False)
                and not self.pilot_capability_permitted(caller)):
            return self._deny_capability_route(cap_id, self._pilot_denial_reason(caller))
        # F8.6 / F8.9.1：受治理 pilot 路径（仅 ``_GATEWAY_PILOT_CAPABILITIES`` 清单内
        # 的 capability 在 gateway_mode=ON 时走 Line B Gateway；其余走 Line A）。
        # principal gate（pilot allowlist）已先于此执行——R1 安全不变量：gateway 不会
        # 绕过 principal authorization。
        if self.capability_gateway_mode and cap_id in _GATEWAY_PILOT_CAPABILITIES:
            # R6.1 fail-closed：python.exec 仅在 python_exec_enabled=True 时经 gateway
            # 路由；禁用时退化为 capability_only 直调拒绝（见下方 self.call 的 capability_only 拦截），
            # 避免走到 fail-open 的 gateway 解析分支。
            if cap_id == "python.exec" and not self._python_exec_enabled:
                _governed = False
            else:
                _governed = True
            if _governed:
                return await self._governed_capability_route(cap_id, args, caller)
        # M0.4.7 D-A4（telemetry）：治理门通过即记录 allowed + routed；Provider 执行
        # 失败（tool_result_ok 判定）记 failed。仅发指标、不改执行语义；指标进入进程级
        # MetricsCollector（WebUI /api/metrics 可见），与既有 ``capability_route_denied``
        # 审计分流（审计=取责，遥测=可观测），互不污染。
        from anima.core.metrics import get_metrics
        _m = get_metrics()
        _m.counter("capability.route.allowed", tags={"capability": cap_id})
        _m.counter("capability.route.routed", tags={"capability": cap_id})
        result = await self.call(resolution.provider.tool_name, args, permission_level,
                                 _via_capability=True)
        if not tool_result_ok(result):
            _m.counter("capability.route.failed", tags={"capability": cap_id})
        return result

    def can_call(self, name: str, permission_level: int) -> bool:
        tool = self.tools.get(name)
        if tool is None:
            return False
        return tool.enabled and permission_level >= tool.min_level

    async def call(self, name: str, args: dict,
                   permission_level: int = LEVEL_USER, *,
                   _via_capability: bool = False) -> str:
        """调用工具并返回结果字符串。

        ``_via_capability`` 仅供 ``call_capability`` 内部使用：标记本次调用来自受控的
        capability 路由，可绕过 capability-only 工具的 legacy 直调守卫（D-A0）。

        M0.4.7 D-A3（R3 closure）：请求名若是一个 capability 的 schema name（且不是
        legacy 工具名），则经 ``call_capability`` 受控路由——Resolver 选 Provider、执行
        仍由本方法完成，但治理门（``capability_mode`` / allowlist）前置。集中在此唯一入口，
        react / executor / webui 等所有调用方自动受益，杜绝 F0 同型旁路。普通 legacy 工具
        名 ``capability_for_schema_name`` 返回 None，逐字节走原路径，不改变普通 tool dispatch。
        """
        tool = self.tools.get(name)
        # R3 断链修复：capability 模式的 LLM 看到的是 schema name（如 ``code_execute``），
        # 它并非 legacy 工具名；在此转发到治理路由，避免「工具不存在」的断链。
        cap_id = self.capability_index.capability_for_schema_name(name)
        if cap_id is not None and tool is None:
            return await self.call_capability(name, args, permission_level)
        if tool is None:
            return f"错误: 工具 '{name}' 不存在"

        # M0.4.7 D-A0（F0 closure）：capability-only 工具禁止经 legacy 直调，
        # 防止绕过 capability allowlist / 路由控制。仅 call_capability 内部经
        # _via_capability=True 放行；委托路径自身的 exec gate / sandbox / audit
        # 仍由 _invoke 统一守卫（capability 不是新的执行权限来源）。
        if tool.capability_only and not _via_capability:
            self._audit("execution_denied", reason="capability_only_called_via_legacy",
                        tool=name)
            return (f"错误: 工具 '{name}' 只能通过 capability 路由调用，"
                    "legacy 直调已被治理策略拒绝")

        if not self.can_call(name, permission_level):
            self._audit("tool_denied", tool=name, permission_level=permission_level)
            return f"错误: 你的权限不足，无法调用工具 '{name}'"

        # P1-3：私聊专属工具即便权限够，在群聊上下文也拒绝执行。
        # 与 schema 隐藏是两道独立门——LLM 可能凭旧上下文/幻觉点名调用，
        # 权限够也可能来自「owner 在群里」，必须在这里再挡一次。
        if self._private_only_blocked(tool):
            ctx = get_call_context()
            self._audit("tool_denied", tool=name, reason="private_only_in_group",
                        group_id=ctx.get("group_id"), permission_level=permission_level)
            return f"错误: 工具 '{name}' 仅限私聊使用，群聊不可调用"

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
        # S5：exec scope 真实生效门控。执行类工具必须持有 granted exec scope，
        # 否则即使 min_level 通过也拒绝执行（与 S4 审计共用 EXECUTION_TOOLS 判定集）。
        # 此门控与调用者权限等级正交：任何代码执行工具都须经 exec_permitted() 放行。
        if tool.name in EXECUTION_TOOLS and not exec_permitted(tool.granted_scopes):
            self._audit("execution_denied", reason="missing_exec_scope", tool=tool.name,
                        granted_scopes=tool.granted_scopes.as_dict())
            return "错误: 该工具未获授权执行代码（缺少 exec scope 授权）"

        # run_python 特殊处理：从 call context 提取入站图片注入沙箱
        if tool.name == "run_python":
            ctx = get_call_context()
            images_b64 = ctx.get("input_images_b64")
            if images_b64 and "input_images_b64" not in args:
                args = {**args, "input_images_b64": images_b64}
            # R1 (M0.4.3-R1)：沙箱分级能力仅由可信 call context 派发，
            # 严禁信任调用方传入的 permission_level —— 否则普通 UID 可在 tool-call
            # args 中注入 level=4，把沙箱升级到 owner 层（network=True +
            # fs_confined=False），构成典型特权参数注入。无条件用 ctx 值覆盖 args，
            # 丢弃任何调用方提供的 permission_level 字段。
            args = {**args, "permission_level": ctx.get("permission_level", LEVEL_USER)}
            logger.info("run_python 调用: code_len=%d has_images=%s code_preview=%.200s",
                        len(args.get("code", "")), bool(images_b64), args.get("code", ""))

        # S4：执行类工具的结构化审计（started / finished / failed）
        exec_meta = self._execution_audit_meta(tool, args)
        if exec_meta is not None:
            self._audit("execution_started", **exec_meta)
        _exec_t0 = time.perf_counter()

        try:
            result = tool.handler(**args)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            if exec_meta is not None:
                self._audit(
                    "execution_failed",
                    duration_ms=round((time.perf_counter() - _exec_t0) * 1000, 2),
                    error=type(exc).__name__,
                    **exec_meta,
                )
            raise
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
        # 普通工具：补上事后可回溯的三样（request / template / result_preview）。
        # 执行类工具跳过——它们已有 payload_preview 与 execution_finished，不重复记一份。
        replay_meta = {} if exec_meta is not None else {
            "request": _audit_request(args),
            "template": tool.template,
            "result_preview": _audit_result_preview(text),
        }
        self._audit("tool_executed", tool=tool.name, ok=ok, result_len=len(text), **replay_meta)
        if exec_meta is not None:
            self._audit(
                "execution_finished",
                duration_ms=round((time.perf_counter() - _exec_t0) * 1000, 2),
                success=ok,
                result_len=len(text),
                **exec_meta,
            )
        return text

    def _execution_audit_meta(self, tool: Tool, args: dict[str, Any]) -> Optional[dict[str, Any]]:
        """S4：构造代码执行审计的公共字段；非执行类工具返回 None。"""
        if tool.name not in EXECUTION_TOOLS:
            return None
        import hashlib

        ctx = get_call_context()
        payload = _exec_payload(args)
        # R1 (M0.4.3-R1)：审计记录的权限等级只能取自可信 ctx，
        # 绝不回退到调用方传入的 args，避免审计轨迹被伪造。
        level = ctx.get("permission_level", LEVEL_USER)
        try:
            level = int(level)
        except (TypeError, ValueError):
            level = LEVEL_USER
        return {
            "tool": tool.name,
            "actor": ctx.get("user_id"),
            "group_id": ctx.get("group_id"),
            "permission_level": level,
            "sandbox_tier": _SANDBOX_TIER_NAMES.get(min(level, 4), "user"),
            "payload_len": len(payload),
            "payload_sha256": hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16],
            "payload_preview": payload[:200],
        }

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

def _governance_evidence_dir(core, cfg_layer):
    """治理证据落盘目录 = ``persistence.data_dir`` / ``capability_layer.governance_data_dir``。

    F9.2：证据不另立数据根——沿用 bot 既有 data_dir，运维只需管一个目录树。
    ``governance_data_dir`` 给绝对路径时按原样使用（``Path.__truediv__`` 语义）。
    """
    from pathlib import Path

    persistence = getattr(getattr(core, "config", None), "persistence", None)
    data_dir = getattr(persistence, "data_dir", None) or "data"
    sub = getattr(cfg_layer, "governance_data_dir", None) or "governance"
    return Path(data_dir) / sub


def _wire_governance_sinks(core, cfg_layer, governance_audit_sink, health_evidence_sink):
    """F9.2 Operational Activation —— 按 config 开关构造三条证据链的持久化出口。

    S1 边界（本函数的全部职责）：**只构造 + 只注入**。
    绝不：改变 resolver / 参与 provider 选择 / 影响 policy decision / 驱动 lifecycle
    迁移。sink 是单向下游终点，没有任何回边指向执行路径。

    S2 默认 OFF：三个开关全关（默认）时本函数返回原样入参 + 两个 ``None``，
    既不建目录也不建文件（零足迹、零开销）。

    显式传入的 sink 优先于 config（既有调用式行为不变；replay / 测试可强制覆盖）。

    :returns: ``(governance_audit_sink, health_evidence_sink, health_observer,
        transition_store)``
    """
    health_observer = None
    transition_store = None
    if cfg_layer is None:
        return governance_audit_sink, health_evidence_sink, health_observer, transition_store

    audit_on = bool(getattr(cfg_layer, "governance_audit_enabled", False))
    health_on = bool(getattr(cfg_layer, "health_evidence_enabled", False))
    transition_on = bool(getattr(cfg_layer, "transition_audit_enabled", False))
    if not (audit_on or health_on or transition_on):
        return governance_audit_sink, health_evidence_sink, health_observer, transition_store

    gov_dir = _governance_evidence_dir(core, cfg_layer)

    if audit_on and governance_audit_sink is None:
        from anima.capability.audit.persistent_sink import PersistentAuditSink

        governance_audit_sink = PersistentAuditSink(str(gov_dir / GOVERNANCE_AUDIT_FILENAME))

    if health_on and health_evidence_sink is None:
        from anima.capability.health.store import HealthEvidenceStore

        health_evidence_sink = HealthEvidenceStore(str(gov_dir / GOVERNANCE_HEALTH_FILENAME))

    if transition_on:
        from anima.capability.governance.lifecycle import CapabilityLifecycle
        from anima.capability.ops.observer import HealthObserver
        from anima.capability.ops.transition_store import TransitionStore

        transition_store = TransitionStore(str(gov_dir / GOVERNANCE_TRANSITION_FILENAME))
        # dormant seam：recovery 通道本 slice **不激活**，registry 永不调用 observer。
        # accessor 先用轻量工厂——它足以让被显式触发的迁移留下审计记录，但**不是**
        # 权威 lifecycle 归属（那要求 resolver 持有 per-capability lifecycle；骨架
        # resolver 尚无 lifecycle_for）。权威 accessor 留待 recovery 激活 slice。
        health_observer = HealthObserver(
            lambda cid: CapabilityLifecycle(cid), transition_store
        )

    return governance_audit_sink, health_evidence_sink, health_observer, transition_store


def create_default_registry(core=None, capability_mode=None,
                            pilot_capability_allowlist=None,
                            capability_gateway_mode=None,
                            governance_audit_sink=None,
                            health_evidence_sink=None) -> ToolRegistry:
    """创建并注册全部内置工具，返回 ToolRegistry。

    M0.4.7 D-A2 —— config 接线（registry wiring）：
    ``capability_mode`` / ``pilot_capability_allowlist`` 为 ``None``（调用方未显式
    提供）时，从 ``core.config.capability_layer`` 读取；``core`` 或 config 缺失则
    回退默认 OFF / 空 allowlist（行为逐字节兼容 D-A1 之前的无 config 状态）。
    显式传入的值优先（replay 用 ``capability_mode=False`` 强制 OFF fallback，
    使其不受生产配置影响而保持确定性）。

    F8.6：``capability_gateway_mode`` / ``governance_audit_sink`` 同理——前者从
    config 读（默认 OFF），后者从 ``core.governance_audit_sink`` 取（缺省 None，
    R4 零开销）；二者均可显式传入以覆盖。

    F9.2 Operational Activation：三条治理证据链（resolution / health / recovery）
    的持久化出口改为 config 驱动构造（见 ``_wire_governance_sinks``）。开关默认全
    OFF —— 未启用时行为与 F9.2 之前逐字节一致（无 sink、无目录、无文件）。函数
    **签名不变**：观测面从 ``core.config.capability_layer`` 读，不新增参数。

    kill switch 语义（R4）：本函数只在新构造 registry 时**读取一次** config；
    运行期变更须受控重启生效——``ConfigService._sync_runtime`` 不热更 registry，
    本函数也不在运行期被重新调用以热切换 activation 状态。
    """
    cfg_layer = None
    if core is not None:
        cfg_layer = getattr(getattr(core, "config", None), "capability_layer", None)
    if capability_mode is None:
        capability_mode = (
            bool(getattr(cfg_layer, "capability_mode", False))
            if cfg_layer is not None else False
        )
    if pilot_capability_allowlist is None:
        pilot_capability_allowlist = (
            list(getattr(cfg_layer, "pilot_capability_allowlist", []) or [])
            if cfg_layer is not None else []
        )
    if capability_gateway_mode is None:
        capability_gateway_mode = (
            bool(getattr(cfg_layer, "capability_gateway_mode", False))
            if cfg_layer is not None else False
        )
    if governance_audit_sink is None and core is not None:
        governance_audit_sink = getattr(core, "governance_audit_sink", None)
    # F9.2：config 驱动的证据链接线（默认全 OFF → 全 None、零文件足迹）。
    (governance_audit_sink, health_evidence_sink,
     health_observer, transition_store) = _wire_governance_sinks(
        core, cfg_layer, governance_audit_sink, health_evidence_sink
    )
    from anima.tools.builtin import register_all_builtin
    registry = ToolRegistry(core, capability_mode=capability_mode,
                            pilot_capability_allowlist=pilot_capability_allowlist,
                            capability_gateway_mode=capability_gateway_mode,
                            governance_audit_sink=governance_audit_sink,
                            health_evidence_sink=health_evidence_sink,
                            health_observer=health_observer,
                            transition_store=transition_store)
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
