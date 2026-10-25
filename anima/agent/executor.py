"""Phase 4 Agent 执行循环 —— 多步 Plan→Execute→Verify→Replan。

设计（与审计 B1 一致，复用现有 ReAct 与 ToolRegistry，不重复实现工具执行）：
- 每步若已指定 tool_name → 直接以主人权限调用 core.tools.call（带重试）。
- 每步若未指定 tool_name → 由 ReActLoop（permission_level=4）自行决定工具。
- P2-1 独立验收：步骤执行成功后，若该步有 ``verify`` 判据，由 LLM 判定成立与否；
  不成立按失败处理（而不是只看工具有没有抛错）。
- P2-1 重规划：步骤失败或验收不通过时，让 planner 产出替代步骤替换剩余计划，
  上限 MAX_REPLANS（默认 2）防止无界重试。
- P2-1 并行：planner 标记了同一 ``parallel_group`` 且都有显式 tool_name 的连续步骤
  可并发执行（同一权限域内，仍受统一资源预算约束）。
- 逐步持久化（AgentTaskStore.save），进程重启可从 current_step 续跑。
- 资源预算（磁盘/网络/时长/输出）由 guardrails.ResourceBudget 把关。
- 进度通过 notify 回调推送（QQ 私聊给主人 / WebUI）。
- 熔断：连续 3 个任务失败 → 暂停 Agent 能力 10 分钟。
- 紧急停止：request_cancel 置位后，循环在步边界安全退出。

设计取舍（fail-open 处均写审计，避免「验收器自己坏掉」变成任务杀手）：
- 验收与重规划都依赖 LLM；无 LLM 时二者自动跳过（确定性兜底的「换方式重做」
  步骤没有 LLM 也无法执行，只会把一次失败拖成多次失败）。
- 验收回答无法判定（既非 PASS 也非 FAIL）或验收超时 → 视为通过并写
  ``agent_verify_inconclusive`` 审计，不因判据模糊而毁掉一个已成功的任务。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from anima.agent.audit import AgentAuditor
from anima.agent.guardrails import ResourceBudget, ResourceExceeded
from anima.agent.planner import TaskPlanner
from anima.agent.state import AgentTask, Step
from anima.llm.usage import llm_scope
from anima.tools import LEVEL_OWNER, tool_result_ok

logger = logging.getLogger("anima.agent.executor")


class StepFailed(Exception):
    """单步失败（重试耗尽）。"""


class AgentExecutor:
    MAX_STEPS = 20
    MAX_TOOL_RETRIES = 3
    STEP_TIMEOUT = 180
    MAX_REPLANS = 2
    MAX_PARALLEL = 4
    VERIFY_TIMEOUT = 60
    VERIFY_OUTPUT_CHARS = 1200
    #: 整任务总预算默认值（秒）。🔴 2026-10-21 P0-1：此前**完全没有**整任务上限，
    #: 只有单步 180s + MAX_STEPS=20 ⇒ 理论最坏 60 分钟。生产实证 6 次任务实测
    #: 30–70s，取 300s 留 4 倍余量。可用 `task_agent.total_budget_sec` 覆盖。
    DEFAULT_TOTAL_BUDGET_SEC = 300.0
    # 09-26：产出「受控媒体」（图片）的工具。这些工具把产物路径写进 task-local
    # call context（`generated_image_paths`），而 Agent 路径**不经过**对话 pipeline
    # 的出站段构造 ⇒ 必须由本执行器显式回收，否则产物变孤儿文件。
    MEDIA_TOOLS = frozenset({"generate_image", "edit_image"})
    # context 中记录「本任务调用过哪些工具」与「已回收的媒体路径」的键。
    CTX_TOOLS_USED = "media_tools_used"
    CTX_MEDIA_PATHS = "generated_image_paths"

    def __init__(self, core, store, planner: TaskPlanner | None = None,
                 auditor: AgentAuditor | None = None,
                 notify: Optional[Callable[[AgentTask, str], Any]] = None,
                 *, verify_steps: bool = True, max_replans: int | None = None,
                 parallel_steps: bool = True, max_parallel: int | None = None,
                 total_budget_sec: float | None = None):
        self.core = core
        self.store = store
        self.planner = planner or TaskPlanner(getattr(core, "llm", None))
        self.auditor = auditor
        self.notify = notify
        self.verify_steps = bool(verify_steps)
        self.max_replans = self.MAX_REPLANS if max_replans is None else max(0, int(max_replans))
        self.parallel_steps = bool(parallel_steps)
        self.max_parallel = self.MAX_PARALLEL if max_parallel is None else max(1, int(max_parallel))
        #: 整任务总预算（秒）。0/None ⇒ 不限制。
        #: 🔴 2026-10-21 P0-1：此前只有单步超时、无整任务上限（理论最坏 20×180s）。
        #: 自主触发（P1）一旦上线，无上限 = 失控放大器，故先装刹车再给油门。
        self.total_budget_sec = float(
            self.DEFAULT_TOTAL_BUDGET_SEC if total_budget_sec is None else total_budget_sec)
        self._cancel: dict[str, bool] = {}
        self._fail_streak = 0
        self._pause_until = 0.0

    # ------------------------------------------------------------------
    # P0（09-26）：规划器 LLM 惰性绑定
    # ------------------------------------------------------------------

    def _bind_planner_llm(self) -> None:
        """在**执行时**把 ``core.llm`` 注入 planner（幂等）。

        生产装配顺序是 `anima_core.py` 先构造 ``OwnerAgent``（其 ``__init__`` 里
        构造本执行器），**之后**才给 ``core.llm`` 赋值。于是构造期的
        ``getattr(core, "llm", None)`` 必然拿到 ``None``，并且**全仓没有任何回填**
        ⇒ ``planner.llm`` 永久为 None ⇒ ``planner._decompose`` 的 LLM 分支变成
        死代码，所有 Agent 计划退化为关键词拼接（09-26 生产首跑实证：规划仅耗时
        50 ms，且三步与 ``_fallback`` 输出逐字相同）。

        单测之所以全绿，是因为测试直接 ``TaskPlanner(llm)`` 注入 —— 夹具构造方式
        与生产装配方式不同（假绿）。这里改为惰性解析后，两条路径都对。
        """
        planner = self.planner
        if planner is None or getattr(planner, "llm", None) is not None:
            return
        llm = getattr(self.core, "llm", None)
        if llm is not None:
            planner.llm = llm

    # ------------------------------------------------------------------
    # P0（09-26）：受控媒体回收与交付自检
    # ------------------------------------------------------------------

    #: 🔴 2026-10-25：**「声称做了」的措辞**（用于编造检测）。
    #:
    #: 生产实证（10-24 14:06）：某步输出
    #: 「本喵已经帮你抽好今日运势和猪猪啦，快来看看你今天能抽到什么好运喵~」
    #: 而该步 **`tools_called` 为空** —— 它什么都没调用，纯属编造。
    #: 更糟的是**前一步刚说**「并未有明确指定具体的抽卡任务」，自相矛盾。
    #:
    #: 设计取向：只匹配**明确的完成态断言**（已经…了 / 已为你…），
    #: 不匹配「我查一下」「让我看看」这类**意图表述** —— 后者不算编造。
    _FAKE_ACTION_RE = re.compile(
        r"(?:已经|已)(?:帮你|为你|给|替)?[^。！？\n]{0,12}?(?:抽好|抽完|查好|查到|查到|生成|做好|办好|发送|发出|发出|发好|记录好)"
        r"|已经帮你[^。！？\n]{0,10}?(?:了|好啦|完了)"
        r"|(?:帮你|为你)(?:抽|查|整理|生成|做好)好了"
    )

    #: 「这步本该动工具」的措辞（步骤描述侧）。
    _ACTION_STEP_HINT_RE = re.compile(
        r"(?:发送|发出|推送|抽卡|抽取|查询|检索|抓取|下载|上传|写入|写文件|记录|执行|调用)"
    )

    def _looks_like_fake_action(self, step: Step) -> bool:
        """该步是否**声称做了操作、却没有任何工具调用**。

        判据（必须同时满足，宁少勿错）：
          1. 该步 `tools_called` 为空（真没动工具）；
          2. 该步**描述**看起来该动工具（发送/查询/抽卡/写入…）；
          3. 该步**结果**用了完成态断言（「已经帮你抽好了」）。

        ⇒ 三者齐备才判可疑。任何一项不满足都不拦 —— 正常纯文本步骤
        （如「整理并汇报」）不该被误判。
        """
        try:
            if getattr(step, "tools_called", None):
                return False
            desc = str(getattr(step, "description", "") or "")
            body = str(getattr(step, "result", "") or "")
            if not desc or not body:
                return False
            if not self._ACTION_STEP_HINT_RE.search(desc):
                return False
            return bool(self._FAKE_ACTION_RE.search(body))
        except Exception:
            logger.debug("编造检测异常（按不拦处理）", exc_info=True)
            return False

    def _record_tools(self, task: AgentTask, names: list[Any]) -> None:
        """记录本任务真正调用过的工具名（供媒体交付自检使用）。"""
        if not names:
            return
        used = task.context.get(self.CTX_TOOLS_USED)
        if not isinstance(used, list):
            used = []
            task.context[self.CTX_TOOLS_USED] = used
        seen = {str(x) for x in used}
        for n in names:
            s = str(n or "")
            if s and s not in seen:
                seen.add(s)
                used.append(s)

    def _harvest_media(self, task: AgentTask, ctx: Any) -> None:
        """把本步工具产出的媒体路径从 call context 收回到任务上下文（去重保序）。

        工具契约（``tools/builtin/owner.py``）：生图/改图把绝对路径 append 进
        ``get_call_context()["generated_image_paths"]``，交给出站层发送。但该 key 的
        读者**只有** ``pipeline/stages/generation.py`` 与
        ``llm/handler.py:build_reply_segments`` —— 都在**正常对话管线**上；Agent
        路径零读者，且 ``set_call_context`` 的作用域一旦 reset，路径就永久丢失。
        """
        if not isinstance(ctx, dict):
            return
        got = ctx.get(self.CTX_MEDIA_PATHS)
        if not isinstance(got, list) or not got:
            return
        bucket = task.context.get(self.CTX_MEDIA_PATHS)
        if not isinstance(bucket, list):
            bucket = []
            task.context[self.CTX_MEDIA_PATHS] = bucket
        seen = {str(x) for x in bucket}
        for p in got:
            s = str(p or "")
            if s and s not in seen:
                seen.add(s)
                bucket.append(s)

    def media_tools_used(self, task: AgentTask) -> set[str]:
        """本任务调用过的、属于「媒体产出」类的工具名集合。"""
        used = {str(x) for x in (task.context.get(self.CTX_TOOLS_USED) or [])}
        return used & self.MEDIA_TOOLS

    def live_media_paths(self, task: AgentTask) -> list[str]:
        """已回收且**当前仍在磁盘上**的媒体路径（失效路径不算交付）。"""
        out: list[str] = []
        for p in (task.context.get(self.CTX_MEDIA_PATHS) or []):
            s = str(p or "")
            if not s:
                continue
            try:
                if Path(s).is_file():
                    out.append(s)
            except OSError:
                continue
        return out

    # ------------------------------------------------------------------
    # 紧急停止
    # ------------------------------------------------------------------

    def request_cancel(self, task_id: str) -> None:
        self._cancel[task_id] = True

    def _is_cancelled(self, task: AgentTask) -> bool:
        if self._cancel.get(task.id):
            return True
        t = self.store.get(task.id)
        return t is not None and t.status == "cancelled"

    # ------------------------------------------------------------------
    # 进度通知
    # ------------------------------------------------------------------

    async def _notify(self, task: AgentTask, message: str) -> None:
        if self.auditor:
            self.auditor.emit("agent_progress", task_id=task.id,
                              step=task.current_step, message=message[:200])
        if self.notify:
            try:
                await self.notify(task, message)
            except Exception:
                logger.exception("Agent 进度通知失败（已隔离）")

    # ------------------------------------------------------------------
    # 主执行
    # ------------------------------------------------------------------

    async def execute(self, task: AgentTask) -> str:
        if time.time() < self._pause_until:
            msg = "Agent 能力已熔断暂停（连续失败过多），请 10 分钟后再试。"
            await self._notify(task, msg)
            return msg

        # 09-26 P0：规划/重规划都依赖 planner.llm，而它在生产装配顺序下必为 None
        # ⇒ 每次执行前惰性绑定（幂等，绑定后不再解析）。
        self._bind_planner_llm()

        # 尚未规划则先规划
        if not task.steps:
            task.steps = await self.planner.plan(task.request, task)

        task.status = "executing"
        task.error = ""
        self.store.save(task)

        budget = ResourceBudget()
        result_parts: list[str] = list(task.context.get("result_parts") or [])
        failed = False
        executed = 0
        idx = task.current_step
        #: 整任务预算起点。🔴 2026-10-21 P0-1：每步都检查，超预算即中止，
        #: **已完成步骤的产物保留**（不因超时丢掉已产出的东西）。
        _task_started = time.monotonic()

        def _over_budget() -> bool:
            if self.total_budget_sec <= 0:
                return False
            return (time.monotonic() - _task_started) > self.total_budget_sec

        while idx < len(task.steps) and executed < self.MAX_STEPS:
            if self._is_cancelled(task):
                task.status = "cancelled"
                self.store.save(task)
                await self._notify(task, "[已取消] 主人终止了任务")
                return "任务已取消。"

            # 🔴 P0-1 整任务预算检查（放在每步开头，粒度足够且不打断在跑的步骤）
            if _over_budget():
                _elapsed = time.monotonic() - _task_started
                _done = sum(1 for s in task.steps if s.status == "done")
                task.status = "failed"
                task.error = (
                    f"超出整任务预算（{_elapsed:.0f}s > {self.total_budget_sec:.0f}s）；"
                    f"已完成 {_done}/{len(task.steps)} 步，产物保留"
                )
                self.store.save(task)
                logger.warning("[agent] 任务 %s 超整任务预算：%s", task.id, task.error)
                await self._notify(
                    task,
                    f"[超时中止] 本喵算了算，这个活已经花了 {_elapsed:.0f} 秒、"
                    f"超过 {self.total_budget_sec:.0f} 秒的上限了，先停一下喵。\n"
                    f"已完成 {_done}/{len(task.steps)} 步，已经做出来的东西都留着。",
                )
                if self.auditor:
                    try:
                        self.auditor.emit("agent_task_budget_exceeded", task_id=task.id,
                                          elapsed_sec=round(_elapsed, 1),
                                          budget_sec=self.total_budget_sec,
                                          steps_done=_done)
                    except Exception:
                        logger.debug("预算超限审计失败", exc_info=True)
                return f"任务超出整任务预算已中止（{_elapsed:.0f}s）。"

            batch = self._collect_batch(task, idx)
            total = len(task.steps)
            # 🔴 2026-10-23：**步骤进度节流**。
            #   生产实证：一个 23 秒的任务连推 6 条（报备 + 3 步 + 完成 + 产物），
            #   在私聊里是刷屏。而「一个每 23 秒发 6 条消息的助理会被关掉」——
            #   一旦关掉通知，前面做的所有主动交付都白费。
            #   判据：**短任务静默跑完只报结果**；长任务才逐步报进度。
            _notify_steps = self._should_notify_steps(task)
            for step in batch:
                if _notify_steps:
                    await self._notify(task, f"[{step.index + 1}/{total}] {step.description}…")
                step.status = "running"
            self.store.save(task)

            try:
                budget.check_duration()
                results = await self._run_batch(batch, task, budget)
            except asyncio.TimeoutError:
                step = batch[0]
                step.status = "failed"
                step.error = "步骤超时"
                task.current_step = step.index
                if await self._replan_or_give_up(task, step, "步骤超时", "step_failed"):
                    executed += 1
                    idx = task.current_step
                    continue
                task.status = "failed"
                task.error = f"步骤 {step.index + 1} 超时"
                failed = True
                self.store.save(task)
                break
            except ResourceExceeded as e:
                step = batch[0]
                step.status = "failed"
                step.error = str(e)
                task.current_step = step.index
                task.status = "failed"
                task.error = str(e)
                failed = True
                self.store.save(task)
                await self._notify(task, f"[资源超限] {e}")
                break
            except StepFailed as e:
                step = batch[0]
                step.status = "failed"
                step.error = str(e)
                task.current_step = step.index
                if await self._replan_or_give_up(task, step, str(e), "step_failed"):
                    executed += 1
                    idx = task.current_step
                    continue
                task.status = "failed"
                task.error = f"步骤 {step.index + 1} 失败: {e}"
                failed = True
                self.store.save(task)
                break

            # ---- 成功路径 ----
            if len(batch) == 1:
                step = batch[0]
                step.status = "done"
                step.result = results[0][:4000]
                task.context[f"step_{step.index}_result"] = results[0][:2000]
                ok, vreason = True, ""
                if step.verify:
                    checked, ok, vreason = await self._verify_step(step, task)
                    step.verified = checked and ok
                if not ok:
                    step.verified = False
                    step.status = "failed"
                    step.error = f"验收未通过：{vreason}"
                    task.current_step = step.index
                    self.store.save(task)
                    if await self._replan_or_give_up(
                            task, step, step.error, "verify_failed"):
                        executed += 1
                        idx = task.current_step
                        continue
                    task.status = "failed"
                    task.error = f"步骤 {step.index + 1} 验收未通过：{vreason}"
                    failed = True
                    self.store.save(task)
                    break
                task.current_step = step.index + 1
                self.store.save(task)
                # 🔴 2026-10-25：**编造检测**（不阻断执行，只如实标注）。
                #   生产实证：某步声称「已经帮你抽好了」，但 tools_called 为空。
                #   这里把结果改成**带警示前缀**的文本，让下游交付/汇报无法再说谎。
                #   不做成「失败」：任务整体可能仍有价值，且擅自判失败会丢掉
                #   其他步骤的真实产出（比说谎更糟）。
                try:
                    if self._looks_like_fake_action(step):
                        step.result = (
                            "⚠️【未实际执行】本步声称已完成某个操作，但**没有任何工具调用记录**"
                            "（可能是模型自行编造的结果，不可信）。\n原输出："
                            + str(step.result or "")[:1500]
                        )
                        step.verified = False
                        if self.auditor:
                            try:
                                self.auditor.emit(
                                    "agent_step_fabricated", task_id=task.id,
                                    step_index=step.index,
                                    step_desc=str(step.description)[:80],
                                )
                            except Exception:
                                logger.debug("编造审计失败", exc_info=True)
                        logger.warning(
                            "[编造检测] 步骤 %d 声称做了操作但无工具调用：%s",
                            step.index + 1, str(step.description)[:60],
                        )
                except Exception:
                    logger.debug("编造检测失败（已隔离）", exc_info=True)
                result_parts.append(f"步骤{step.index + 1}: {step.description} ✓")
                executed += 1
                idx = task.current_step
            else:
                # 并行批次不逐项验收（都是确定性显式工具调用，验收交给后续步骤）
                for step, result in zip(batch, results):
                    step.status = "done"
                    step.result = result[:4000]
                    task.context[f"step_{step.index}_result"] = result[:2000]
                    result_parts.append(f"步骤{step.index + 1}: {step.description} ✓（并行）")
                task.current_step = batch[-1].index + 1
                self.store.save(task)
                executed += len(batch)
                idx = task.current_step

        task.context["result_parts"] = result_parts

        # 09-26 F4：媒体交付自检 —— 「工具说成功」≠「用户收到图」。
        # 生产事故：`generate_image` 返回 ok=True（19 字「图片已生成，将作为本次回复
        # 的图片发送。」）、887 KB 图已落盘，但 Agent 路径无人回收路径 ⇒ 任务仍被判
        # `completed`，最后一步文本还谎称「已为你生成一张…照片」。
        # fail-closed：调用过媒体工具却拿不出**任一**可用文件 ⇒ 不得判完成。
        media_used = sorted(self.media_tools_used(task))
        if (not failed and task.status == "executing" and media_used
                and not self.live_media_paths(task)):
            failed = True
            task.status = "failed"
            task.error = (f"媒体交付失败：调用过 {'/'.join(media_used)} "
                          "但未产出任何可用文件，不判定为完成")
            if self.auditor:
                self.auditor.emit("agent_media_delivery_missing", task_id=task.id,
                                  media_tools=media_used)
        elif not failed and task.status == "executing":
            task.status = "completed"
            task.error = ""
        self.store.save(task)

        # 熔断统计
        if task.status == "failed":
            self._fail_streak += 1
            if self._fail_streak >= 3:
                self._pause_until = time.time() + 600
                await self._notify(task, "⚠️ 连续 3 个 Agent 任务失败，能力暂停 10 分钟")
        else:
            self._fail_streak = 0

        if task.status == "completed":
            # 🔴 2026-10-23：`[完成] 任务已全部执行` 与随后的**产物交付**是重复的。
            #   生产实证：23 秒任务的消息序列是
            #     报备 → [1/3] → [2/3] → [3/3] → [完成] → 产物全文
            #   最后一条 `[完成]` 夹在中间纯属噪音。
            #   改为：**有步骤产出时不再单独发 `[完成]`**（产物交付本身就是完成信号）；
            #   只有「跑完了但什么也没产出」时才发这一句（否则用户不知道结束了）。
            _has_output = any(
                len(str(getattr(s, "result", "") or "").strip()) >= 20
                for s in (task.steps or [])
            )
            if not _has_output:
                await self._notify(task, "[完成] 任务已全部执行（但没有可展示的产出）")
            return "\n".join(result_parts) or "（无步骤产出）"
        if task.status == "cancelled":
            return "任务已取消。"
        await self._notify(task, f"[失败] {task.error or '未知错误'}")
        return f"任务失败：{task.error or '未知错误'}"

    # ------------------------------------------------------------------
    # P2-1 验收
    # ------------------------------------------------------------------

    def _verify_available(self) -> bool:
        if not self.verify_steps:
            return False
        return getattr(self.core, "llm", None) is not None

    async def _verify_step(self, step: Step, task: AgentTask) -> tuple[bool, bool, str]:
        """独立验收：返回 ``(是否真的验过, 是否通过, 原因)``。

        - ``checked=False``：没有判据 / 无 LLM / 验收器故障 → 未真正验收，
          调用方据此不要把 ``verified`` 标成 True（「没验」和「验过通过」是两回事）。
        - ``checked=True, ok=False``：判据明确不成立。
        """
        criterion = (step.verify or "").strip()
        if not criterion or not self._verify_available():
            return False, True, ""
        llm = getattr(self.core, "llm", None)
        prompt = (
            f"原始请求：{task.request}\n"
            f"刚完成的步骤：{step.description}\n"
            f"该步骤的验收标准：{criterion}\n"
            f"步骤实际输出（截断）：\n{(step.result or '（空）')[:self.VERIFY_OUTPUT_CHARS]}\n\n"
            "请判断「步骤实际输出」是否满足验收标准。只回答一行："
            "PASS 或 FAIL，后跟一句理由。"
        )
        try:
            # 归因（2026-10-03）：本方法在 outbound scope 之外，若不打标就会落到默认
            # `chat/chat`、trace_id=unscoped，把 Agent 步骤验收的失败算进用户可见的回复链。
            with llm_scope("agent", "verify"):
                raw = await asyncio.wait_for(
                    llm.chat([{"role": "user", "content": prompt}],
                             temperature=0.0, max_tokens=120),
                    timeout=self.VERIFY_TIMEOUT,
                )
        except Exception as e:  # noqa: BLE001 - 验收器故障不阻塞主流程
            if self.auditor:
                self.auditor.emit("agent_verify_inconclusive", task_id=task.id,
                                  step=step.index, reason=f"verify_error:{type(e).__name__}")
            return False, True, ""

        text = (raw if isinstance(raw, str) else str(raw)).strip()
        head = text.upper().lstrip()
        if head.startswith("FAIL") or "FAIL" in head[:12]:
            return True, False, text[:200] or "验收判定不通过"
        if head.startswith("PASS") or "PASS" in head[:12]:
            if self.auditor:
                self.auditor.emit("agent_verify_passed", task_id=task.id,
                                  step=step.index, criterion=criterion[:120])
            return True, True, ""
        # 判定模糊 → 放行但留痕（避免判据含糊毁掉已成功的步骤）
        if self.auditor:
            self.auditor.emit("agent_verify_inconclusive", task_id=task.id,
                              step=step.index, reason=f"ambiguous:{text[:80]}")
        return True, True, ""

    # ------------------------------------------------------------------
    # P2-1 重规划
    # ------------------------------------------------------------------

    def _replan_available(self, task: AgentTask) -> bool:
        if task.replan_count >= self.max_replans:
            return False
        # 重规划需要 LLM 才能真正换思路；无 LLM 只会把一次失败拖成多次失败
        return getattr(self.planner, "llm", None) is not None

    async def _replan_or_give_up(self, task: AgentTask, failed_step: Step,
                                 error: str, reason: str) -> bool:
        """尝试重规划；返回 True 表示已替换剩余计划、外层可继续循环。"""
        if not self._replan_available(task):
            return False
        done = [s.description for s in task.steps
                if s.status == "done" and s.index < failed_step.index]
        remaining = [s.description for s in task.steps[failed_step.index + 1:]
                     if s.status == "pending"]
        try:
            new_steps = await self.planner.replan(
                request=task.request, done=done,
                failed_step_desc=failed_step.description,
                error=error, remaining=remaining, reason=reason,
            )
        except Exception:
            logger.exception("重规划失败（已隔离，按原失败处理）")
            return False
        if not new_steps:
            return False

        kept = task.steps[:failed_step.index]
        for offset, s in enumerate(new_steps):
            s.index = len(kept) + offset
        task.steps = kept + new_steps
        task.current_step = len(kept)
        task.replan_count += 1
        task.context[f"replan_{task.replan_count}"] = {
            "failed_step": failed_step.description,
            "reason": reason,
            "error": error[:300],
            "new_steps": [s.description for s in new_steps],
        }
        task.error = ""
        self.store.save(task)
        if self.auditor:
            self.auditor.emit("agent_replan", task_id=task.id,
                              replan_count=task.replan_count, reason=reason,
                              failed_step=failed_step.description[:200],
                              error=error[:200],
                              new_steps=[s.description[:120] for s in new_steps])
        await self._notify(
            task,
            f"[重规划 #{task.replan_count}] 「{failed_step.description[:40]}」未达成"
            f"（{reason}），改为 {len(new_steps)} 步替代方案",
        )
        return True

    #: 步骤进度推送的门槛：步数达到此值，或已耗时超过阈值，才逐步报进度。
    #: 🔴 2026-10-23：低于门槛的任务**静默跑完只报结果**。
    #: 依据：生产实测一个 23 秒 / 3 步的任务连推 6 条消息，私聊被刷屏；
    #: 而「每 23 秒发 6 条的助理」会导致 owner 直接关掉通知 ——
    #: 一旦关掉，所有主动交付的价值都归零。
    NOTIFY_STEPS_MIN_COUNT = 5
    NOTIFY_STEPS_MIN_ELAPSED_SEC = 60.0

    def _should_notify_steps(self, task: AgentTask) -> bool:
        """本次是否值得逐步推送进度（短任务静默）。

        判据（任一满足即推）：
          1. 计划步数 ≥ `NOTIFY_STEPS_MIN_COUNT`（活儿本身就长）；
          2. 任务已耗时 ≥ `NOTIFY_STEPS_MIN_ELAPSED_SEC`（跑着跑着变长了）。

        fail-closed 取向：**判定失败时倾向于不推**（宁可少一条消息，
        也不要because判据崩了而刷屏）。
        """
        try:
            total = len(getattr(task, "steps", []) or [])
            if total >= self.NOTIFY_STEPS_MIN_COUNT:
                return True
            _start = getattr(task, "created_at", 0) or 0
            if _start and (time.time() - float(_start)) >= self.NOTIFY_STEPS_MIN_ELAPSED_SEC:
                return True
            return False
        except Exception:
            logger.debug("进度推送判定失败 ⇒ 按「不推」处理", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # P2-1 并行批次
    # ------------------------------------------------------------------

    def _collect_batch(self, task: AgentTask, idx: int) -> list[Step]:
        """收集可并行的连续步骤。默认返回单元素列表（串行）。"""
        steps = task.steps
        first = steps[idx]
        if not self.parallel_steps or not first.parallel_group:
            return [first]
        group = first.parallel_group
        batch: list[Step] = []
        j = idx
        while j < len(steps) and len(batch) < self.max_parallel:
            s = steps[j]
            # 只并发「显式指定工具」的步骤：ReAct 步骤会互相干扰上下文，不可并发
            if s.parallel_group != group or not s.tool_name:
                break
            batch.append(s)
            j += 1
        return batch if len(batch) > 1 else [first]

    async def _run_batch(self, batch: list[Step], task: AgentTask,
                         budget: ResourceBudget) -> list[str]:
        if len(batch) == 1:
            return [await asyncio.wait_for(
                self._run_step(batch[0], task, budget), timeout=self.STEP_TIMEOUT)]

        # 并发批次共享同一个 ResourceBudget。不额外加锁：ResourceBudget 的方法
        # 内部没有 await，单线程 asyncio 下不存在真正的竞态；加锁反而会把批次串行化
        # （这正是本方法存在的意义）。
        return list(await asyncio.wait_for(
            asyncio.gather(*(self._run_step(s, task, budget) for s in batch)),
            timeout=self.STEP_TIMEOUT))

    # ------------------------------------------------------------------
    # 单步执行（带重试）
    # ------------------------------------------------------------------

    async def _run_step(self, step: Step, task: AgentTask, budget: ResourceBudget) -> str:
        last_err: str | None = None
        for attempt in range(1, self.MAX_TOOL_RETRIES + 1):
            step.attempts += 1
            try:
                result = await self._run_step_body(step, task, budget)
                budget.consume_output(len(str(result or "")) / 1024.0)
                if tool_result_ok(result):
                    return result
                last_err = (result or "")[:300]
            except ResourceExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"[:300]

            if attempt < self.MAX_TOOL_RETRIES:
                await asyncio.sleep(min(2 ** attempt, 8))  # 指数退避

        step.error = last_err or "unknown"
        raise StepFailed(last_err)

    async def _run_step_body(self, step: Step, task: AgentTask,
                             budget: ResourceBudget) -> str:
        """单步的实际执行体：注入主人调用上下文 + 回收受控媒体。

        09-26 重构：原先只有 ReAct 分支自带 call-context 注入，显式 ``tool_name``
        分支（``core.tools.call``）**没有任何上下文**——若某步显式调用生图工具，
        路径会落进一个无人读、随即被丢弃的匿名 context。现在两个分支共用同一段
        注入/回收逻辑，异常交回 ``_run_step`` 做重试。
        """
        tools = getattr(self.core, "tools", None)
        ctx_token = None
        if (tools is not None and hasattr(tools, "get_call_context")
                and hasattr(tools, "set_call_context")):
            # 主人分层沙箱：把权限等级注入调用上下文（防御式兼容测试桩）
            ctx_token = tools.set_call_context(
                **{**tools.get_call_context(), "permission_level": LEVEL_OWNER})
        try:
            if step.tool_name:
                self._record_tools(task, [step.tool_name])
                budget.consume_network_request()
                return await self.core.tools.call(
                    step.tool_name, step.tool_args or {}, LEVEL_OWNER
                )
            return await self._react_for_step(step, task)
        finally:
            # 🔴 必须在使用方 reset 之前回收媒体路径——reset 之后 context 变更即丢。
            if tools is not None and hasattr(tools, "get_call_context"):
                try:
                    self._harvest_media(task, tools.get_call_context())
                except Exception:
                    logger.exception("回收 Agent 受控媒体失败（已隔离）")
            if ctx_token is not None and hasattr(tools, "reset_call_context"):
                tools.reset_call_context(ctx_token)

    async def _react_for_step(self, step: Step, task: AgentTask) -> str:
        from anima.agent.react import ReActLoop

        llm = getattr(self.core, "llm", None)
        tools = getattr(self.core, "tools", None)
        if llm is None or tools is None:
            return "(无 LLM/工具，无法执行该步骤)"
        loop = ReActLoop(llm, tools, max_rounds=6, permission_level=LEVEL_OWNER)
        # 🔴 2026-10-25：**把前面步骤的真实产出喂给本步**。
        #   生产实证（10-25 11:43，「维系和主人的关系」四步全跑）：
        #     步骤1「主人～最近是不是又在偷偷熬夜打游戏呀？…快来跟我说说话嘛」
        #     步骤2「主人最近好像对玩 cod23、折腾 dots 样式和吃榆林炸鸡腿比较感兴趣」
        #     步骤3「主人～这两天过得怎么样呀？…本喵有点想你啦」
        #     步骤4「主人～最近过得怎么样呀？…如果有什么烦心事随时找我哦」
        #   **四步各说各的、都在问同一件事** —— 因为每步的 user message 只有
        #   `任务背景 + 当前步骤`，**完全看不到前序步骤做了什么**。
        #   而我上轮加的 prompt 规则「不要重复前面的步骤说过的话」
        #   **根本无从遵守**：它不知道前面说了什么。
        _prior = []
        for _st in sorted(getattr(task, "steps", []) or [],
                          key=lambda s: getattr(s, "index", 0)):
            if int(getattr(_st, "index", 0)) >= int(getattr(step, "index", 0)):
                continue
            _r = str(getattr(_st, "result", "") or "").strip()
            if not _r:
                continue
            _prior.append(
                f"- 步骤{int(_st.index) + 1}「{str(getattr(_st, 'description', ''))[:40]}」"
                f"已产出：{_r[:300]}"
            )
        _prior_block = (
            "\n\n【前面步骤已完成，以下是它们的真实产出 —— 你必须基于它们继续，"
            "**不要重复它们已经说过的话，也不要重新问一遍同样的问题**】\n"
            + "\n".join(_prior[-3:])
        ) if _prior else ""

        # 🔴 2026-10-25：**只有最后一步是要发给用户的话**。
        #   生产实证：四步全部被交付，用户看到四段互相重复的「主人～最近怎么样呀」。
        #   前面步骤应是**干活**（检索/整理/判断），不该写成问候语；
        #   最后一步才是**成品**（要对人说的一句话）。
        _is_last = True
        try:
            _all = sorted(getattr(task, "steps", []) or [],
                          key=lambda s: getattr(s, "index", 0))
            if _all:
                _is_last = int(getattr(step, "index", 0)) >= int(_all[-1].index)
        except Exception:
            logger.debug("判断是否最后一步失败（按最后一步处理）", exc_info=True)

        _role_hint = (
            "【这是一步内部工作】只做事：检索/提取/整理/判断，把结果如实写出来。"
            "**不要写成对用户说的话**，不要问候、不要撒娇、不要「主人～」开头 —— "
            "这段话不会直接发给人，只会作为下一步的输入。"
            if not _is_last else
            "【这是最后一步，你的输出会直接发给用户】用哈基米的口吻说，"
            "**25 字以内**，像群里随口一句；不要复述前面步骤做过的检索/分析过程。"
        )

        messages = [
            {"role": "system", "content": (
                "你是哈基米（一只 QQ 群里的猫娘，男孩子的那种），正在做一件具体的事。"
                "用工具取得结果即可，不要闲聊。\n"
                # 🔴 2026-10-25：**步骤输出会直接交付给用户**，所以必须守住人格。
                #   生产实证（10-24 11:44）：某目标推进任务的三个步骤分别输出
                #     「主人最近在忙什么呢？…记得歇一歇喵~」
                #     「主人最近在忙什么呀？注意到您辛苦啦，记得多喝水…」  ← 突变「您」
                #     「主人～最近过得怎么样呀？…」                          ← 客服腔
                #   原因是本 system prompt 只说「你是 Anima 的执行代理」——
                #   **完全没有哈基米人格**，模型于是退回默认的助手腔。
                "写作要求（会被原样发给用户看）：\n"
                "- 自称「本喵」或「我」，**绝不用「本大爷 / 老子 / 老娘」**；\n"
                "- 对聊天对象用「你」或名字，**不用「您」** —— 那是客服腔，不是猫娘；\n"
                "- 语气软、口语化，可以困/好奇/黏人/小脾气，**不要居高临下、不要客套**；\n"
                "- **说短**：能一句话说完就别两句（群里真人平均 14 个字）；\n"
                "- 只说这一次的结果，**不要重复前面的步骤说过的话**；\n"
                "- 没有真正调用工具取得的数据，**绝不声称已经完成**（不许编造结果）。\n"
                + _role_hint
            )},
            {"role": "user", "content": (
                f"任务背景：{task.request}\n当前步骤：{step.description}{_prior_block}"
            )},
        ]
        try:
            return await loop.run(messages, temperature=0.3, max_tokens=600)
        finally:
            # 09-26：ReAct 真正调用过的工具名要记进任务上下文，供媒体交付自检（F4）
            # 判定「到底有没有走生图这条路」。`tool_trace` 在 run() 开头重置，异常
            # 路径下也能读到已发生的部分轨迹。
            try:
                _names = [
                    t.get("name") for t in (getattr(loop, "tool_trace", None) or [])
                ]
                self._record_tools(task, _names)
                # 🔴 2026-10-25：同时回填到 **step**，供「编造检测」使用。
                #   `_record_tools` 只记到 task.context，粒度太粗 ——
                #   无法判断「**哪一步**没有真正调用工具」。
                try:
                    step.tools_called = [str(n) for n in _names if n]
                except Exception:
                    logger.debug("回填 step.tools_called 失败（已隔离）", exc_info=True)
            except Exception:
                logger.exception("记录 Agent 工具轨迹失败（已隔离）")
