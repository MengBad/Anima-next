"""Pipeline 阶段共享工具函数与常量。"""

from __future__ import annotations
import asyncio
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.pipeline import PipelineContext

logger = logging.getLogger("anima.pipeline.stages")

# OneBot/CQ 是传输格式，不是对话正文。
from anima.constants import CQ_TRANSPORT_RE as _CQ_TRANSPORT_RE


def _bg_task_done(task: asyncio.Task) -> None:
    """fire-and-forget 后台任务的异常回调。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("后台任务异常 (name=%s)", task.get_name(), exc_info=exc)


def _clean_transport_text(text: str) -> str:
    return _CQ_TRANSPORT_RE.sub("", text or "").strip()


def _detect_hurtful(text: str) -> bool:
    """检测是否包含伤害性语言，排除 playful teasing 语境。"""
    if not text:
        return False
    playful_markers = (
        "喵", "略略略", "哼", "呸呸", "嘻嘻", "哈哈", "嘿嘿",
        "😂", "🤣", "😹", "🐱", "🐾", "～", "~",
        "本喵", "猫娘", "小鱼干", "鱼板", "猫窝", "踩奶",
        "笨蛋", "傻瓜", "呆子", "哼唧", "呜", "呐",
        "打你", "揍你", "弹你", "挠你", "咬你",
    )
    playful_count = sum(1 for m in playful_markers if m in text)
    hurtful = {
        "滚", "烦", "讨厌", "垃圾", "傻逼", "闭嘴", "别说了", "恶心",
        "废物", "菜", "fuck", "shit", "去死", "死开", "😡", "💢",
    }
    hurt_count = sum(1 for w in hurtful if w in text)
    if hurt_count == 0:
        return False
    if playful_count >= hurt_count:
        return False
    return True


# ---------------------------------------------------------------------------
# 跑偏兜底频控
# ---------------------------------------------------------------------------

_derail_history: dict[int, deque] = {}
_DERAIL_HISTORY_MAX = 1000


# ---------------------------------------------------------------------------
# 承诺跟进检测
# ---------------------------------------------------------------------------

_PROMISE_RE = re.compile(
    r"(回头帮你|待会儿帮你|下次|过会儿|稍后|等下|一会儿|"
    r"本喵去查|本喵帮你|帮你查|帮你看看|回头再|下次再|"
    r"等本喵|让本喵|本喵先记|记下了|记住了)",
    re.I,
)

# K1: 工具失败 + 承诺重试的检测
_FAILURE_RE = re.compile(
    r"(本喵没查到|查询失败|没搜到|没找到|查不到|搜不到|"
    r"工具失败|请求失败|没查到准信|脑子卡了)",
    re.I,
)
_RETRY_PROMISE_RE = re.compile(
    r"(等下再帮你|回头再查|过会儿再|下次再帮|稍后再|"
    r"本喵再试|再帮你查|回头补上)",
    re.I,
)
# 威胁 / 角色扮演攻击语境——这类内容不是"承诺"，不应触发跟进
_PROMISE_THREAT_RE = re.compile(
    r"(?:死定了|气死|弄死|打死|踩死|掐死|咬死|"
    r"把你.*踩|把你.*抠|把你.*拆|把你.*砸|"
    r"今晚死|明天死|等着瞧|你完了|你死定|"
    r"炸毛|尾巴竖|龇牙|嘶吼|狂踩|踩奶|"
    r"踩成筛子|扣下来当|抠下来当)"
)


def _detect_promise_and_schedule(ctx: "PipelineContext") -> None:
    """检测回复中的承诺性语句，创建议程以便后续主动跟进。"""
    if not ctx.reply or not ctx.group_id:
        return
    if ctx.detail_type != "group":
        return
    core = ctx.meta.get("core")
    if core is None:
        return

    if _PROMISE_THREAT_RE.search(ctx.reply):
        return

    has_promise = _PROMISE_RE.search(ctx.reply)
    has_failure = _FAILURE_RE.search(ctx.reply)
    has_retry_promise = _RETRY_PROMISE_RE.search(ctx.reply)

    if not has_promise and not (has_failure and has_retry_promise):
        return

    try:
        if has_failure and has_retry_promise:
            intent = (
                f"上次答应帮 {ctx.sender_name} 查的事没查到，再试一次。"
                f"当时的回复是：{ctx.reply[:100]}"
            )
            source = "failure_retry"
            priority = 0.5
        else:
            intent = (
                f"之前答应了 {ctx.sender_name} 一件事，看看是否需要跟进。"
                f"当时的回复是：{ctx.reply[:100]}"
            )
            source = "promise_followup"
            priority = 0.45

        core.propose_agenda(
            int(ctx.group_id), intent, priority=priority,
            expires_at=time.time() + 4 * 3600, source=source,
        )
        logger.info("%s议程已创建 group=%s user=%s", source, ctx.group_id, ctx.user_id)
    except Exception:
        logger.debug("议程创建失败")
