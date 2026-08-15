"""M0.4.6 D2 —— `code.execute` Provider Adapter（极薄包装层，委托 run_python）。

纪律（M0.4.6 Step 3，APPROVED FOR IMPLEMENTATION，D2 范围限定）：

- 本模块定义 provider Tool（``name="code_exec"``，LLM schema 名 ``code_execute``）。
- handler 是极薄 adapter：做 allowlist schema 校验，然后委托给**已硬化**的
  ``run_python`` 基底（``registry.call("run_python", {"code": ...})``）。
- 它**不**实现沙箱、权限模型、或审计管线——这些都由 ``ToolRegistry._invoke``
  在委托的 ``run_python`` 调用上统一执行。
- D3（独立 gate）已在本模块 import 并 ``register``：``register_all_builtin`` 调用
  ``make_code_exec_tool(registry, capability_ids=["code.execute"], granted_scopes={"exec": True})``
  完成单 provider 挂载（``providers_for("code.execute") == ["code_exec"]``）。
- 模块工厂函数接受可选的 ``capability_ids`` / ``granted_scopes`` 参数，默认 ``[]`` / ``None``，
  以便 D2 测试（无绑定时）仍能验证「不挂载为 provider」。绑定由 D3 的注册调用显式注入。

审计透明性：``code_exec`` 不在 ``EXECUTION_TOOLS`` 内，因此外层 ``_invoke`` 不产生
任何 ``execution_*`` 审计事件；唯一的执行轨迹来自被委托的 ``run_python._invoke``。
无双审计。
"""

from __future__ import annotations

from typing import Any, Optional

from anima.tools import LEVEL_USER, Tool, get_call_context


class CodeExecSchemaViolation(Exception):
    """Provider 收到 allowlist 之外的字段时抛出（契约 v0.2 §6.1：拒绝而非静默丢弃）。"""


# 允许列表：LLM 唯一可传递的字段。
_CODE_EXECUTE_ALLOWED_FIELDS = frozenset({"code"})


def make_code_exec_tool(registry, capability_ids: Optional[list] = None,
                        granted_scopes: Any = None,
                        capability_only: bool = False) -> Tool:
    """构造 `code.execute` provider Tool（run_python 之上的 adapter）。

    ``capability_ids`` / ``granted_scopes`` 默认 ``None``（等价 ``[]`` / 空集），
    使 D2 测试在无绑定时仍能验证「不挂载为 provider」。D3 由 ``register_all_builtin``
    显式注入 ``capability_ids=["code.execute"]`` + ``granted_scopes={"exec": True}``
    完成单 provider 挂载；在此之前本 Tool 存在但不作为 `code.execute` 的 Provider
    （``providers_for("code.execute")`` 为空）。

    ``capability_only`` 默认 ``False``（保持 D2 无绑定语义：未挂载时直接 ``call``
    可调）；M0.4.7 D-A0 由 ``register_all_builtin`` 注入 ``True``，使挂载后的
    ``code_exec`` 退出 legacy schema 并禁止 legacy 直调（F0 closure）。
    """

    async def code_exec_handler(code: str, **kw: Any) -> str:
        # (1) Schema 边界：allowlist。拒绝任何 `code` 之外的字段。
        #     纵深防御——即便原始 args（react.py 透传）携带注入字段，也在此拒绝，
        #     而非静默忽略（契约 v0.2 §6.1）。
        extra = set(kw) - _CODE_EXECUTE_ALLOWED_FIELDS
        if extra:
            registry._audit(
                "execution_denied",
                reason="schema_violation",
                tool="code_exec",
                rejected_fields=sorted(extra),
            )
            raise CodeExecSchemaViolation(
                f"code.execute 收到未授权字段: {sorted(extra)}"
            )

        # (2) 只信可信 ctx。绝不读取调用方 args 中的 permission_level
        #     （R1：沙箱分级仅由可信 ctx 派生）。被委托的 run_python._invoke 还会
        #     无条件用可信 ctx 覆盖 args 的 permission_level——此处传递可信值只是纵深防御，
        #     不构成新的权限来源。
        ctx = get_call_context()
        trusted_level = int(ctx.get("permission_level", LEVEL_USER))

        # (3) 委托给已硬化基底。执行权（exec gate / sandbox / audit）完全由
        #     ToolRegistry._invoke 在 run_python 调用上行使。
        return await registry.call(
            "run_python",
            {"code": code},
            permission_level=trusted_level,
        )

    return Tool(
        name="code_exec",
        description=(
            "在受控沙箱中执行用户授权的 Python 代码（code.execute 能力）。"
            "只能接收 code 字段；禁止任何 sandbox / 文件系统 / 网络参数透传。"
        ),
        parameters={
            "code": {"type": "string", "description": "要执行的 Python 代码"},
        },
        handler=code_exec_handler,
        required=["code"],
        min_level=LEVEL_USER,
        # D3 由 register_all_builtin 注入真实绑定（capability_ids=["code.execute"]
        # + granted_scopes={"exec": True}）。默认 None → [] / 空集，保持 D2 无绑定语义。
        capability_ids=capability_ids,
        granted_scopes=granted_scopes,
        # M0.4.7 D-A0：挂载时由 register_all_builtin 注入 True，关闭 legacy 直调面（F0 closure）。
        capability_only=capability_only,
    )


# ---------------------------------------------------------------------------
# R6.1 —— `python.exec` Provider 适配 Tool（owner-only 执行边界）
#
# 纪律（R6.1 anchored 当前 task）：
# - 本 Tool 仅作为 capability_index 的可解析入口 + gateway 路由触发点；
#   真实执行权（owner authorization / sandbox / audit）完全由
#   ``PythonExecProvider``（经 capability gateway 路由）行使。
# - ``capability_only=True``：关闭 legacy 直调面（F0 closure），仅经 capability
#   路由可达；即便 legacy 直调被绕过，下方 handler 也**绝不执行代码**。
# - handler 仅为占位：任何经 legacy 触发的调用都返回明确拒绝，不委托 run_python。
#   唯一真实的执行路径是 gateway → PythonExecProvider.invoke 的硬 owner 边界。
# - 不引入新的沙箱 / 权限模型 / 审计管线；完全复用 R5 已硬化的 sandbox.run_python。
# ---------------------------------------------------------------------------

def make_python_exec_tool(registry, capability_ids: Optional[list] = None,
                          granted_scopes: Any = None,
                          capability_only: bool = False) -> Tool:
    """构造 `python.exec` 的 capability 入口 Tool（owner-only，经 gateway 执行）。

    与 ``make_code_exec_tool`` 同构，但语义更严格：
    - ``capability_only`` 默认 ``False``（保持无绑定语义）；由 ``register_all_builtin``
      注入 ``True``，关闭 legacy 直调面。
    - handler **绝不执行代码**（只占位 + 审计），真实执行须经 gateway → Provider 的
      硬 owner-only 边界。
    """

    async def python_exec_handler(code: str, **kw: Any) -> str:
        # R6.1：python.exec 只经 capability 路由（gateway → PythonExecProvider）执行；
        # legacy 直调已被 capability_only 拦截。此 handler 仅作占位，绝不执行代码。
        # 即便 capability_only 被绕过，也不会落到 run_python。
        registry._audit(
            "execution_denied",
            reason="legacy_path_blocked",
            tool="python_exec",
            capability="python.exec",
        )
        return "[R6.1] python.exec 仅经 capability 路由可达；legacy 直调已禁用。"

    return Tool(
        name="python_exec",
        description=(
            "在受控沙箱中执行主人（owner）授权的 Python 代码（python.exec 能力，owner-only）。"
            "只能接收 code 字段；禁止任何 sandbox / 文件系统 / 网络参数透传。"
        ),
        parameters={
            "code": {"type": "string", "description": "要执行的 Python 代码"},
        },
        handler=python_exec_handler,
        required=["code"],
        min_level=LEVEL_USER,
        capability_ids=capability_ids,
        granted_scopes=granted_scopes,
        capability_only=capability_only,
    )


__all__ = ["make_code_exec_tool", "make_python_exec_tool", "CodeExecSchemaViolation"]
