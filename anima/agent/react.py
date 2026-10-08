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
import random
import re
import time
from typing import Any
from anima.llm import outbound_circuit_open_until, outbound_consecutive_failures  # 等待预算需**出站**视角（2026-10-02/03）
from anima.llm.usage import llm_scope
from anima.pipeline.context import ContextComposer
from anima.tools import tool_result_ok

logger = logging.getLogger("anima.agent.react")

# 工具处理器通常不会抛异常，而是返回可读错误文本。若把这类文本也记成成功，
# 会污染工具自学习，并诱导模型围绕无效结果继续编造。
def _tool_result_ok(result: object) -> bool:
    return tool_result_ok(result)


#: P2-3：会产生「外部时效事实」的工具。命中后本轮回答要带来源/时间口径。
#:
#: 🔴 2026-10-06（r27）：这里必须写 **LLM 真正会调用的名字**，不是 Provider/Tool id。
#: 生产事故：集合里原写的是 Provider id ``web_search``，而 LLM 调用的是
#: capability schema name ``search_web``（``capability/index.schema_name_for``：
#: ``search.web`` → ``search_web``；Tool ``web_search`` 挂了
#: ``capability_ids=["search.web"]`` ⇒ 在 capability 模式下**其 Tool 名对 LLM 不可见**）。
#: ⇒ 7d 内 **22 个联网搜索回合**既没拿到时效约束、也没追加时效脚注 —— **静默失效**。
#: 日志实证：``name=web_search`` 出现 **0** 次、``name=search_web`` 出现 **22** 次。
#: 守卫见 ``tests/test_r27_freshness_gate.py::test_freshness_tools_are_llm_facing_names``。
_FRESHNESS_TOOLS = frozenset({
    "search_web", "x_search", "query_web_memory", "bilibili_read",
    "skill_web_brief", "skill_member_profile", "skill_group_roster",
})

#: 「取到可用内容」的最小字符数 —— 时效脚注的**必要**条件之一。
#:
#: 🔴 为什么需要它：脚注的前置条件原先只有 ``_tool_result_ok()``，而它判的是
#: 「非空 ∧ 无失败标记」⇒ **「本视频没有字幕」这类「成功但没内容」也算 ok**，
#: 于是脚注照加，出现「正文说抓不到 / 脚注说刚在网上翻到了」的自相矛盾
#: （72h 实测 14 条脚注里 **12 条**如此）。
#:
#: 阈值取自**生产实测分布**（7d，60 次工具调用的 ``result_len``）——**双峰、无重叠**：
#:   无内容：``query_web_memory`` 19、``x_search`` 23、``bilibili_read`` 49~95
#:   有内容：``x_search`` 309~1240、``search_web`` 3362~13019
#: 即**无内容上界 95、有内容下界 309**，取 200 落在 3 倍空档中间。
#: ⚠️ 这是**长度代理**，不是语义判定：宁可保守（够长才算「取到」），
#: 因为少加一次脚注只是少一句披露，而**加错脚注是在说假话**。
_FRESHNESS_MIN_CONTENT_CHARS = 200


def _has_usable_content(result: object) -> bool:
    """工具结果是否**确有可用内容**（区别于「成功但什么都没取到」）。"""
    text = str(result or "").strip()
    return len(text) >= _FRESHNESS_MIN_CONTENT_CHARS


_FRESHNESS_GUIDANCE = (
    "上面是工具取到的事实材料。给最终回答时务必遵守："
    "① 涉及日期、数字、版本、价格的结论，带上你依据的来源或日期；"
    "② 材料里没有明确日期的，就写「未标注日期」，不要推断成「今天」「最近」；"
    "③ 材料明显陈旧时直接说明，不要当作最新事实自信断言；"
    "④ 工具没拿到有用信息就老实说没查到，不要用记忆补全成像是查到的样子。"
)

#: `_empty_content_fallback()` 的三池等义话术（2026-09-19 去模板化）。
#:
#: 背景：三池原本各只有**一句**固定措辞，实测 09-18 单是「无工具」那句就在
#: 06/12/22 三个时段被复用 6 次 —— 用户侧读起来就是复读。改为同池多句随机取。
#:
#: 硬约束（与 D-19 一致，改文案时不得破坏，由 test_react_empty_fallback_wording 守护）：
#: - 场景无关：任何一句都不得绑定具体工具/平台（BV、视频、链接…）；
#: - 保留口吻：每句都含「本喵」与「喵」，不得退化成机械话术；
#: - 池间不相交：三池两两无重复，保证分支仍可区分。
#: - **不得伪装成"没听清"**（2026-09-20 深审新增）：本出口在「上游拒答」
#:   与「上游卡壳」两种情况下**长得一样**，无法区分。原「无工具」池三句全是
#:   「你再说一遍 / 你重发一下」——用户读到就以为 bot 聋了，于是原样重发、
#:   再撞同一堵墙（实测 G5/G7：露骨内容 → 「你再说一遍喵？」→ 用户重发 → 循环）。
#:   改为「换句话说 / 换个话题」：给出口而**不承诺重听**，也不再索取原样复述。
#: - **不得甩锅话题**（2026-09-22 深审新增，仅约束「无工具」池）：09-20 那次的
#:   「换个话题」同样是个错误修法 —— 实测 09-22 14:39，三个候选模型对同一请求
#:   **全部返回 HTTP 200 + 空 content**（网关侧静默返空），穷尽 failover 后落本池，
#:   出站「本喵刚没想好怎么接喵，你换个话题也行。」用户读到的是「bot 嫌我聊的话题」。
#:   病因与用户说了什么**无关** ⇒ 无工具池改为**自认是本喵这边的岔子**：既不评价
#:   话题、也不索取复述。「工具」两池不受此约束 —— 那里工具确实没成，请用户把条件
#:   说得更具体是真话，不是甩锅。
#: 🔴 2026-10-08 扩池（3 → 7）：生产实证 4 次兜底里 **3 次抽到同一句**
#: （`chat_memory.json` 实测「打了个嗝」×3 + 「卡了一下」×1）⇒ 用户侧读起来就是复读。
#: 3 句池在 4 次抽样下出现重复是数学必然。新增 4 句仍严守下列全部硬约束：
#: 场景无关 / 保留「本喵…喵」口吻 / 自认是本喵这边的岔子 /
#: 不索取原样复述 / 不甩锅话题 / 不伪装「没听清」。
#: 由 `tests/test_react_empty_fallback_wording.py` 逐句守护（含「≥6 句」下限）。
_NO_TOOL_FALLBACKS = (
    "本喵这边刚卡了一下喵，这条没接住，不赖你。",
    "唔……本喵这边出了点小岔子喵，刚才那句没生成出来。",
    "本喵这边刚打了个嗝喵，这条没答上来，不是不想理你。",
    "本喵这边刚才走神了一下喵，这句没接上，你继续说你的。",
    "咦，本喵这边好像漏了一拍喵，刚才那句没吐出来，本喵缓缓。",
    "本喵这边卡壳了喵，这条没接住，真不是故意晾着你。",
    "本喵这边刚才有点犯迷糊喵，这句话没组织出来，别误会喵。",
)
_TOOL_FAILED_FALLBACKS = (
    "本喵工具这边没查到准信喵，你换个说法再问一次？",
    "本喵这边工具有点不灵光，没摸着准信喵……换个说法再问一次？",
    "工具没帮上本喵的忙喵，你换句话再问一遍试试？",
)
_TOOL_OK_FALLBACKS = (
    "本喵查了一圈但没整理出有用的结论喵，你换个说法、或者把条件说细一点再问我一次？",
    "本喵翻了一圈没拢出个像样的结论喵，要说细一点、或者换个问法吗？",
    "查是查了，但本喵没拼出有用的一句话喵……你换个说法再试一次？",
)

#: 「上游根本没给出任何东西」的一手计数（llm 侧进程内累计，只增不减）。
#:
#: 🔴 三个都要看，**不能只看第一个**（2026-09-23 定案）。三者的触发路径互不重叠：
#:   * ``outbound_empty_completion_count`` —— HTTP 200 但补全为空（网关静默降级）；
#:   * ``outbound_failure_count`` —— 候选穷尽后仍失败（连接被重置 / 5xx / 超时等传输故障）；
#:   * ``outbound_circuit_open_count`` —— 熔断器打开的次数。
#: 熔断打开后 ``llm/__init__.py`` 的 ``_post`` **早退**，连 200-空的计数都不增 ⇒
#: 只看 200-空 一项时，14:55 那种纯传输故障会被 ``tool_trace`` 洗成
#: ``fallback_no_tool``（"模型自己无话可说"）—— 审计据此读结论就会读错。
#:
#: 🔴 三个都**只统计出站正文调用**（`llm_scope(..., outbound=True)`），不是进程级全量
#: 计数（2026-09-24 定案）。旁路与出站共用同一个 llm 实例，用全量计数时旁路的故障
#: 会推高增量 ⇒ 归因桶翻成 ``upstream_empty`` 并白等一次预算（09-24 生产实证：
#: 17:04 两轮旁路穷尽恰好落在闸门 STOP 之后）。取名与 `health()` 导出键一致。
_LLM_FAULT_COUNTERS = (
    "outbound_empty_completion_count",
    "outbound_failure_count",
    "outbound_circuit_open_count",
)

#: 上游静默时「等一会儿再要一次」的默认预算（秒）；0 = 关闭（一键回滚点）。
#: 只在「本来就要出罐头话术」的路径上付出这段延迟，且只等一次。
#: 由 ``react.upstream_patience_sec`` 覆盖。
#:
#: 🔴 2026-09-25 按实测把默认值从 6.0 抬到 20.0 —— **6.0 被数据否掉了**：
#: 48h 内 4 次真实样本（18:37 / 22:21 / 23:25 / 00:03）**全部等满 6s 后重试仍空**，
#: 而静默实际持续 **8–20s**（23:25 那次直到 30s 熔断）。低于静默下限的预算
#: **结构上不可能成功**，那 6s 是纯延迟、零收益。现在取观测窗上沿（20s），
#: 代价仍有界：只在「本来就要出罐头话术」的路上、只等一次、且 ``0`` 可关。
#: ⚠️ 这是**按数据外推**的选择（尚无 20s 救回的成功样本），首个真实样本会给出答案。
_DEFAULT_UPSTREAM_PATIENCE_SEC = 20.0

#: 单次工具调用的墙钟上限（秒）；由 ``react.tool_timeout_sec`` 覆盖。
#:
#: 🔴 2026-10-03：旧值是**硬编码 120.0**，10-02 实测 `x_search` 单次耗 **102 秒**
#: 仍未触发 ⇒ 整回合被一个工具拖死，同群两条消息排队 125s/121s 后各用 49ms
#: 判 idle（用户白等 2 分钟、零回复）。收紧到 45s：覆盖内部**串行**最坏
#: （agent_tools browser `goto` 30s ＋ `click` 15s），同时砍掉 100s 级的极端。
_DEFAULT_TOOL_TIMEOUT_SEC = 45.0

#: 整回合 ReAct 墙钟预算（秒）；0 = 关闭（**默认**，不改变既有行为）。
#: 由 ``react.turn_deadline_sec`` 覆盖。
#:
#: 🔴 2026-10-03：单次工具上限（`tool_timeout_sec`）只挡住「**一个**工具卡死」，
#: 挡不住「**多轮**工具 + 多次 LLM 往返」的**累计**。实测 `x_search` 回合单次耗
#: 102s，期间**同群串行锁被持住**（`handler._group_turn_lock`）⇒ 同群其它用户排队、
#: 白等 2 分钟且零回复。给整回合一个硬上界后，超预算即「带已得工具结果收口作答」，
#: 单回合持锁时间不再无界。
#: 默认 0 = 关，与 `upstream_patience_sec` 同一策略：要启用请显式配置。
_DEFAULT_TURN_DEADLINE_SEC = 0.0

#: 时效脚注（P2-3）。2026-09-23 改文案：原句是
#: 「（以上来自联网检索；来源未标注具体日期，时效以抓取时间 {date} 为准）」——
#: 三项披露（来自网上 / 对方没写日期 / 参考日期）都在，但**逐字是审计口径**：
#: 分号、「来源未标注」、「以…为准」在群聊里一读就出戏，与 09-21 删 `_MOOD_TAIL`、
#: 09-23 去 meta 括号是同一类「机器痕迹」。改为口语转述，**披露内容一项不减**。
#: 硬约束（由 `tests/test_web_failover_freshness_p23.py` 守护）：
#: 必须含 `{date}`、必须含「喵」、必须含「网」、不得出现上述报告体词。
_FRESHNESS_FOOTER = "（刚在网上翻的喵，那边没写日期，就当是 {date} 看到的吧）"

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
                 freshness_footer: bool = True,
                 upstream_patience_sec: float = _DEFAULT_UPSTREAM_PATIENCE_SEC,
                 tool_timeout_sec: float = _DEFAULT_TOOL_TIMEOUT_SEC,
                 turn_deadline_sec: float = _DEFAULT_TURN_DEADLINE_SEC):
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
        # 上游静默时的挽救预算（秒）。0 = 关（改回「立刻出罐头话术」）。
        self.upstream_patience_sec = max(0.0, float(upstream_patience_sec))
        # 单次工具调用的墙钟上限（秒）。见 `_DEFAULT_TOOL_TIMEOUT_SEC`。
        self.tool_timeout_sec = max(1.0, float(tool_timeout_sec))
        # 整回合墙钟预算（秒）。0 = 关闭。见 `_DEFAULT_TURN_DEADLINE_SEC`。
        self.turn_deadline_sec = max(0.0, float(turn_deadline_sec))
        self._turn_start: float | None = None
        self._hit_turn_deadline = False
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

    def _llm_fault_counts(self) -> tuple[int, int, int] | None:
        """上游故障三项一手计数快照；**任一项读不到即返回 None**（fail-closed）。

        三项都是 llm 侧的**出站镜像计数**（只统计 `llm_scope(..., outbound=True)`
        的调用），不是进程级全量计数 —— 详见 `_LLM_FAULT_COUNTERS` 的说明。

        宁可退回旧的 `tool_trace` 分流，也不在没有确凿计数时宣称"上游故障" ——
        误报会把基础设施抖动说成模型的问题、也会把模型的问题说成抖动，两头都误导审计。
        """
        llm = getattr(self, "llm", None)
        if llm is None:
            return None
        values: list[int] = []
        for name in _LLM_FAULT_COUNTERS:
            value = getattr(llm, name, None)
            if not isinstance(value, int):
                return None
            values.append(value)
        return (values[0], values[1], values[2])

    def _upstream_went_empty(self) -> bool:
        """本轮是否发生过「HTTP 200 + 空补全」（网关静默降级）。"""
        baseline = getattr(self, "_llm_fault_baseline", None)
        current = self._llm_fault_counts()
        if baseline is None or current is None:
            return False
        return current[0] > baseline[0]

    def _upstream_went_silent(self) -> bool:
        """本轮上游是否**根本没给出任何东西** —— 200-空 / 传输故障 / 熔断，三者取并。

        🔴 依据是 llm 一手计数的**增量**，不是话术、也不是 `tool_trace` —— 后者与真实
        病因无关。2026-09-22 定案的归因错位就在这里：14:39 三个候选模型全 200-空，
        而 `tool_trace` 为空 ⇒ 这一条出站被洗成 `fallback_no_tool`「模型自己无话可说」。

        2026-09-23 补齐第二种病因：熔断打开时 `_post` 早退、连 200-空的计数都不增，
        14:55 那次的纯传输故障因此**同样**被洗成 `fallback_no_tool`。

        基线在 `run()` 开头取；`ReActLoop.__new__` 造出来的对象没有基线 ⇒ 返回 False，
        行为退回 `tool_trace` 分流（既有单测走的正是这条路）。

        🔴 2026-09-24 收窄：判据只认**出站**调用贡献的增量。此前读进程级全量计数，
        旁路调用（跨群提取／表情判定／反刍…）会推高同一批计数 ⇒ 与「本 turn 正文为空」
        同窗时，归因桶被翻成 `upstream_empty`（本该 `no_tool`）并白等一次预算。
        用户可见影响小，**失真的是遥测** —— 而分桶的意义就是让审计读得对。
        """
        baseline = getattr(self, "_llm_fault_baseline", None)
        current = self._llm_fault_counts()
        if baseline is None or current is None:
            return False
        return any(now > base for base, now in zip(baseline, current))

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

        分支顺序 = **真实病因优先**（2026-09-22）：上游静默时一律走通用池，
        不让 `tool_trace` 决定话术 —— 否则会给出「换个更具体的说法再来」这种
        与病因无关的引导，用户读到就是被甩锅。

        桶按病因**分开**（2026-09-23）：200-空 记 `upstream_empty`，
        传输故障/熔断 记 `upstream_error`。两者话术相同（对用户都是"本喵这边的岔子"），
        但审计必须分得清 —— 混成一桶等于没修归因。
        """
        if self._upstream_went_silent():
            self._record_fallback(
                "upstream_empty" if self._upstream_went_empty() else "upstream_error"
            )
            return random.choice(_NO_TOOL_FALLBACKS)
        if self.tool_trace and any(not item.get("ok") for item in self.tool_trace):
            self._record_fallback("tool_failed")
            return random.choice(_TOOL_FAILED_FALLBACKS)
        if self.tool_trace:
            # 工具都“成功”但模型仍空回复：仍要出口，避免用户感觉 bot 死机。
            # ⚠️ 措辞必须**场景无关**：这是通用出口，而此前写的是「换个具体视频链接
            # （带 BV）再丢一次」——那是 B 站专属要求，用户问天气/代码时收到这句就荒谬
            # （债 D-19）。场景专属的引导留在各自工具内（如 bilibili.py），不放进兜底。
            self._record_fallback("tool_ok")
            return random.choice(_TOOL_OK_FALLBACKS)
        self._record_fallback("no_tool")
        return random.choice(_NO_TOOL_FALLBACKS)

    def _record_fallback(self, kind: str) -> None:
        """兜底出站计数（fail-soft，与 `record_tool_spin` 同一套口径）。

        口径见 `ReplyQuality.record_fallback`。遥测绝不能影响回复路径，故一律吞异常 ——
        这里已经是「上游把自己说空了」的最后出口，再让计数抛异常会把兜底本身也摔掉。
        """
        try:
            if self.quality is not None:
                self.quality.record_fallback(kind)
        except Exception:
            logger.debug("兜底出站遥测记录失败", exc_info=True)

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
            with llm_scope("chat", "chat", outbound=True):
                c = await self.llm.chat(
                    messages, temperature=temperature, max_tokens=max_tokens
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ReAct 纯对话重试失败: %s", exc)
            return ""
        if isinstance(c, str) and c.strip():
            return c.strip()
        return ""

    def _turn_deadline_exceeded(self) -> bool:
        """整回合墙钟预算是否已用尽。0/未开跑 ⇒ False（fail-safe 到「不干预」）。

        判据只看单调钟，不猜；``_turn_start`` 由 ``run()`` 每次重置，
        所以实例可复用而不会把上一回合的耗时算进来。
        """
        deadline = float(getattr(self, "turn_deadline_sec", 0.0) or 0.0)
        if deadline <= 0.0:
            return False
        start = getattr(self, "_turn_start", None)
        if start is None:
            return False
        return (time.monotonic() - start) >= deadline

    def _patience_wait(self) -> float | None:
        """上游静默时「等一会儿再要一次」**值得**等多久；None = 不值得等，直接出兜底。

        四种情形（判据全部取自 llm 一手状态，不猜）：
          * **出站已连续两次整链失败** —— 窗口性故障，等不到头 ⇒ 返回 None（2026-10-03）；
          * 熔断未打开 —— 按预算等一次（09-25 实测静默持续 8–20s，故预算取 20s）；
          * 熔断已打开且剩余 ≤ 预算 —— 等到它**自然关闭**再试，这才是等的意义；
          * 熔断剩余 > 预算 —— 等不到头，白等 ⇒ 返回 None，立刻出罐头话术。

        🔴 2026-10-03 新增第一条：20s 预算的原假设是「瞬时抖动、等一会儿能救回」，
        但簇 A 实测推翻了它 —— 02:00:53 与 02:00:56 两次整链失败后仍等满 20s，
        第三次照样全空，**用户白等 20 秒后照样收兜底**。连续两次整链失败是
        「上游正处于故障窗」的强信号，此时等待零收益。只有**单次**失败保留等待。
        """
        budget = float(getattr(self, "upstream_patience_sec", 0.0) or 0.0)
        if budget <= 0:
            return None
        # 出站桶已连续失败 ≥2 次 ⇒ 窗口性故障，别再白等（判据必须看出站桶，
        # 本方法在 outbound scope 外，直接读 property 会拿到 background 桶）。
        if outbound_consecutive_failures(getattr(self, "llm", None)) >= 2:
            return None
        # 等待预算服务的是**出站正文** ⇒ 必须看出站桶。分桶后属性视图跟随调用 scope，
        # 而本方法在 outbound scope 之外被调用，直接读属性会把 background 桶的冷却
        # 误当作出站的剩余时间（2026-10-02）。
        until = outbound_circuit_open_until(getattr(self, "llm", None))
        try:
            remaining = float(until) - time.time()
        except (TypeError, ValueError):
            remaining = 0.0
        if remaining <= 0:
            return budget
        if remaining > budget:
            return None
        return remaining

    async def _salvage_before_fallback(self, msgs: list[dict], temperature: float,
                                       max_tokens: int) -> str:
        """出口前的最后一次挽救：上游静默时等一会儿再要一次正文。

        只在**本来就要出罐头话术**的路径上调用，所以这段延迟的期望成本极低；
        且只等一次、不做重试风暴（上游持续静默时早点给用户一个交代更好）。
        失败一律返回空串，绝不抛 —— 这里已经是最后一道出口。
        """
        if not self._upstream_went_silent():
            return ""
        wait = self._patience_wait()
        if wait is None:
            return ""
        logger.warning("上游静默（%s），%.1fs 后再要一次最终回复",
                       "200-空" if self._upstream_went_empty() else "传输故障/熔断", wait)
        try:
            await asyncio.sleep(wait)
            return await self._finalize_content(msgs, temperature, max_tokens)
        except Exception:  # noqa: BLE001
            logger.warning("上游静默后的挽救重试异常，直接出兜底", exc_info=True)
            return ""

    async def _finalize_or_fallback(self, msgs: list[dict], temperature: float,
                                    max_tokens: int) -> str:
        """两处出口（早退 / 触顶收尾）共用的收口：先挽救，仍空才出罐头话术。

        单一入口是刻意的 —— 分散在两处早晚会各自漂移（这正是 09-22 归因错位的成因）。
        """
        salvaged = await self._salvage_before_fallback(msgs, temperature, max_tokens)
        if not is_placeholder_reply(salvaged):
            logger.warning("上游静默后的挽救重试成功，出站 %d 字", len(salvaged))
            return self._apply_freshness_footer(salvaged)
        return self._empty_content_fallback()

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
        # 整回合墙钟预算计时起点（见 `_turn_deadline_exceeded`）；每回合重置。
        self._turn_start = time.monotonic()
        self._hit_turn_deadline = False
        # 本轮上游故障计数基线：兜底出口据此判定真实病因（2026-09-22，09-23 补齐传输/熔断）。
        self._llm_fault_baseline = self._llm_fault_counts()
        tool_schemas = self.tools.get_schemas(
            self.permission_level, sort_by_priority=self.priority_sort,
        ) if self.tools else []
        current_msgs = list(messages)  # 不修改原始列表
        prev_sig = None  # 反工具死循环：记录上一轮工具调用签名

        for round_idx in range(self.max_rounds):
            # 整回合墙钟预算：超预算则不再发起新一轮（含 LLM 与工具），带已得结果收口。
            if self._turn_deadline_exceeded():
                logger.warning(
                    "ReAct 整回合超时（>%.0fs，已完成 %d 轮），带已得工具结果收口",
                    self.turn_deadline_sec, round_idx,
                )
                self._hit_turn_deadline = True
                break
            # 询问 LLM（带已过滤的工具 schema）
            with llm_scope("chat", "react", outbound=True):
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
                    return await self._finalize_or_fallback(
                        messages, temperature, max_tokens
                    )
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
                # 同一轮内的多个工具：超预算后不再执行剩余工具（尽早收敛）。
                if self._turn_deadline_exceeded():
                    logger.warning(
                        "ReAct 整回合超时，跳过本轮剩余 %d 个工具", len(tool_calls) - i,
                    )
                    self._hit_turn_deadline = True
                    break
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
                        timeout=self.tool_timeout_sec,
                    )
                except asyncio.TimeoutError:
                    logger.warning("工具调用超时(%.0fs) name=%s", self.tool_timeout_sec, name)
                    result = (
                        f"工具执行超时（超过 {self.tool_timeout_sec:.0f} 秒），本次未获得结果"
                    )
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
                if name in _FRESHNESS_TOOLS and ok and _has_usable_content(result):
                    # 🔴 r27：还要**确实取到内容**才记账。
                    # 只说「工具跑成功了」不够 —— 「本视频没有字幕」也是成功，
                    # 但它没有任何可披露的检索结论；此时加脚注等于说假话
                    # （正文「抓不到内容」+ 脚注「刚在网上翻的」）。
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

        # 达到最大轮次 / 反死循环 / 整回合超时 → 最后问一次，无工具
        if not self._hit_tool_spin:
            if self._hit_turn_deadline:
                # 超时收口**不**记为触顶 —— 两者语义不同（触顶=轮次用尽；超时=时间用尽）。
                logger.warning(
                    "ReAct 整回合超时收口（%.0fs），基于已得工具结果作答",
                    self.turn_deadline_sec,
                )
            else:
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

        with llm_scope("chat", "react_finalize", outbound=True):
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
        return await self._finalize_or_fallback(current_msgs, temperature, max_tokens)
