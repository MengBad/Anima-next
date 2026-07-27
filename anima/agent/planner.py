"""Phase 3 任务规划器：把自然语言请求分解为有序步骤。

优先用 LLM 结构化分解；LLM 不可用或解析失败时回退到确定性启发式，
保证在任何环境下都能产出可用计划（便于测试与离线运行）。
"""

from __future__ import annotations

import json
import re

from anima.agent.state import AgentTask, Step

_SYSTEM_PROMPT = (
    "你是 Anima 的任务规划器。把用户的工程/检索请求分解为 2-6 个有序步骤，"
    "每步是一句可执行的操作描述（中文，动宾结构）。\n"
    "只输出一个 JSON 数组，例如：[\"搜索 X 的官方文档\",\"编写脚本验证\",\"写入结果文件\"]。"
    "不要输出任何解释或 markdown 代码块。"
)


class TaskPlanner:
    def __init__(self, llm=None):
        self.llm = llm

    async def plan(self, request: str, task: AgentTask | None = None) -> list[Step]:
        descriptions = await self._decompose(request)
        return [Step(index=i, description=d) for i, d in enumerate(descriptions)]

    async def _decompose(self, request: str) -> list[str]:
        if self.llm is not None:
            try:
                raw = await self._call_llm(request)
                parsed = self._parse_llm(raw)
                if parsed:
                    return parsed
            except Exception:
                pass
        return self._fallback(request)

    async def _call_llm(self, request: str) -> str:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": request},
        ]
        resp = await self.llm.chat(messages, temperature=0.2, max_tokens=600)
        return resp if isinstance(resp, str) else str(resp)

    @staticmethod
    def _parse_llm(text: str) -> list[str]:
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
        return [str(x).strip() for x in arr if str(x).strip()][:6]

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
