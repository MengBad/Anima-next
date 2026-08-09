"""F8.11 —— OutcomeNormalizer：把「执行表面事实」归一化为 governance 语义。

Q1 裁定：``ProviderResult.success`` 降级为 **transport / execution surface 字段**
（只表示「handler 返回了值」，混合了「handler returned normally」与「execution
succeeded」两种语义）。governance 的 canonical 输入是
:class:`NormalizedExecutionOutcome` —— 由本模块从 surface 事实**纯投影**而来。

Q2 裁定：governance 分类 adapter 只处理**已知安全 failure pattern**（本模块自带
最小注册表），未知返回 → ``UNKNOWN``。**不复制** tools 层 marker 系统（``_TOOL_
FAILURE_PREFIXES`` / ``_TOOL_INTERNAL_MARKERS``——避免双注册表长期漂移），也
**不 import** tools / runtime（R1 依赖面 + import 纪律：health 包零 runtime/tools
依赖，本模块用鸭子类型 ``getattr`` 读取输入）。

Q3 裁定：本模块是 **pure projection，不是 policy engine**——只做分类，不做
quarantine / retry / recovery 决策。

红线：
- R1：本模块不改 tools.normalize_tool_result / marker 注册表（模型面行为冻结）；
- R2：纯函数——输入不被变异，分类结果绝不反向影响 execution（方向只能
  execution → classification → evidence）；
- R3：``UNKNOWN`` 保留——不为了 Recovery 方便把未知强行归失败。
"""
from __future__ import annotations

import enum
from typing import Any

#: governance 专属失败前缀（Q2：已知安全 failure pattern，小注册表）。
#: 与 tools 层 _TOOL_FAILURE_PREFIXES 刻意不共享（R1 + 防漂移）；覆盖 F8.9.3
#: 钉死的 search.web 吞异常串（"搜索时出错了："）、_SAFE_TOOL_FAILURE 形态与
#: 通用 "错误:" 前缀。
_FAILURE_PREFIXES: tuple[str, ...] = (
    "搜索时出错了：",
    "工具暂时不可用",
    "错误:",
)

#: governance 专属「无法确定」marker（子串匹配）——命中 → UNKNOWN，绝不归 FAILURE。
_UNKNOWN_MARKERS: tuple[str, ...] = (
    "traceback",
    "[stderr]",
    "unexpected error",
    "exception:",
)

#: error_class 提取分隔符（取最后一段）。
_CLASS_SEPARATORS: tuple[str, ...] = ("：", ": ")


class NormalizedExecutionOutcome(str, enum.Enum):
    """governance 对一次执行的语义判定（Recovery 未来唯一消费对象，Q4）。"""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class OutcomeNormalizer:
    """把 surface 事实归一化为 :class:`NormalizedExecutionOutcome`（纯函数，无状态）。"""

    # -- 主入口 -------------------------------------------------------------
    @staticmethod
    def normalize(provider_result: Any, *, payload: str | None = None) -> "tuple[NormalizedExecutionOutcome, str | None]":
        """从 ProviderResult（鸭子类型：data/error/success）归一化。

        :returns: ``(outcome, error_class)``——``error_class`` 仅在 FAILURE 时非 None。
        """
        text = payload if payload is not None else str(getattr(provider_result, "data", "") or "")
        error = str(getattr(provider_result, "error", "") or "")
        outcome = OutcomeNormalizer.classify(text, error=error)
        error_class = (
            OutcomeNormalizer.extract_error_class(text, error)
            if outcome is NormalizedExecutionOutcome.FAILURE else None
        )
        return outcome, error_class

    @staticmethod
    def normalize_text(text: str) -> "tuple[NormalizedExecutionOutcome, str | None]":
        """纯文本归一化（无 ProviderResult 时的便捷入口）。"""
        return OutcomeNormalizer.normalize(None, payload=str(text or ""))

    # -- 分类 ---------------------------------------------------------------
    @staticmethod
    def classify(payload: str, *, error: str = "") -> NormalizedExecutionOutcome:
        """三态分类：显式 error → FAILURE；命中已知失败前缀 → FAILURE；
        命中未知 marker → UNKNOWN；其余 → SUCCESS。"""
        if error:
            # provider 显式声明失败（未来路径：ProviderResult.error 非空）→ 权威 FAILURE
            return NormalizedExecutionOutcome.FAILURE
        text = str(payload or "").strip().lower()
        for prefix in _FAILURE_PREFIXES:
            if text.startswith(prefix.lower()):
                return NormalizedExecutionOutcome.FAILURE
        for marker in _UNKNOWN_MARKERS:
            if marker in text:
                return NormalizedExecutionOutcome.UNKNOWN
        return NormalizedExecutionOutcome.SUCCESS

    @staticmethod
    def extract_error_class(payload: str, error: str = "") -> str | None:
        """从失败文本提取类型名（best-effort）："搜索时出错了：TimeoutError" →
        "TimeoutError"。取最后分隔符之后的部分；无分隔符则截断原文。"""
        src = (error or payload or "").strip()
        if not src:
            return None
        for sep in _CLASS_SEPARATORS:
            if sep in src:
                src = src.rsplit(sep, 1)[1].strip()
                break
        return src[:80] or None
