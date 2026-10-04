"""Phase 3 任务规划器：把自然语言请求分解为有序步骤。

优先用 LLM 结构化分解；LLM 不可用或解析失败时回退到确定性启发式，
保证在任何环境下都能产出可用计划（便于测试与离线运行）。
"""

from __future__ import annotations

import json
import logging
import re

from anima.agent.state import AgentTask, Step
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.agent.planner")

_SYSTEM_PROMPT = (
    "你是 Anima 的任务规划器。把用户的工程/检索请求分解为 2-6 个有序步骤，"
    "每步是一句可执行的操作描述（中文，动宾结构）。\n"
    "只输出一个 JSON 数组。元素可以是字符串，也可以是对象：\n"
    '{"description":"...","verify":"如何验收这一步（可观测判据）",'
    '"parallel_group":"同组且互不依赖的步骤填同名，无依赖关系则留空"}\n'
    "示例：[\"搜索 X 的官方文档\",{\"description\":\"写入结果文件\","
    "\"verify\":\"文件存在且包含 Y\"}]\n"
    "不要输出任何解释或 markdown 代码块。"
)

_REPLAN_SYSTEM_PROMPT = (    "你是 Anima 的任务重规划器。原计划的一步失败了，请给出**替代方案**的后续步骤。\n"
    "要求：\n"
    "1. 不要重复已经成功的步骤，也不要原样重试失败的那一步（换思路/换工具/降级目标）。\n"
    "2. 只输出 1-4 步，JSON 数组，元素格式与规划一致（字符串或 "
    '{"description":"...","verify":"..."}）。\n'
    "3. 若失败原因是能力缺失（工具不存在、未授权、外部服务不可用），就改为"
    "「如实说明限制并汇总已有结果」，不要硬凑。\n"
    "不要输出任何解释或 markdown 代码块。"
)


def _is_empty_array(text: str) -> bool:
    """判断 LLM 输出是不是一个「明确的空数组」（``[]`` / ```[]```）。

    与「输出无法解析」区分开：前者是模型明确表态「没有可行替代」，
    后者才该回落到确定性兜底。
    """
    t = (text or "").strip()
    m = re.search(r"\[.*?\]", t, re.DOTALL)
    if not m:
        return False
    try:
        return json.loads(m.group(0)) == []
    except json.JSONDecodeError:
        return False


class TaskPlanner:
    def __init__(self, llm=None):
        self.llm = llm
    async def plan(self, request: str, task: AgentTask | None = None) -> list[Step]:
        raw = await self._decompose(request)
        return self._to_steps(raw)

    @staticmethod
    def _to_steps(raw: list[dict]) -> list[Step]:
        steps: list[Step] = []
        for i, item in enumerate(raw):
            steps.append(Step(
                index=i,
                description=str(item.get("description", "")).strip(),
                verify=str(item.get("verify", "")).strip(),
                parallel_group=str(item.get("parallel_group", "")).strip(),
            ))
        return [s for s in steps if s.description]

    async def replan(self, *, request: str, done: list[str], failed_step_desc: str,
                     error: str, remaining: list[str],
                     reason: str = "step_failed") -> list[Step]:
        """为失败步骤产出替代方案。

        reason:
          - step_failed：工具重试耗尽
          - verify_failed：执行成功但独立验收判定不成立
        """
        planned = await self._decompose_replan(
            request=request, done=done, failed_step_desc=failed_step_desc,
            error=error, remaining=remaining, reason=reason,
        )
        steps = self._to_steps(planned)
        for s in steps:
            s.origin = "replan"
        return steps

    async def _decompose_replan(self, **kw) -> list[dict]:
        if self.llm is None:
            # 09-26：`planner.llm` 曾因构造期捕获而在生产恒为 None，且**全程无日志**
            # ⇒ 「模型没给出方案」与「根本没调模型」无法区分。此分支必须留痕。
            logger.info("重规划器未绑定 LLM（planner.llm=None），改用确定性兜底方案")
            return self._fallback_replan(**kw)
        raw = ""
        try:
            raw = await self._call_llm_replan(**kw)
            parsed = self._parse_llm(raw)
            if parsed:
                logger.info("LLM 重规划产出 %d 步", len(parsed))
                return parsed
            if _is_empty_array(raw):
                # LLM 明确表示「没有可行替代」→ 如实失败，不要用兜底话术硬凑
                logger.info("LLM 重规划明确返回空数组（无可替代方案），按失败处理")
                return []
        except Exception:
            logger.warning("LLM 重规划调用异常，回退确定性兜底方案", exc_info=True)
        logger.warning("LLM 重规划输出不可用（%r），回退确定性兜底方案", (raw or "")[:200])
        return self._fallback_replan(**kw)

    @scoped_llm("agent", "replan")
    async def _call_llm_replan(self, *, request: str, done: list[str],
                               failed_step_desc: str, error: str,
                               remaining: list[str], reason: str) -> str:
        kind = "执行成功但未通过验收" if reason == "verify_failed" else "执行失败"
        payload = (
            f"原始请求：{request}\n"
            f"已完成步骤：{done or '（无）'}\n"
            f"失败步骤：{failed_step_desc}（{kind}）\n"
            f"失败原因：{error}\n"
            f"原计划剩余步骤：{remaining or '（无）'}"
        )
        messages = [
            {"role": "system", "content": _REPLAN_SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ]
        resp = await self.llm.chat(messages, temperature=0.2, max_tokens=600)
        return resp if isinstance(resp, str) else str(resp)

    @staticmethod
    def _fallback_replan(*, request: str, done: list[str], failed_step_desc: str,
                         error: str, remaining: list[str],
                         reason: str = "step_failed") -> list[dict]:
        """确定性兜底：换一种方式再试 + 保留原剩余步骤 + 诚实收尾。"""
        if reason == "verify_failed":
            alt = f"换用不同方式重做：{failed_step_desc}（上次结果未通过验收）"
        else:
            alt = f"换用不同方式完成：{failed_step_desc}（上次失败：{error[:60]}）"
        out: list[dict] = [{"description": alt, "verify": "本次产出不含错误标记"}]
        for r in (remaining or [])[:2]:
            out.append({"description": r})
        out.append({"description": "如实说明本次失败的限制并汇总已有结果"})
        return out

    async def _decompose(self, request: str) -> list[dict]:
        if self.llm is None:
            # 09-26 修复：这里曾静默走兜底（`if self.llm is not None` 恒假），
            # 生产所有 Agent 计划都退化成 `_fallback` 关键词拼接，且日志零痕迹。
            logger.warning("规划器未绑定 LLM（planner.llm=None），改用确定性兜底计划")
            return [{"description": d} for d in self._fallback(request)]
        raw = ""
        try:
            raw = await self._call_llm(request)
            parsed = self._parse_llm(raw)
            if parsed:
                logger.info("LLM 规划产出 %d 步", len(parsed))
                return parsed
        except Exception:
            logger.warning("LLM 规划调用异常，回退确定性兜底计划", exc_info=True)
        logger.warning("LLM 规划输出不可用（%r），回退确定性兜底计划", (raw or "")[:200])
        return [{"description": d} for d in self._fallback(request)]

    @scoped_llm("agent", "plan")
    async def _call_llm(self, request: str) -> str:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": request},
        ]
        resp = await self.llm.chat(messages, temperature=0.2, max_tokens=600)
        return resp if isinstance(resp, str) else str(resp)

    @staticmethod
    def _parse_llm(text: str) -> list[dict]:
        """解析 LLM 输出为步骤 dict 列表。字符串元素与对象元素都接受。

        解析失败返回空列表（调用方回退到确定性启发式），不抛异常。
        """
        text = (text or "").strip()
        # 去掉可能的 ```json 代码块围栏
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(arr, list):
            return []
        out: list[dict] = []
        for item in arr[:6]:
            if isinstance(item, str):
                desc = item.strip()
                if desc:
                    out.append({"description": desc, "verify": "", "parallel_group": ""})
            elif isinstance(item, dict):
                desc = str(item.get("description") or "").strip()
                if not desc:
                    continue
                out.append({
                    "description": desc,
                    "verify": str(item.get("verify") or "").strip(),
                    "parallel_group": str(item.get("parallel_group") or "").strip(),
                })
        return out

    @staticmethod
    def _fallback(request: str) -> list[str]:
        """确定性兜底：按关键词拼出通用步骤。"""
        req = request or ""
        steps: list[str] = []
        if re.search(r"搜|查|找|fetch|http|网页|文档|下载|拉取", req, re.I):
            steps.append("收集相关上下文与资料（联网检索/读取）")
        if re.search(r"写|生成|创建|文件|报告|总结", req, re.I):
            steps.append("整理结论并写入工作区文件")
        if re.search(r"运行|执行|跑|python|脚本|代码|部署|安装", req, re.I):
            steps.append("用沙箱执行核心操作并收集输出")
        if not steps:
            steps.append("理解请求并收集必要信息")
            steps.append("执行核心操作")
        steps.append("验证结果正确性")
        steps.append("汇总回复主人")
        # 去重保序
        seen = set()
        unique = []
        for s in steps:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique[:6]
