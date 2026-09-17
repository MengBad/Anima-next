"""ReAct 循环 —— Think → Act → Observe → Think 迭代。

设计：
- LLM 返回 tool_calls → 执行工具 → 结果回注 → 再问 LLM → 直到无 tool_calls 或达上限
- 每轮最多 N 次迭代（默认 5），防止无限循环
- 单工具异常不影响循环，返回错误让 LLM 自行调整
- 工具调用前按用户权限等级做二次校验
"""

from __future__ import annotations
import asyncio
import datetime
import json
import logging
import re
from typing import Any
from anima.llm.usage import llm_scope
from anima.pipeline.context import ContextComposer
from anima.tools import tool_result_ok

logger = logging.getLogger("anima.agent.react")

# 工具处理器通常不会抛异常，而是返回可读错误文本。若把这类文本也记成成功，
# 会污染工具自学习，并诱导模型围绕无效结果继续编造。
def _tool_result_ok(result: object) -> bool:
    return tool_result_ok(result)


#: P2-3：会产生「外部时效事实」的工具。命中后本轮回答要带来源/时间口径。
_FRESHNESS_TOOLS = frozenset({
    "web_search", "x_search", "query_web_memory", "bilibili_read",
    "skill_web_brief", "skill_member_profile", "skill_group_roster",
})

_FRESHNESS_GUIDANCE = (
    "上面是工具取到的事实材料。给最终回答时务必遵守："
    "① 涉及日期、数字、版本、价格的结论，带上你依据的来源或日期；"
    "② 材料里没有明确日期的，就写「未标注日期」，不要推断成「今天」「最近」；"
    "③ 材料明显陈旧时直接说明，不要当作最新事实自信断言；"
    "④ 工具没拿到有用信息就老实说没查到，不要用记忆补全成像是查到的样子。"
)

_FRESHNESS_FOOTER = "（以上来自联网检索；来源未标注具体日期，时效以抓取时间 {date} 为准）"

#: 回答里出现的「日期感」token：有其一就不追加时效脚注（说明它已经标注了时间口径）。
_DATE_TOKEN_RE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[:：]\d{2}",
)

#: 内部占位串 —— **不是内容**，绝不允许出现在任何消费方拿到的结果里。
#: `（想了一下不知道说什么...）` 目前**没有任何产生点**（全库只剩守卫在引用它），
#: 保留只为兼容仍在轮转的旧回复/旧日志，**不要删**。
_PLACEHOLDER_REPLIES = ("（思考中...）", "（想了一下不知道说什么...）")


def is_placeholder_reply(text: object) -> bool:
    """判定「非内容」占位串（空串同样算非内容）。

    🔴 用**前缀**判定，**不是**精确相等 —— 这是 09-18 缺陷的核心教训：
    占位串可能被**追加**装饰（典型是 `_apply_freshness_footer()` 直接拼时效脚注，
    因为它没有前导换行，结果是 `（思考中...）（以上来自联网检索；…）`），
    而下游两处守卫当时都写成 `reply.strip() in {"（思考中...）", …}` 的**精确集合**，
    装饰后**静默不命中** → 占位串原样直达用户。

    装饰只可能**追加**（不会前置），所以前缀判定足够，且不会误伤正文里提到
    这两个串的正常回答。共享此函数，避免各消费方各写一份判定再各自腐烂。
    """
    t = str(text or "").strip()
    if not t:
        return True
    return any(t.startswith(p) for p in _PLACEHOLDER_REPLIES)


class ReActLoop:
    """ReAct 执行循环。"""

    def __init__(self, llm, tools, max_rounds: int = 5,
                 permission_level: int = 2, quality=None,
                 tool_result_token_budget: int = 1800,
                 priority_sort: bool = True,
                 freshness_footer: bool = True):
        self.llm = llm
        self.tools = tools
        self.max_rounds = max_rounds
        self.permission_level = permission_level
        # 可选 ReplyQuality：记录工具空转 / 触顶，不阻塞主路径
        self.quality = quality
        # P0-1 Memory-First：schema 按 Tool.priority 升序（记忆类先于 web_search）。
        # 由 generation 依 config.react.memory_first 传入；关掉即回到注册顺序。
        self.priority_sort = bool(priority_sort)
        # P2-3：时效兜底脚注（只对用过时效工具、且回答里毫无日期痕迹的情况生效）
        self.freshness_footer = bool(freshness_footer)
        self._context_composer = ContextComposer(
            tool_result_token_budget=tool_result_token_budget,
        )
        # P0 工具自学习：采集本次回复里的工具调用轨迹（不进 LLM 上下文）
        self.tool_trace: list[dict] = []
        self._hit_tool_spin = False
        self._hit_max_rounds = False
        self._freshness_tools_used: set[str] = set()
        self._freshness_guidance_sent = False

    def _apply_freshness_footer(self, content: str) -> str:
        """回答里完全没有日期痕迹、但本轮确实查过外部事实 → 追加一句时效声明。

        只做「披露」，不改写答案内容：不加脚注等于让一个无日期的检索结论看起来像
        永恒事实，这对时效类问题是最常见的误导来源。
        """
        if not self.freshness_footer or not content:
            return content
        if is_placeholder_reply(content):
            # 占位串不是回答，补时间口径毫无意义；更糟的是拼接后会变成
            # `（思考中...）（以上来自联网检索；…）`，让下游「精确相等」守卫失效（09-18）。
            return content
        if not self._freshness_tools_used:
            return content
        if _DATE_TOKEN_RE.search(content):
            return content
        stamp = datetime.date.today().isoformat()
        return content + _FRESHNESS_FOOTER.format(date=stamp)

    def _empty_content_fallback(self) -> str:
        """上游把自己说空了 —— （content 空 **且** 纯对话重试也空）时的**唯一**出口。

        🔴 绝不返回内部占位串。占位串一旦产出，就有三个消费方，而**只有一个有守卫**：

        | 消费方 | 占位守卫 |
        |---|---|
        | `pipeline/stages/generation.py:718/756` | 有（此前是精确相等 → 被装饰后失效） |
        | `agent/executor.py:447` | **无** → 占位串成为该 step 的「结果」 |
        | `core/tasks.py:156` | **无** → 进 `task.evidence` 且任务被判 `completed` |

        所以封堵必须落在**源头**（这里），而不是给三个消费方各补一份判定 ——
        后者迟早会漏掉第四个消费方，或像 09-18 那样被一行装饰整体击穿。
        """
        if self.tool_trace and any(not item.get("ok") for item in self.tool_trace):
            return "本喵工具这边没查到准信喵，你换个说法再问一次？"
        if self.tool_trace:
            # 工具都“成功”但模型仍空回复：仍要出口，避免用户感觉 bot 死机。
            return "本喵查了一圈但没整理出有用结论喵，你换个具体视频链接（带 BV）再丢一次？"
        return "本喵刚卡住一下，你再说一遍喵？"

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
        self._freshness_tools_used = set()
        self._freshness_guidance_sent = False
        tool_schemas = self.tools.get_schemas(
            self.permission_level, sort_by_priority=self.priority_sort,
        ) if self.tools else []
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
                if is_placeholder_reply(content):
                    # 🔴 源头封堵（09-18）：此前这里是 `content or "（思考中...）"`，
                    # 占位串再被时效脚注装饰后，下游精确相等守卫全部失效 →
                    # 用户看到 `（思考中...）（以上来自联网检索；…）`；
                    # executor/tasks 两个无守卫消费方还会把它当成有效结果。
                    if content and content.strip():
                        logger.warning(
                            "ReAct 最终回复为占位串（上游全空），改用兜底出口: %r",
                            str(content)[:60],
                        )
                    else:
                        logger.warning("ReAct 最终回复为空（上游全空），改用兜底出口")
                    return self._empty_content_fallback()
                return self._apply_freshness_footer(content)

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
                # A2（收敛计划）：工具级超时——单工具卡死不再拖死整条回复
                try:
                    result = await asyncio.wait_for(
                        self.tools.call(name, args, self.permission_level),
                        timeout=120.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("工具调用超时(120s) name=%s", name)
                    result = "工具执行超时（超过 120 秒），本次未获得结果"
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
                if name in _FRESHNESS_TOOLS and ok:
                    self._freshness_tools_used.add(name)

            # P2-3：时效约束此前只在「触顶强制收尾」路径注入，正常一轮工具→作答的
            # 主路径（最常见）完全没有约束。这里在本轮工具结果之后补一条约束，
            # 让紧接着生成的回答就带上来源/时间口径；只注入一次，避免多轮累积占 token。
            if self._freshness_tools_used and not self._freshness_guidance_sent:
                self._freshness_guidance_sent = True
                current_msgs.append({
                    "role": "user",
                    "content": _FRESHNESS_GUIDANCE,
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
                    "根据上面工具结果，用一两句哈基米口吻给出最终回答。回答务必基于工具返回的内容，"
                    "并遵守：① 区分「检索查到的」与「你自己已知的」，不确定不要混为一谈；"
                    "② 若搜索结果没给出明确日期、或明显是旧信息，要明确说“这条比较旧/时效不确定”，"
                    "不要把它当最新事实自信断言；③ 涉及时间/新闻/价格等时效内容，最好带上你依据的日期或来源；"
                    "④ 若工具没拿到有用信息，就老实说没查到，不要编造，也不要输出空话。"
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
        if not is_placeholder_reply(content):
            return self._apply_freshness_footer(content)
        # 上游全空 → 与「早退路径」共用同一个兜底出口（单一口径，避免两处各自漂移）
        return self._empty_content_fallback()
