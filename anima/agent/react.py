"""ReAct 循环 —— Think → Act → Observe → Think 迭代。

设计：
- LLM 返回 tool_calls → 执行工具 → 结果回注 → 再问 LLM → 直到无 tool_calls 或达上限
- 每轮最多 N 次迭代（默认 5），防止无限循环
- 单工具异常不影响循环，返回错误让 LLM 自行调整
- 工具调用前按用户权限等级做二次校验
"""

from __future__ import annotations
import json
import logging
from typing import Any
from anima.llm.usage import llm_scope
from anima.pipeline.context import ContextComposer
from anima.tools import tool_result_ok

logger = logging.getLogger("anima.agent.react")

# 工具处理器通常不会抛异常，而是返回可读错误文本。若把这类文本也记成成功，
# 会污染工具自学习，并诱导模型围绕无效结果继续编造。
def _tool_result_ok(result: object) -> bool:
    return tool_result_ok(result)


class ReActLoop:
    """ReAct 执行循环。"""

    def __init__(self, llm, tools, max_rounds: int = 5,
                 permission_level: int = 2, quality=None,
                 tool_result_token_budget: int = 1800):
        self.llm = llm
        self.tools = tools
        self.max_rounds = max_rounds
        self.permission_level = permission_level
        # 可选 ReplyQuality：记录工具空转 / 触顶，不阻塞主路径
        self.quality = quality
        self._context_composer = ContextComposer(
            tool_result_token_budget=tool_result_token_budget,
        )
        # P0 工具自学习：采集本次回复里的工具调用轨迹（不进 LLM 上下文）
        self.tool_trace: list[dict] = []
        self._hit_tool_spin = False
        self._hit_max_rounds = False

    async def _finalize_content(self, messages: list[dict], temperature: float,
                                max_tokens: int,
                                structured_response: dict | None = None) -> str:
        """取最终回复文本。

        reasoning 模型（如 gemini-3.5-flash-low）偶发只把正文放进
        reasoning_content、content 字段返回空。策略：
          1. 若结构化响应已带非空 content 直接用；
          2. 否则重试一次**不带 tools 的纯对话**请求（网关此时稳定返回中文
             content），彻底绕开 reasoning-only 占位；
          3. 仍失败返回空串，调用方负责兜底。
        """
        if structured_response is not None:
            c = structured_response.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
        try:
            with llm_scope("chat", "chat"):
                c = await self.llm.chat(
                    messages, temperature=temperature, max_tokens=max_tokens
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ReAct 纯对话重试失败: %s", exc)
            return ""
        if isinstance(c, str) and c.strip():
            return c.strip()
        return ""

    async def run(self, messages: list[dict],
                  temperature: float = 0.8,
                  max_tokens: int = 512) -> str:
        """执行 ReAct 循环，返回最终回复文本。

        Args:
            messages: 初始对话消息（含 system prompt）
            temperature: LLM 温度
            max_tokens: 最大 token

        Returns:
            最终回复文本（LLM 的最终 content 字段）
        """
        self.tool_trace = []  # 每条回复重新采集
        tool_schemas = self.tools.get_schemas(self.permission_level) if self.tools else []
        current_msgs = list(messages)  # 不修改原始列表
        prev_sig = None  # 反工具死循环：记录上一轮工具调用签名

        for round_idx in range(self.max_rounds):
            # 询问 LLM（带已过滤的工具 schema）
            with llm_scope("chat", "react"):
                response = await self.llm.chat_structured(
                    current_msgs, tool_schemas,
                    temperature=temperature, max_tokens=max_tokens
                )

            # 检查是否有 tool_calls
            tool_calls = response.get("tool_calls", [])
            content = response.get("content", "") or ""

            if not tool_calls:
                # 没有工具调用 → 最终回复
                logger.debug("ReAct 轮次 %d: 无工具调用, 最终回复", round_idx + 1)
                if not content:
                    content = await self._finalize_content(
                        messages, temperature, max_tokens
                    )
                return content or "（思考中...）"

            # 反工具死循环：工具调用签名与上一轮完全相同（同一查询重复搜）
            # → 立即转出最终回复，避免 5 轮空转把延迟拖到 15s 且最终仍可能 content 空。
            # 覆盖单工具和多工具场景：把本轮所有工具调用签名排序后做指纹比对。
            if len(tool_calls) >= 1:
                sigs = tuple(sorted(
                    (tc.get("function", {}).get("name", ""),
                     tc.get("function", {}).get("arguments", "{}"))
                    for tc in tool_calls
                ))
                if sigs == prev_sig:
                    logger.info(
                        "ReAct 轮次 %d: 检测到重复工具调用 %s，提前结束转最终回复",
                        round_idx + 1, [s[0] for s in sigs],
                    )
                    self._hit_tool_spin = True
                    try:
                        if self.quality is not None:
                            self.quality.record_tool_spin(1)
                    except Exception:
                        logger.exception("工具空转遥测记录失败")
                    break
                prev_sig = sigs

            # 有工具调用 → 执行
            logger.info("ReAct 轮次 %d: %d 个工具调用", round_idx + 1, len(tool_calls))

            # 添加 assistant 消息
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
            assistant_msg["tool_calls"] = tool_calls
            current_msgs.append(assistant_msg)

            # 执行每个工具，添加结果
            for i, tc in enumerate(tool_calls):
                func = tc.get("function", {})
                name = func.get("name", "unknown")
                args_str = func.get("arguments", "{}")
                call_id = tc.get("id") or f"call_{round_idx}_{i}"

                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}

                # 权限二次校验（工具调用前的最后一道闸）
                result = await self.tools.call(name, args, self.permission_level)
                # 工具参数和结果可能含用户正文、URL、文件路径；journal 只保留
                # 名称、成败和长度，排障走受控 audit/任务记录而非聊天日志。
                ok = _tool_result_ok(result)
                logger.info("工具调用完成 name=%s ok=%s result_len=%d", name, ok, len(str(result)))

                # P0 工具自学习：记录轨迹（仅采集 logger 已打印过的信息）
                self.tool_trace.append({
                    "name": name,
                    "args": args,
                    "result": str(result)[:200],
                    "ok": ok,
                    "round": round_idx + 1,
                })

                # 网页/文档工具结果可能非常大；截到 token 预算内后再回注，
                # 否则一次搜索就能撑爆后续 ReAct 轮次并挤掉当前会话。
                result_for_context = self._context_composer.compact_tool_result(str(result))
                current_msgs.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_for_context,
                })

        # 达到最大轮次（或被反死循环守卫 break）→ 最后问一次，无工具
        if not self._hit_tool_spin:
            self._hit_max_rounds = True
            try:
                if self.quality is not None:
                    self.quality.record_react_max_rounds()
            except Exception:
                logger.exception("ReAct 触顶遥测记录失败")
        logger.info("ReAct 达到最大轮次 %d, 强制最终回复", self.max_rounds)
        # 若工具已执行过，把事实约束写进最后一轮，减少空兜底/瞎编。
        if self.tool_trace:
            current_msgs.append({
                "role": "user",
                "content": (
                    "根据上面工具结果，用一两句哈基米口吻给出最终回答；"
                    "若工具没拿到有用信息，就老实说没查到，不要编造，也不要输出空话。"
                ),
            })
        with llm_scope("chat", "react_finalize"):
            final = await self.llm.chat_structured(
                current_msgs, [],  # 不再给工具
                temperature=temperature, max_tokens=max_tokens
            )
        # 关键：强制收尾必须基于 current_msgs（含 tool 结果），绝不能回落到
        # 原始 messages，否则会丢掉全部工具事实，reasoning 模型更容易空 content。
        content = await self._finalize_content(
            current_msgs, temperature, max_tokens, final
        )
        if content and content.strip() not in {"（想了一下不知道说什么...）", "（思考中...）"}:
            return content
        if self.tool_trace and any(not item.get("ok") for item in self.tool_trace):
            return "本喵工具这边没查到准信喵，你换个说法再问一次？"
        if self.tool_trace:
            # 工具都“成功”但模型仍空回复：仍要出口，避免用户感觉 bot 死机。
            return "本喵查了一圈但没整理出有用结论喵，你换个具体视频链接（带 BV）再丢一次？"
        return "本喵刚卡住一下，你再说一遍喵？"
