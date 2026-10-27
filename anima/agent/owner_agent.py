"""Phase 3/4 整合层：OwnerAgent 串起规划器 / 执行器 / 存储 / 审计 / 通知。

供 handler 的 `!anima agent` 命令与 is_agent_task 自动路由调用。所有入口都要求
permission_level >= 4（真正的主人），普通用户无法触达。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from anima.agent.audit import AgentAuditor
from anima.agent.executor import AgentExecutor
from anima.agent.planner import TaskPlanner
from anima.agent.state import AgentTask, AgentTaskStore

logger = logging.getLogger("anima.agent.owner_agent")

#: 步骤结果短于此值视为空壳（"图片已生成""见上一步"），不单独交付。
_MIN_STEP_RESULT_CHARS = 8


def _split_text_chunks(text: str, limit: int) -> list[str]:
    """按**句子边界**把长文切成 ≤`limit` 的分片。

    为什么不硬切：QQ 上一条被切在半句话里，读起来就是"话没说完"
    （与 10-05 出站截断吸附句读是同一条纪律）。
    """
    text = str(text or "")
    limit = max(50, int(limit))
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            out.append(rest)
            break
        window = rest[:limit]
        cut = -1
        for i in range(len(window) - 1, max(0, int(limit * 0.5)) - 1, -1):
            if window[i] in "。！？!?…\n":
                cut = i + 1
                break
        if cut <= 0:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:]
    return out


class OwnerAgent:
    def __init__(self, core):
        self.core = core
        persistence = core.persistence
        data_dir = getattr(getattr(core.config, "persistence", None), "data_dir", "data")
        self.auditor = AgentAuditor(str(data_dir))
        # 让 agent_tools._audit 能直接找到 AgentAuditor（否则工具审计会落到 core.audit）
        core.agent_auditor = self.auditor
        self.store = AgentTaskStore(persistence)
        _cfg = getattr(core.config, "task_agent", None)
        self.executor = AgentExecutor(
            core, self.store,
            # 🔴 09-26：**不能**在构造期捕获 `core.llm`。生产装配顺序是
            # `anima_core.py` 先 `self.agent = OwnerAgent(self)`、**之后**才
            # `self.llm = None` / `cli.py` 里 `core.llm = llm`，此处
            # `getattr(core, "llm", None)` 必然拿到 None 且全仓无回填 ⇒ 规划器的
            # LLM 分支变成死代码（计划全部退化为关键词拼接）。真正的绑定改到
            # `AgentExecutor._bind_planner_llm()`（执行时惰性解析）。
            planner=TaskPlanner(None),
            auditor=self.auditor,
            notify=self._notify,
            verify_steps=bool(getattr(_cfg, "verify_steps", True)),
            max_replans=getattr(_cfg, "max_replans", None),
            parallel_steps=bool(getattr(_cfg, "parallel_steps", True)),
            max_parallel=getattr(_cfg, "max_parallel", None),
            # 🔴 2026-10-21 P0-1：整任务总预算（默认 300s）。此前无上限，
            # 理论最坏 20×180s=60min；自主触发（P1）前必须先有刹车。
            total_budget_sec=getattr(_cfg, "total_budget_sec", None),
        )

    async def _notify(self, task: AgentTask, message: str) -> None:
        gateway = getattr(self.core, "gateway", None)
        if not gateway:
            return
        try:
            from anima.llm.handler import OutboundMsg
        except Exception:
            return
        try:
            await gateway.send(OutboundMsg("private", int(task.creator), message[:1500]))
        except Exception:
            logger.exception("Agent 进度推送失败（已隔离）")

    async def notify_interrupted_on_startup(self) -> int:
        """启动后把「被重启打断」的任务私聊告知 owner（2026-10-21）。

        🔴 为什么需要：此前重启时在跑的 Agent 任务被静默判 `failed` ——
        用户只知道「我没收到结果」，不知道「它被重启打断了」。现在状态改
        `interrupted`（进度保留、可重跑），但仍需**主动说一声**，否则依然是
        个黑洞：任务躺在列表里没人知道。

        fail-closed：gateway 未就绪 / 无 interrupted / 发送失败 ⇒ 静默返回 0，
        绝不因通知问题影响启动。返回成功通知的条数。
        """
        ids = list(getattr(self.store, "interrupted_on_load", []) or [])
        if not ids:
            return 0
        gateway = getattr(self.core, "gateway", None)
        if not gateway:
            logger.info("[agent] 有 %d 个任务被重启打断，但 gateway 未就绪（下次再报）", len(ids))
            return 0
        try:
            from anima.llm.handler import OutboundMsg
        except Exception:
            return 0
        sent = 0
        for tid in ids:
            task = self.store.get(tid)
            if task is None:
                continue
            done = sum(1 for s in (task.steps or []) if getattr(s, "status", "") == "done")
            total = len(task.steps or [])
            _req = str(getattr(task, "request", "") or "").strip().replace("\n", " ")
            body = (
                f"本喵有个活干到一半被重启打断了喵。\n"
                f"任务：{tid}\n"
                f"内容：{_req[:80]}{'…' if len(_req) > 80 else ''}\n"
                f"进度：已完成 {done}/{total} 步（进度保留着，没有丢）\n"
                f"重跑：!anima agent approve {tid}"
            )
            try:
                await gateway.send(OutboundMsg("private", int(task.creator), body[:1500]))
                sent += 1
            except Exception:
                logger.warning("[agent] 重启打断通知失败 task=%s", tid, exc_info=True)
        if sent:
            self.auditor.emit("agent_tasks_interrupted", count=sent, task_ids=ids[:5])
            logger.warning("[agent] 已告知 owner %d 个任务被重启打断", sent)
        return sent

    async def _deliver_media(self, task: AgentTask) -> None:
        """把 Agent 任务产出的受控媒体（生图/改图）发到**请求所在会话**。

        09-26 生产事故：Agent 路径**没有任何图片交付通道** —— `generate_image` 只把
        路径写进 task-local call context，而该 key 的读者只有对话 pipeline 的
        generation 阶段与 `handler.build_reply_segments`；Agent 出站只走 `_notify`
        （纯文本 + 固定私聊）⇒ 887 KB 的图生成成功、落盘、从未发出，用户在群里什么
        都没收到。此处复用 `executor` 回收的路径显式交付。

        会话选择：建草稿时把 ``session``（detail_type/target_id）写进任务 context，
        交付回原会话；老任务没有该字段时退回私聊 creator（历史行为，不更差）。

        fail-open：单张失败只记日志，不改变任务状态（状态已由 executor 判定）。
        """
        paths = self.executor.live_media_paths(task)
        if not paths:
            return
        gateway = getattr(self.core, "gateway", None)
        if gateway is None:
            logger.warning("Agent 媒体交付跳过：gateway 未就绪（task=%s, %d 张）",
                           task.id, len(paths))
            return
        try:
            from anima.core.gateway import OutboundMsg
        except Exception:
            logger.exception("Agent 媒体交付失败：无法导入 OutboundMsg")
            return
        sess = task.context.get("session") or {}
        detail_type = str(sess.get("detail_type") or "private")
        try:
            target = int(sess.get("target_id") or task.creator)
        except (TypeError, ValueError):
            target = int(task.creator)
        delivered, missing = 0, 0
        for p in paths:
            try:
                await gateway.send(OutboundMsg(
                    detail_type, target, [{"type": "image", "data": {"file": p}}]))
                delivered += 1
            except Exception:
                missing += 1
                logger.exception("Agent 媒体单张交付失败（已隔离）: %s", p)
        if self.auditor:
            self.auditor.emit("agent_media_delivered", task_id=task.id,
                              delivered=delivered, failed=missing,
                              detail_type=detail_type, target=target)
        logger.info("Agent 媒体交付: task=%s 成功 %d / 失败 %d -> %s:%s",
                    task.id, delivered, missing, detail_type, target)

    async def submit(self, request: str, creator: int, *, auto_run: bool = True,
                     session: dict | None = None) -> AgentTask:
        """创建 Agent 任务；auto_run=True 时后台异步执行并返回任务句柄。"""
        task = self.store.create(request, creator, session=session)
        if auto_run:
            asyncio.create_task(self._run(task))
        return task

    async def _run(self, task: AgentTask) -> None:
        result_text = ""
        try:
            result_text = await self.executor.execute(task) or ""
        except Exception:
            logger.exception("Agent 任务执行异常")
            try:
                task.status = "failed"
                task.error = "执行代理发生未捕获异常"
                task.updated_at = time.time()
                self.store.save(task)
                await self._notify(task, "[失败] 执行代理发生未捕获异常")
            except Exception:
                pass
        finally:
            # 09-26 F2：无论成败，只要真的产出了媒体就交付（异常路径下也可能已有图）。
            try:
                await self._deliver_media(task)
            except Exception:
                logger.exception("Agent 媒体交付异常（已隔离）")
            # 🔴 D-36（2026-10-18）：**文本产物**也必须交付。
            # 这里曾只交媒体 ⇒ 纯文本任务零交付（生产实证见 `_deliver_text`）。
            try:
                await self._deliver_text(task, result_text)
            except Exception:
                logger.exception("Agent 文本交付异常（已隔离）")
            # 🔴 2026-10-23 P2：长期目标推进完成后**回写真实进展**。
            #   生产实测过只 mark_run 不 add_note 的后果：
            #   目标永远停在旧进展，下次派生又拿旧上下文去跑 ⇒「假装在推进」。
            #   而且我必须承认：第一次测试时目标里那条进展「群内讨论了 X 和 Y」
            #   是**我在测试脚本里编的占位符**，它忠实照做了 ⇒ 产出对不上真实话题。
            #   所以回写的必须是**任务真实产出**，不是我注入的假数据。
            try:
                await self._record_goal_progress(task, result_text)
            except Exception:
                logger.debug("Agent 目标进展回写异常（已隔离）", exc_info=True)

    #: 单条 QQ 消息的正文上限（`_notify` 用的是 1500，这里留余量）
    _TEXT_CHUNK = 1200
    #: 一次任务最多交付的总字数（超出截断，避免刷屏）
    _TEXT_TOTAL = 3600
    #: 群聊里的正文上限：长产物**不在群里刷屏**，群里只留摘要 + 指向私聊
    _TEXT_GROUP_MAX = 700
    #: 单个步骤结果最多交付的字数
    _TEXT_STEP_MAX = 1000

    #: 出站净化：Agent 正文走 `gateway.send` 直发、**不经过 pipeline**，
    #: 所以对话层的守卫必须在这里复用手动一遍。
    #:
    #: 🔴 绝不能「见长数字就打码」：生产正文里正常会出现
    #: 「积分从 29100 涨到 29600」「成本 120 元」「读了 16 条」这类数字，
    #: 一律打码会把**有效信息**毁掉（实测被
    #: `test_sanitize_keeps_normal_text` 抓住）。
    #:
    #: 判据分两档（宁可漏删，不可误删有效数字）：
    #:   ① 账号上下文里的数字（「QQ号是N」「账号：N」）⇒ 连词带数字一起删；
    #:   ② **裸的 QQ 号形态**（9–11 位、首位非 0、且不是明显金额/时间）
    #:      ⇒ 打码。真实 QQ 号是 5–11 位，这里取 9–11 位以减少误伤
    #:      （「29100」「120」这类 5–6 位金额/数量不碰）。
    _QQ_CONTEXT_RE = re.compile(
        r"(?:主人的?|用户的?|你的?)?\s*"
        r"(?:QQ\s*号?|帐号|账号|绑定(?:的)?(?:账号|号码)?)"
        r"\s*(?:是|为|：|:)?\s*\d{5,12}",
        re.I,
    )
    #: 裸 QQ 号：9–11 位数字，前后不能紧邻数字/小数点（排除金额、版本号）
    _BARE_QQ_RE = re.compile(r"(?<![\d.])\d{9,11}(?![\d.])")

    @classmethod
    def _sanitize_delivery(cls, text: str) -> str:
        """净化 Agent 交付正文（步骤脚手架 / 账号数字 / 元话语包裹）。

        ## 为什么需要它

        Agent 交付用 `gateway.send()` 直发，**绕过整条 pipeline** ⇒
        `guardrail` 的 QQ 号泄漏拦截在这条路径上完全不生效。

        生产实证（2026-10-27 11:44）用户收到：
            【步骤1】基于已有的零星话题和状态数据，直接构思一句简短的关怀话术
            主人的QQ号是1562290139。构思的关怀话术：「主人今天有好好吃饭嘛？…」

        人格正本明令「绝不报QQ号」。

        ## 为什么是净化而不是拒发

        交付正文被整条拒发 = 用户什么都没拿到（比发一条带噪的还差）。

        ## 保守原则

        **只删确定性噪声**（步骤标签、账号上下文里的数字），
        不碰任何其它数字/正文 —— 误删有效信息比漏删一句脚手架更糟。
        """
        text = str(text or "")
        if not text.strip():
            return ""
        # 1) 步骤脚手架行（`【步骤1】…` 整行删；行内前缀只删前缀）
        text = re.sub(r"(?m)^\s*【\s*步骤\s*\d+\s*】.*$", "", text)
        text = re.sub(r"【\s*步骤\s*\d+\s*】\s*", "", text)
        # 1b) **步骤元话语**：「当前步骤：<描述>\n\n执行：<真正的话>」。
        #
        # 生产实证（b2860d152c 步1，00:01 那条）：
        #     当前步骤：如实告知用户目前小鱼干库存为零，无法接受贿赂，并幽默回应其傲娇语气。
        #
        #     执行：哼，小鱼干库存为零，免谈！…
        #
        # `当前步骤：…` 是模型的**工作笔记**，`执行：…` 之后才是要说的话。
        # 只保留「执行：」后面的部分；没有「执行：」就把「当前步骤：」整行删掉。
        _exec_m = re.search(r"(?m)^\s*执行[：:]\s*", text)
        if _exec_m:
            text = text[_exec_m.end():]
        text = re.sub(r"(?m)^\s*当前步骤[：:].*$", "", text)
        # 1c) **写给下游的计划**：「下一步可直接将该图片…发送给用户」。
        #     这是它对「我接下来该干什么」的陈述，不是对用户说的话。
        #     生产实证（5c86a40cd0 步2，16:04）：
        #     「…爆笑梗图。下一步可直接将该图片及相应短句发送给用户。」
        #
        #     ⚠️ 必须**收窄**：`下一步` 本身是正常词（「这道题的下一步是求导」）。
        #     只删「下一步 + 情态词（可直接/可以/将/就/会/需要）+ 交付动作」
        #     这一种明确的计划句式 —— 实测放宽会把正常叙述整句删掉
        #     （被 `test_metatalk_cleanup_does_not_eat_real_content` 抓出）。
        text = re.sub(
            r"(?:下一步|接下来)\s*"
            r"(?:可直接|可以直接|可以|将|就|会|需要|要)\s*"
            r"[^。\n]*?"
            r"(?:发送|发|交付|回复|推送|告知|给出|输出)"
            r"[^。\n]*[。\n]?",
            "", text)
        # 2) 只在**账号上下文**里删数字（不做全局数字打码）
        text = cls._QQ_CONTEXT_RE.sub("", text)
        # 2b) 裸 QQ 号（9–11 位，前后不紧邻数字/小数点）⇒ 打码而非删除。
        #     这类多半是「发给 1562290139 就好」这种没有上下文词的形态；
        #     打码保留句子结构，读起来仍通顺。
        text = cls._BARE_QQ_RE.sub("***", text)
        # 3) 压掉净化后留下的空行堆
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # 4) 只剩标点/空白 ⇒ 视为无产物
        if not re.search(r"[\w\u4e00-\u9fff]", text):
            return ""
        return text

    @staticmethod
    def _goal_note_indicates_progress(body: str) -> bool:
        """本次推进是否**取得了实质进展**（用于进展标注，不用于改状态）。

        🔴 为什么不用长度阈值：实测
        `b8620a16c0`（没做成）total=203 字，
        `b96b79c59e`（真做成）total=248 字 —— **数值几乎一样，分不出来**。

        改用**结构化信号**：优先看有没有可验证的产出证据（写文件路径、
        明确的产出物），其次才看是否出现「无法/未能」这类失败陈述。

        取向：**宁可标成「有进展」也不误标「未取得实质进展」** ——
        因为误标会打击它的正确工作，而漏标只是少一个提示。
        真正需要 owner 注意的极端情况（完全没产出）已在上游被 body 为空拦掉。
        """
        t = str(body or "")
        if not t:
            return False
        import re as _re
        # 明确失败陈述（**优先判定**）。
        # 🔴 2026-10-23 实测踩过：文本含「无法完成…**建议**由群成员提供导出文件」，
        # 因为没命中旧的失败词表（只查「无法完成」不看上下文顺序），
        # 被判成「有进展」—— 而它其实什么都没做成。**建议往往是失败后的补救**，
        # 不是产出的证据。
        _fail_markers = (
            "无法完成", "未能成功", "未能导出", "无法导出", "不可用",
            "无法获取", "无法访问", "接口未能", "缺少", "无法读取", "失败",
        )
        _has_fail = any(m in t for m in _fail_markers)
        # 强硬证据：写出了文件 / 产出了结构化结果
        _has_output = bool(_re.search(
            r"(output/|\.md\b|\.txt\b|\.json\b|已写入|已归档|已生成|已保存)", t))
        # 失败陈述优先：只要出现失败词且**没有**明确的文件产出，就判未推进。
        # 这样「无法完成…建议…」不会被「建议」蒙过去。
        if _has_fail and not _has_output:
            return False
        return True

    async def _record_goal_progress(self, task: AgentTask, result_text: str = "") -> None:
        """把本次推进的**真实产出**回写到关联的长期目标（P2，2026-10-23）。

        🔴 为什么必须做（生产实测）：
        只 `mark_run()` 不 `add_note()` 的后果是 —— 目标永远停在旧进展，
        下次派生时 Agent 拿的还是旧上下文 ⇒「假装在推进」。

        🔴 为什么进展必须来自**任务真实产出**：
        第一次测试时我往 goal 里塞了一条自己编的进展
        （「群内讨论了 X 和 Y」），它忠实照做了，产出的 summary 与真实的
        structured 话题完全对不上。**它没编造，是我的测试数据污染了它。**
        所以这里只回写 step.result / result_text 的真实内容。

        fail-closed：无 goal_id / 读不到 goal / 任何异常 ⇒ 静默返回，
        绝不影响任务交付。
        """
        ctx = task.context if isinstance(task.context, dict) else {}
        goal_id = str(ctx.get("goal_id") or "")
        if not goal_id:
            return
        try:
            from anima.agent.goal import GoalStore
        except Exception:
            return
        persistence = getattr(self.core, "persistence", None)
        if persistence is None:
            return
        try:
            store = GoalStore(persistence)
            if store.get(goal_id) is None:
                return
            # 取**最后一条有实质内容的步骤结果**作为本次进展
            steps = sorted(getattr(task, "steps", []) or [],
                           key=lambda s: getattr(s, "index", 0))
            body = ""
            for st in reversed(steps):
                res = str(getattr(st, "result", "") or "").strip()
                if len(res) >= 20:
                    body = res
                    break
            if not body:
                body = str(result_text or "").strip()
            if not body:
                store.add_note(goal_id, f"[{task.status}] 本次推进无产出")
                return
            # 🔴 2026-10-23：**如实标注本次是否取得实质进展**。
            #   实测案例 b8620a16c0：拿不到群消息，重规划后只做了一步
            #   「如实说明无法完成」，执行器视角它"完成了所有步骤" ⇒ 判 completed。
            #   状态机没撒谎（它确实完成了自己规划的事），但**目标没被推进**。
            #   ⇒ 不改状态判定（靠文本猜「做没做成」必然误判，误判比不判更糟），
            #      而是在进展里**明确区分**这两种情况，让 owner 一眼看出。
            _progressed = self._goal_note_indicates_progress(body)
            _tag = "有进展" if _progressed else "未取得实质进展"
            note = f"[{task.status}·{_tag}] {body[:280]}".replace("\n", " ")
            store.add_note(goal_id, note)
            logger.info("[agent] 目标 %s 进展已回写（task=%s, %s, %d 字）",
                        goal_id, task.id, _tag, len(note))
        except Exception:
            logger.debug("目标进展回写失败（不影响任务）", exc_info=True)

    async def _deliver_text(self, task: AgentTask, result_text: str = "") -> None:
        """把 Agent 任务的**文本产物**发回请求所在会话（D-36）。

        🔴 09-26 的 `_deliver_media` 只解决了"图发不出去"；**纯文本任务依旧零交付**：
        `executor.execute()` 把产物当**返回值**返回（`executor.py:355-356`），
        `_run` 丢弃该返回值、`finally` 只调 `_deliver_media` ⇒ 用户收到
        「[完成] 任务已全部执行」却什么都没拿到，随后对话层**编造**
        「明明发过去了 / 被网络吞了」并要求用户重发（2026-10-18 生产实证 11:06–11:08，
        `agent_tasks.json::36bcd32ab6` 明明 completed、6 步全 done）。

        🔴 **交付"步骤结果"而不是 `result_parts`**：后者是
        `f"步骤{index+1}: {description} ✓"` 清单（`executor.py:308/317`），
        11-18 那次任务实测 **186 字全是标题**，发出去等于给用户一张待办列表。
        真正的产物在各步的 `step.result`（同任务合计 2020 字）。

        约束：单任务只交一次（`text_delivered` 落 `task.context`）、分片 ≤ `_TEXT_CHUNK`、
        总长 ≤ `_TEXT_TOTAL`、失败任务不交（失败已有 `[失败]` 通知）。
        """
        if getattr(task, "status", "") != "completed":
            return
        ctx = task.context if isinstance(task.context, dict) else {}
        if ctx.get("text_delivered"):
            return

        steps = sorted(getattr(task, "steps", []) or [], key=lambda s: getattr(s, "index", 0))
        # 🔴 2026-10-27：**步骤脚手架不再进出站**。用户要的是结果，不是过程。
        #
        # 前两轮都在治标，方向是错的：
        #
        #   2026-10-20（11a531c）：交付步骤标题，认为「标题用于溯源」。
        #     ⇒ 用户开始收到 `【步骤1】…【步骤2】…` 的工程日志。
        #   2026-10-25（00ecb04）：只删「内容重复的步骤」，理由写的是
        #     「步骤标题不能删，它是溯源锚点」。
        #     ⇒ 治的是「同一个意思说三遍」，**没治「为什么要给用户看步骤」**。
        #
        # 2026-10-27 生产实证（owner 直接质问「为什么要把步骤给我看」）：
        #
        #     【步骤1】基于已有的零星话题和状态数据，直接构思一句简短、轻松且
        #             带有生活气息的关怀话术
        #     主人的QQ号是1562290139。构思的关怀话术：「主人今天有好好吃饭嘛？…」
        #
        #     【步骤2】向主人发送这条主动问候消息（1562290139）
        #     主人今天有好好吃饭嘛？别太辛苦啦，记得揉揉猫猫～
        #
        # 用户看到的是一条**能用的问候**，被埋在「构思过程 + 内部 QQ 号 + 元话语」
        # 里。这不是"溯源"问题，是**把推理草稿当交付物**。
        #
        # ⇒ 正确做法：
        #   · 出站只给**最终产物**（末步 result，或显式 result_text）；
        #   · 步骤标题/全部步骤结果**转进 `chat_memory` 供溯源**（
        #     由调用方 `_record_delivery_note` 负责，不进用户可见文本）；
        #   · 多步任务在**结果确有多个不同产物**时才合并多条，
        #     且用自然分隔而不是 `【步骤N】` 标签。
        #
        # 「溯源」是内部需求，不该让用户替我们承担这个成本。
        _kept_texts: list[str] = []

        def _similar(a: str, b: str) -> bool:
            """粗判两段结果是否在说同一件事（字符二元组 Jaccard）。

            阈值 **0.85**（刻意高于项目其它主题去重用的 0.55）：
            这里要合并的是「**同一个意思原样说了三遍**」（生产实证：三步都是
            「主人最近在忙什么…注意休息」，几乎逐字重合），不是「主题相近」。
            用 0.55 会把「第一段内容用于测试正文」和「第二段…」这种只差两个字的
            模板文本误合并（实测被测试抓出：三步只剩一步，分片能力丢失）。
            ⇒ 宁可少合并，也不要吞掉真实的多步产物。
            """
            _clean = lambda s: "".join(ch for ch in s if ch.isalnum() or ch.isspace())
            sa, sb = _clean(a), _clean(b)
            if not sa or not sb:
                return False
            ga = {sa[i:i + 2] for i in range(len(sa) - 1)} or {sa}
            gb = {sb[i:i + 2] for i in range(len(sb) - 1)} or {sb}
            if not ga or not gb:
                return False
            return len(ga & gb) / len(ga | gb) >= 0.85

        def _contains_or_similar(a: str, b: str) -> bool:
            """`a` 是 `b` 的包裹版（含它 / 被它含）也算重复。

            🔴 2026-10-27：单纯的 Jaccard ≥0.85 **抓不住真实事故**。
            生产实证（8e6c68ab74）：

                步1: 主人的QQ号是1562290139。构思的关怀话术：「主人今天有好好吃饭嘛？…」
                步2: 主人今天有好好吃饭嘛？别太辛苦啦，记得揉揉猫猫～

            步2 整句被**完整包含**在步1 里，但步1 多了一段前缀，Jaccard 只有
            约 0.6 ⇒ 没被合并 ⇒ 用户收到「构思过程 + 定稿」两段，
            同一句话出现两次。

            判据：去噪后一方是另一方的子串且较长者不超其 3 倍 ⇒ 视为同一件事
            （保留**更短/更干净**的那条，即后出现的定稿）。

            倍数取 3 而非 2：真实事故里步1=45 字、步2=21 字，比值 2.14 ——
            2 倍刚好卡住（实测被本测试抓出）。而 3 倍仍远小于「一篇长文里
            顺手提到同一句话」的比值，不会误合并。
            """
            if _similar(a, b):
                return True
            _clean = lambda s: "".join(ch for ch in s if ch.isalnum())
            ca, cb = _clean(a), _clean(b)
            if not ca or not cb:
                return False
            short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
            # 太短的不做包含判定（「好的」会被任何句子包含）
            if len(short) < 10:
                return False
            return short in long_ and len(long_) <= len(short) * 3

        _rows: list[tuple[str, str]] = []
        for st in steps:
            res = str(getattr(st, "result", "") or "").strip()
            # 太短的是"已生成图片 / 见上一步"这类空壳，不值得单独发。
            if len(res) < _MIN_STEP_RESULT_CHARS:
                continue
            _rows.append((str(getattr(st, "description", "") or ""), res))

        # 反序去重：从最后一步往前，保留与"已保留集合"都不重复的步骤
        _keep_idx: list[int] = []
        for i in range(len(_rows) - 1, -1, -1):
            _desc, _text = _rows[i]
            if any(_contains_or_similar(_text, _rows[j][1]) for j in _keep_idx):
                logger.info(
                    "文本交付去重：步骤「%s」与后续步骤内容重复，已省略",
                    _desc[:24] or f"#{i + 1}",
                )
                continue
            _keep_idx.append(i)
        _keep_idx.sort()

        # 出站正文：**只给产物，不给步骤标签**。
        # 多步时按自然空行拼接（正文本身已是一句话就不加任何分隔）。
        _bodies = [res[:self._TEXT_STEP_MAX] for _desc, res in
                   (_rows[i] for i in _keep_idx)]
        # 单步任务最常见（「关心一下主人」这种）⇒ 直接就是那一句，不加分隔
        body = "\n\n".join(_bodies).strip() if len(_bodies) > 1 else "".join(_bodies).strip()

        # `result_text` 是 executor 的显式产物；有它时优先于步骤拼装
        # （步骤 result 可能是中间稿，`result_text` 是定稿）。
        _explicit = str(result_text or "").strip()
        if _explicit and len(_explicit) >= len(body):
            body = _explicit

        if not body:
            logger.info("Agent 文本交付跳过：无产物 task=%s", task.id)
            return

        truncated = len(body) > self._TEXT_TOTAL
        body = body[:self._TEXT_TOTAL]
        if truncated:
            body = body.rstrip() + "\n…（内容较长已截断）"

        gateway = getattr(self.core, "gateway", None)
        if gateway is None:
            logger.warning("Agent 文本交付跳过：gateway 未就绪（task=%s）", task.id)
            return
        try:
            from anima.core.gateway import OutboundMsg
        except Exception:
            logger.exception("Agent 文本交付失败：无法导入 OutboundMsg")
            return

        sess = ctx.get("session") or {}
        detail_type = str(sess.get("detail_type") or "private")
        try:
            target = int(sess.get("target_id") or task.creator)
        except (TypeError, ValueError):
            target = int(task.creator)
        creator = int(task.creator)

        # 🔴 群聊防刷屏：长产物**不在群里连发**。群里留一段（≤ `_TEXT_GROUP_MAX`）+
        # 一句指引，完整版走 creator 私聊 —— 既不丢内容，也不把群当文件柜。
        # （2026-10-18 回放：本例 body 2217 字，原样进群就是 2 连发。）
        plans: list[tuple[str, int, str]] = []
        if detail_type == "group" and len(body) > self._TEXT_GROUP_MAX:
            # 群里那条要放**最后一段实质内容**（通常是最终产物），而不是把
            # 「步骤1 语料分析」这半截截给群里看（10-18 回放：原样取 body[:700]
            # 群里只看到语料清单，看不到 16 句脚本）。
            # 2026-10-27：`blocks` 已不再含步骤标签，取最后一段 ≥200 字的产物；
            # 都不够长时退回 body 尾部。
            _tail_pick = next(
                (b for b in reversed(_bodies) if len(b) >= 200),
                (_bodies[-1] if _bodies else body),
            )
            head = _tail_pick[:self._TEXT_GROUP_MAX].rstrip() + "\n…（完整结果已私聊给你）"
            plans.append(("group", target, head))
            for c in _split_text_chunks(body, self._TEXT_CHUNK):
                plans.append(("private", creator, c))
        else:
            for c in _split_text_chunks(body, self._TEXT_CHUNK):
                plans.append((detail_type, target, c))

        # 🔴 2026-10-27：**出站前过一遍对话层的守卫**。
        #
        # Agent 交付走 `gateway.send` 直发，**不经过 pipeline** ⇒ 对话层所有
        # 出站守卫（QQ 号泄漏 / 元话语 / 编造断言）在这条路径上全部失效。
        #
        # 生产实证（10-27 11:44）用户收到：
        #     【步骤1】…构思一句简短、轻松且带有生活气息的关怀话术
        #     主人的QQ号是1562290139。构思的关怀话术：「主人今天有好好吃饭嘛？…」
        #
        # 人格正本明令「绝不报QQ号」，`guardrail._detect_prompt_leak` 也**能**
        # 识别它（实测 `account_digits:156***`），但它根本没被调用。
        #
        # ⇒ 在同一处补下游净化。用 `strip`-类纯函数而非 `SKIP_REPLY`：
        #   交付正文被整条拒发等于用户什么都没拿到，那是更坏的结果。
        _sanitized: list[tuple[str, int, str]] = []
        for _dt, _tid, _chunk in plans:
            _clean = self._sanitize_delivery(_chunk)
            if _clean:
                _sanitized.append((_dt, _tid, _clean))
        plans = _sanitized
        if not plans:
            logger.warning("Agent 文本交付净化后为空，跳过 task=%s", task.id)
            return

        sent = 0
        for dt, tid, chunk in plans:
            try:
                await gateway.send(OutboundMsg(dt, tid, chunk))
                sent += 1
            except Exception:
                logger.exception("Agent 文本分片发送失败（已隔离）task=%s", task.id)
                break

        ctx["text_delivered"] = sent > 0
        ctx["text_delivered_chars"] = len(body)
        ctx["text_delivered_parts"] = sent
        task.context = ctx
        self.store.save(task)

        if self.auditor:
            try:
                self.auditor.emit("agent_text_delivered", task_id=task.id,
                                  parts=sent, chars=len(body),
                                  detail_type=detail_type, target=target)
            except Exception:
                pass
        logger.info("Agent 文本交付: task=%s %d 片 / %d 字 -> %s:%s",
                    task.id, sent, len(body), detail_type, target)

        # 🔴 D-36b：把"刚刚交付了什么"写进会话记忆。
        # 任务通知走 `gateway.send` 直发、**不进 chat_memory** ⇒ 对话层对任务结果
        # 完全无知，用户问"东西呢"时只能编。这里留一条**摘要**（不写全文，
        # 免得污染上下文）：既让下一轮知道"我交过什么"，也不撑大记忆。
        #
        # 🔴 2026-10-26：**包裹语本身是错的**（上线次日就出问题）。
        #
        # 生产实证（10-26 00:01:06，私聊）：实际发出去的那条，开头是一句
        # 我拼的"交付告知语"，后面接的是模型在任务里随口抱怨的一句话。
        # 见 `tests/test_agent_delivery_meta.py::test_fake_prose_wrapper_is_gone`
        # （那里断言该句式不得再出现；此处刻意不复述原句，避免被误判为代码里还在用）。
        #
        # 两个错误叠在一起：
        #  1. `_delivery_gist` 抽到的那句话根本**不是产物**，是模型在任务里
        #     随口抱怨的一句话 —— 「实质产物首句」这个假设不成立：
        #     步骤结果里混杂着思考、抱怨、寒暄，机械抽「末段首句」抽不准。
        #  2. **那句包裹语把它包装成了正文发给用户**。旧格式
        #     「（Agent 任务 xxx 已完成并交付 275 字：…）」虽然难看，但一眼是
        #     内部消息；加一句人话包裹反而**冒充成了正常说话**。
        #
        # ⇒ 结论：交付通知**不该由本文件拼任何自然语言**。
        #   用户已经通过 `gateway.send` 收到了真正的正文（上面 `sent` 片数）。
        #   这里只需要给**对话层**留一条可回溯的、明显是元信息的记录，
        #   让人（和模型自己）都知道"我干过一件事"，而不冒充正文。
        #
        #   格式：`[已交付 <任务短id>] <gist 或 状态>`
        #     · 带方括号前缀 ⇒ 一眼是元信息，模型不会当自己的话延续
        #       （`chat_memory._is_bot_meta_line` 会在注入时跳过它）
        #     · 任务 id 只留 8 位（原来 10 位，够溯源又不占位）
        #     · 不带字数/步骤号
        try:
            from anima.memory.chat_memory import get_chat_memory
            _gist = self._delivery_gist(body)
            note = "[已交付 %s] %s" % (
                str(task.id)[:8], _gist or "（正文已发）")
            get_chat_memory().add(
                detail_type, target, "bot", "Anima", note,
                user_id=(int(task.creator) if detail_type == "group" else None),
            )
        except Exception:
            logger.debug("Agent 交付回写会话记忆失败（已隔离）", exc_info=True)

    #: 交付摘要里最多留多少字（进 prompt 的记忆条目，越短越好）
    _GIST_MAX_CHARS = 60

    #: 步骤脚手架标记：`【步骤1】` / `【步骤 2】` / `步骤3:` / `Step 1:`
    _STEP_SCAFFOLD_RE = re.compile(
        r"(?:【\s*步骤\s*\d+\s*】|步骤\s*\d+\s*[:：]|Step\s*\d+\s*[:：])"
    )

    @classmethod
    def _delivery_gist(cls, body: str) -> str:
        """从交付正文里取一句「实质内容摘要」，剔掉步骤脚手架。

        - 逐行去掉 `【步骤N】…` 这种标题行；
        - 取**最后一段**实质文字的首句（末段通常是定稿，不是中间产物）；
        - 压掉换行/多余空白，超长截断。

        返回空串表示「没有可提炼的实质内容」（调用方就不再写记忆）。
        """
        text = str(body or "")
        if not text.strip():
            return ""
        # 去掉行内的步骤标题前缀（标题与正文可能同行，如「【步骤1】分析… \\n 正文」）
        cleaned_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if cls._STEP_SCAFFOLD_RE.fullmatch(stripped):
                continue
            stripped = cls._STEP_SCAFFOLD_RE.sub("", stripped).strip()
            # 「【步骤1】分析主人最近的回复内容」这类**纯标题**（短且以动词开头）
            # 无法可靠区分，一律保留 —— 但整体长度受 `_GIST_MAX_CHARS` 限制。
            if stripped:
                cleaned_lines.append(stripped)
        if not cleaned_lines:
            return ""
        # 末段优先（定稿），但末段过短（<8 字，如「完成了」）时回退到最长段
        tail = cleaned_lines[-1]
        pick = tail if len(tail) >= 8 else max(cleaned_lines, key=len)
        pick = re.sub(r"\s+", " ", pick).strip()
        if len(pick) > cls._GIST_MAX_CHARS:
            cut = pick[:cls._GIST_MAX_CHARS]
            for sep in ("。", "！", "？", "，", "；", ".", "!", "?"):
                idx = cut.rfind(sep)
                if idx >= cls._GIST_MAX_CHARS // 2:
                    cut = cut[:idx]
                    break
            pick = cut.rstrip("，、；,; ") + "…"
        return pick

    async def approve(self, task_id: str) -> str:
        task = self.store.get(task_id)
        if task is None:
            return "未找到 Agent 任务"
        if task.status == "executing":
            return f"任务 {task_id} 正在执行中"
        # 清除可能的历史取消标志/状态，允许重新执行
        self.executor._cancel.pop(task_id, None)
        try:
            self.store.set_status(task_id, "planning")
        except ValueError:
            pass
        asyncio.create_task(self._run(task))
        return f"已启动 Agent 任务 {task_id}"

    def cancel(self, task_id: str) -> str:
        self.executor.request_cancel(task_id)
        try:
            self.store.set_status(task_id, "cancelled", "主人取消")
        except ValueError:
            pass
        return f"已请求取消 Agent 任务 {task_id}"

    def status_text(self, limit: int = 10) -> str:
        items = self.store.list()[:limit]
        if not items:
            return "暂无 Agent 任务"
        return "\n".join(f"{t.id} [{t.status}] {t.request[:60]}" for t in items)
