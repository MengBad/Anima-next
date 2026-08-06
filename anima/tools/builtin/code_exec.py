"""M0.4.6 D2 —— `code.execute` Provider Adapter（极薄包装层，委托 run_python）。

纪律（M0.4.6 Step 3，APPROVED FOR IMPLEMENTATION，D2 范围限定）：

- 本模块定义 provider Tool（``name="code_exec"``，LLM schema 名 ``code_execute``）。
- handler 是极薄 adapter：做 allowlist schema 校验，然后委托给**已硬化**的
  ``run_python`` 基底（``registry.call("run_python", {"code": ...})``）。
- 它**不**实现沙箱、权限模型、或审计管线——这些都由 ``ToolRegistry._invoke``
  在委托的 ``run_python`` 调用上统一执行。
- 本模块**不**在 ``builtin/__init__.py`` 中 import / 注册（那是 D3）。因此默认情况下
  它是惰性代码：只有后续单独 gated 的 D3 步骤才会 import 并 ``register`` 它。
- D2 阶段 Tool 的 ``capability_ids=[]``、**不绑定 capability**（D3 才会加
  ``capability_ids=["code.execute"]`` + ``granted_scopes={"exec": True}`` 并注册）。

审计透明性：``code_exec`` 不在 ``EXECUTION_TOOLS`` 内，因此外层 ``_invoke`` 不产生
任何 ``execution_*`` 审计事件；唯一的执行轨迹来自被委托的 ``run_python._invoke``。
无双审计。
"""

from __future__ import annotations

from typing import Any

from anima.tools import LEVEL_USER, Tool, get_call_context


class CodeExecSchemaViolation(Exception):
    """Provider 收到 allowlist 之外的字段时抛出（契约 v0.2 §6.1：拒绝而非静默丢弃）。"""


# 允许列表：LLM 唯一可传递的字段。
_CODE_EXECUTE_ALLOWED_FIELDS = frozenset({"code"})


def make_code_exec_tool(registry) -> Tool:
    """构造 `code.execute` provider Tool（run_python 之上的 adapter）。

    返回的 Tool **未绑定 capability**（``capability_ids=[]``）：D3（独立 gate）才会
    附加 capability 绑定并注册它。在此之前本 Tool 存在但不作为 `code.execute` 的
    Provider 挂载（``providers_for("code.execute")`` 仍为 ``[]``）。
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
        # D2：暂**不绑定** capability——capability_ids 为空，不声明任何 scope。
        #     （D3 会改为 capability_ids=["code.execute"] + granted_scopes={"exec": True}
        #      并注册——属于独立 gate，不在本步。）
        capability_ids=[],
        granted_scopes=None,
    )


__all__ = ["make_code_exec_tool", "CodeExecSchemaViolation"]
