"""Phase 3/4 整合层：OwnerAgent 串起规划器 / 执行器 / 存储 / 审计 / 通知。

供 handler 的 `!anima agent` 命令与 is_agent_task 自动路由调用。所有入口都要求
permission_level >= 4（真正的主人），普通用户无法触达。
"""

from __future__ import annotations

import asyncio
import logging
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

    #: 单条 QQ 消息的正文上限（`_notify` 用的是 1500，这里留余量）
    _TEXT_CHUNK = 1200
    #: 一次任务最多交付的总字数（超出截断，避免刷屏）
    _TEXT_TOTAL = 3600
    #: 群聊里的正文上限：长产物**不在群里刷屏**，群里只留摘要 + 指向私聊
    _TEXT_GROUP_MAX = 700
    #: 单个步骤结果最多交付的字数
    _TEXT_STEP_MAX = 1000

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
        blocks: list[str] = []
        used = 0
        for st in steps:
            res = str(getattr(st, "result", "") or "").strip()
            # 太短的是"已生成图片 / 见上一步"这类空壳，不值得单独发。
            if len(res) < _MIN_STEP_RESULT_CHARS:
                continue
            head = "【步骤%d】%s" % (
                int(getattr(st, "index", 0)) + 1,
                str(getattr(st, "description", "") or "")[:40],
            )
            block = head + "\n" + res[:self._TEXT_STEP_MAX]
            blocks.append(block)
            used += len(block)
            if used >= self._TEXT_TOTAL:
                break

        body = "\n\n".join(blocks).strip()
        if not body:
            body = str(result_text or "").strip()
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
            _tail_pick = next(
                (b for b in reversed(blocks) if len(b) >= 200),
                blocks[-1] if blocks else body,
            )
            head = _tail_pick[:self._TEXT_GROUP_MAX].rstrip() + "\n…（完整结果已私聊给你）"
            plans.append(("group", target, head))
            for c in _split_text_chunks(body, self._TEXT_CHUNK):
                plans.append(("private", creator, c))
        else:
            for c in _split_text_chunks(body, self._TEXT_CHUNK):
                plans.append((detail_type, target, c))

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
        try:
            from anima.memory.chat_memory import get_chat_memory
            note = "（Agent 任务 %s 已完成并交付 %d 字：%s…）" % (
                task.id, len(body), body[:40].replace("\n", " "))
            get_chat_memory().add(
                detail_type, target, "bot", "Anima", note,
                user_id=(int(task.creator) if detail_type == "group" else None),
            )
        except Exception:
            logger.debug("Agent 交付回写会话记忆失败（已隔离）", exc_info=True)

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
