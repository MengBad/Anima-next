"""观察驱动执行循环：看现实 → 决定动作 → 执行 → 再看。

## 为什么要有这个文件（2026-11-04 结论）

旧路径是「计划驱动」：planner 一次性产出 4 步，executor 逐步执行，
每步用另一个 LLM 读 step.result 字符串判 PASS/FAIL。生产数据证明它做不完任务：

    285f251b52  总耗时 21 秒  4 步  replan=0
      └ 第 2 步 tools=[browse_credential, ask_user]  ← 一次浏览器都没开

计划里没写「先看看页面有什么」，它就不会去看；验收器读的是文本不是页面，
所以截图存了也判不出「已登录」。四个任务 replan_count 全是 0。

## 这个循环做什么

    while 未完成 and 未超预算:
        obs   = 收集上一轮的产物（截图/文件/HTTP 文本）
        reply = ReAct 一轮（模型自选工具，可多轮）
        ok    = 用**磁盘上的事实**判完成（不是让 LLM 猜）

步数是执行的产物，不是前提。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from anima.agent import artifact

logger = logging.getLogger("anima.agent.loop")

DEFAULT_ROUND_TIMEOUT = 300.0
DEFAULT_BUDGET_SEC = 1800.0
DEFAULT_MAX_ROUNDS = 40
STALL_ROUNDS = 4


@dataclass
class Observation:
    """一轮观察到的现实。"""

    kind: str = ""
    path: str = ""
    content: str = ""
    ok: bool = True
    note: str = ""


@dataclass
class RoundRecord:
    """一轮的历史，回灌给模型避免重复劳动。"""

    index: int
    reply: str = ""
    tools: list = field(default_factory=list)
    new_artifacts: list = field(default_factory=list)
    elapsed: float = 0.0

class ObserveLoop:
    """观察驱动的执行循环。"""

    def __init__(self, core: Any, *,
                 max_rounds: int = DEFAULT_MAX_ROUNDS,
                 budget_sec: float = DEFAULT_BUDGET_SEC,
                 round_timeout: float = DEFAULT_ROUND_TIMEOUT,
                 notify: Optional[Callable[[str], Any]] = None,
                 react_factory: Optional[Callable[..., Any]] = None,
                 llm: Any = None,
                 tools_for: Optional[Callable[[], Any]] = None):
        self.core = core
        self.max_rounds = max(1, int(max_rounds))
        self.budget_sec = float(budget_sec)
        self.round_timeout = float(round_timeout)
        self.notify = notify
        self._react_factory = react_factory
        self._llm = llm
        self._tools_for = tools_for
        self.rounds: list = []

    # 依赖惰性解析：不要在构造期抓 core.llm（装配顺序问题，见 owner_agent）
    def _llm_now(self) -> Any:
        if self._llm is not None:
            return self._llm
        return getattr(self.core, "llm", None)

    def _tools_now(self) -> Any:
        if self._tools_for is not None:
            return self._tools_for()
        return getattr(self.core, "tools", None)

    def _make_react(self, tools: Any, llm: Any) -> Any:
        if self._react_factory is not None:
            return self._react_factory(llm, tools)
        from anima.agent.react import ReActLoop
        from anima.tools import LEVEL_OWNER
        return ReActLoop(llm, tools, max_rounds=12, permission_level=LEVEL_OWNER)

    # -- 观察 --

    def collect_observations(self, task: Any) -> list:
        """把任务上下文里已有的产物转成 Observation 列表（只读事实）。"""
        ctx = getattr(task, "context", None) or {}
        out: list = []
        for p in artifact.media_paths(ctx):
            ok, why = artifact.verify_screenshot(p)
            out.append(Observation(kind="screenshot", path=p, ok=ok, note=why))
        return out

    def _harvest(self, tools: Any, task: Any) -> None:
        """把本轮工具产出的媒体路径从 call context 收回任务上下文。"""
        try:
            if not hasattr(tools, "get_call_context"):
                return
            got = tools.get_call_context().get("generated_image_paths") or []
        except Exception:
            return
        if not isinstance(got, list) or not got:
            return
        ctx = getattr(task, "context", None)
        if not isinstance(ctx, dict):
            return
        bucket = ctx.get("generated_image_paths")
        if not isinstance(bucket, list):
            bucket = []
            ctx["generated_image_paths"] = bucket
        seen = {str(x) for x in bucket}
        for p in got:
            sp = str(p or "")
            if sp and sp not in seen:
                seen.add(sp)
                bucket.append(sp)
    def render_observations(self, obs: list) -> str:
        if not obs:
            return "（还没有任何截图/文件产物）"
        lines = []
        for o in obs[-6:]:
            mark = "OK" if o.ok else "BAD"
            tail = o.path or o.content[:80]
            note = f" —— {o.note}" if o.note else ""
            lines.append(f"  [{mark}] {o.kind} {tail}{note}")
        return "\n".join(lines)

    def render_history(self) -> str:
        if not self.rounds:
            return ""
        lines = []
        for r in self.rounds[-6:]:
            tools = "/".join(r.tools) if r.tools else "(无工具)"
            head = str(r.reply or "").strip().replace("\n", " ")[:120]
            lines.append(f"  第{r.index}轮 工具={tools} 说明={head}")
        return "\n".join(lines)

    # -- 完成判据 --

    def check_completion(self, task: Any, obs: list, rounds: list) -> tuple:
        """用**事实**判完成。返回 (是否完成, 原因)。"""
        ctx = getattr(task, "context", None) or {}
        want = str(ctx.get("done_when_kind") or "").strip().lower()

        if want == "screenshot":
            good = [o for o in obs if o.kind == "screenshot" and o.ok]
            if good:
                return True, f"已产出合格截图 {len(good)} 张"
            return False, "还没有合格截图"

        good = [o for o in obs if o.ok]
        if not good:
            return False, "还没有任何可用产物"
        if not rounds:
            return False, "还没有执行轮次"
        if _says_done(str(rounds[-1].reply or "")):
            return True, "模型判断已收工且有可用产物"
        return False, "模型还没说做完"
    # -- 主循环 --

    async def run(self, task: Any) -> str:
        """跑观察循环，返回最终交付文本。"""
        llm = self._llm_now()
        tools = self._tools_now()
        if llm is None or tools is None:
            return "执行代理未就绪（LLM 或工具不可用）。"

        started = time.monotonic()
        stall = 0
        last_count = 0
        final_text = ""

        for i in range(1, self.max_rounds + 1):
            if getattr(task, "status", "") == "waiting_user":
                return "在等主人回答，先停在这里。"
            if self.budget_sec > 0 and (time.monotonic() - started) > self.budget_sec:
                return final_text or f"超时（{self.budget_sec:.0f}s），已做的都留着。"

            obs = self.collect_observations(task)
            ok, why = self.check_completion(task, obs, self.rounds)
            if ok:
                logger.info("[观察循环] 第 %d 轮判定完成：%s", i, why)
                return final_text or why

            if len(obs) <= last_count:
                stall += 1
            else:
                stall = 0
            last_count = len(obs)
            if stall >= STALL_ROUNDS:
                logger.info("[观察循环] 连续 %d 轮无新产物，停止", stall)
                if self.notify:
                    await _safe(self.notify, "本喵试了几轮都没进展，先把已有的给你。")
                return final_text or "试了几轮没有进展，先把已有的结果给你。"

            prompt = self.build_prompt(task, obs)
            loop = self._make_react(tools, llm)
            # 2026-11-04：必须注入调用上下文并回收媒体 —— 否则工具产出的截图
            # 只写进 get_call_context() 的临时 dict，reset 后永久丢失，
            # 观察循环永远看不到产物（生产实测 108s / 3 轮 / new=0）。
            ctx_token = None
            if hasattr(tools, "set_call_context"):
                try:
                    from anima.tools import LEVEL_OWNER
                    base = tools.get_call_context() if hasattr(tools, "get_call_context") else {}
                    ctx_token = tools.set_call_context(**{**base, "permission_level": LEVEL_OWNER})
                except Exception:
                    ctx_token = None
            try:
                reply = await asyncio.wait_for(
                    loop.run([{"role": "user", "content": prompt}],
                             temperature=0.3, max_tokens=800),
                    timeout=self.round_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("[观察循环] 第 %d 轮超时 %.0fs", i, self.round_timeout)
                reply = "(这一轮超时了)"
            except Exception:
                logger.exception("[观察循环] 第 %d 轮异常", i)
                reply = "(这一轮出错了)"
            finally:
                # 必须先回收再 reset（reset 之后 context 就变了）
                self._harvest(tools, task)
                if ctx_token is not None and hasattr(tools, "reset_call_context"):
                    try:
                        tools.reset_call_context(ctx_token)
                    except Exception:
                        logger.debug("reset_call_context 失败（已隔离）", exc_info=True)

            traced = [str(t.get("name")) for t in (getattr(loop, "tool_trace", None) or [])
                      if isinstance(t, dict) and t.get("name")]
            after = self.collect_observations(task)
            known = {o.path for o in obs}
            new_ones = [o for o in after if o.path not in known]
            self.rounds.append(RoundRecord(
                index=i, reply=str(reply or "")[:500], tools=traced,
                new_artifacts=[o.path for o in new_ones],
                elapsed=round(time.monotonic() - started, 1),
            ))
            final_text = str(reply or "") or final_text
            logger.info("[观察循环] 第 %d 轮 工具=%s 新产物=%d",
                        i, "/".join(traced) or "无", len(new_ones))

        return final_text or f"跑满 {self.max_rounds} 轮仍未达成目标。"
    # -- prompt --

    def _answers_block(self, task: Any) -> str:
        ctx = getattr(task, "context", None) or {}
        try:
            rows = list(ctx.get("user_answers") or [])[-3:]
        except Exception:
            return ""
        lines = []
        for a in rows:
            if not isinstance(a, dict):
                continue
            q = str(a.get("question") or "")[:120]
            r = str(a.get("answer") or "")[:200]
            if r:
                lines.append(f"- 主人答：{r}")
        if not lines:
            return ""
        return ("\n\n【主人已经回答过 —— 照这个做，不要重复问】\n" + "\n".join(lines))

    def build_prompt(self, task: Any, obs: list) -> str:
        req = str(getattr(task, "request", "") or "")
        hist = self.render_history() or "  (还没开始)"
        return "\n".join([
            "你在替主人做一件真事。每轮只做**一个能推进的**动作，做完就停，下一轮会看到结果。",
            "",
            f"任务：{req}",
            self._answers_block(task),
            "",
            "【目前的实际产物】",
            self.render_observations(obs),
            "",
            "【已经做过的】",
            hist,
            "",
            "规则：",
            "- 先看现状再动手。需要页面信息就用 browse_web_act 的 get_text / screenshot。",
            "- **不要重复做已经做过的事**（看上面的历史）。",
            "- 缺账号密码就用 browse_credential 取；取不到就换路子（扫码/等主人/跳过该项）。",
            "- 同一件事失败两次就换方法，不要原样重试第三次。",
            "- 全部做完时，用一句话说明结果，并在句末写「收工」。",
            "",
            "现在做下一个动作。",
        ])


_DONE_WORDS = ("收工", "已完成", "都做完了", "全部完成", "搞定了")


def _says_done(text: str) -> bool:
    return any(w in str(text or "") for w in _DONE_WORDS)


async def _safe(fn: Callable[..., Any], *args: Any) -> None:
    try:
        res = fn(*args)
        if asyncio.iscoroutine(res):
            await res
    except Exception:
        logger.debug("通知失败（已隔离）", exc_info=True)