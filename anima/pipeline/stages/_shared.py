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


_MD_BOLD_RE = re.compile(r"\*\*([^\s*][^*\n]{0,60}?)\*\*")
_MD_HEAD_RE = re.compile(r"^[ \t]{0,3}#{1,4}\s+", re.M)
_LATEX_TEXT_RE = re.compile(r"\\(?:text|mathrm)\{([^}]*)\}")
_LATEX_FRAC_RE = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_LATEX_SYMBOLS = (
    ("\\times", "×"), ("\\cdot", "·"), ("\\approx", "≈"), ("\\pm", "±"),
    ("\\leq", "≤"), ("\\geq", "≥"), ("\\rightarrow", "→"), ("\\to", "→"),
    ("\\quad", " "), ("\\,", " "),
)


def _strip_markdown_math(text: str) -> str:
    """出站 Markdown/LaTeX 清洗（2026-09-06 用户体验测试④）。

    QQ 不渲染 markdown，工具类回复里的裸 `**加粗**`、`$公式$`、`### 标题`
    会原样暴露给群友。成对加粗去符号（单 `**` 运算符如 `2 ** 10` 因不配对
    而保留）、行首标题井号去掉、常见 LaTeX 命令转为可读文本、`$` 剥壳。
    列表符号与代码块内容不处理。
    """
    t = _MD_BOLD_RE.sub(r"\1", text)
    t = _LATEX_FRAC_RE.sub(lambda m: f"({m.group(1)})/({m.group(2)})", t)
    t = _LATEX_TEXT_RE.sub(r"\1", t)
    for raw, pretty in _LATEX_SYMBOLS:
        t = t.replace(raw, pretty)
    t = _MD_HEAD_RE.sub("", t)
    return t.replace("$", "")


def _clean_transport_text(text: str) -> str:
    return _strip_markdown_math(_CQ_TRANSPORT_RE.sub("", text or "")).strip()


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

    # P1-1：完成/取消信号先落锤——用户说「搞定了/不用了」→ 结掉本群最早未结的债。
    # 放在记账之前，避免同一轮里刚记完又被自己结掉。
    try:
        from anima.cognitive.debt import get_debt_manager as _get_dm
        _pcfg = getattr(getattr(core, "config", None), "persistence", None)
        _pdd = getattr(_pcfg, "data_dir", "data") if _pcfg else "data"
        _get_dm(_pdd).complete_by_keyword(ctx.text or "", str(ctx.group_id))
    except Exception:
        logger.debug("debt 完成信号匹配失败，隔离")

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
        # 深审 P1-2：标记本回复已建承诺跟进议程，供 handler 的 episodes.observe
        # 跳过第二次建档（双路径曾让用户几小时内被重复追问 2-3 次）
        ctx.meta["promise_agenda_created"] = True

        # P1-1：复用上面同一份命中结果再记一条结构化债（不新增第二套触发正则）。
        # agenda 管「什么时候主动说话」，debt 管「欠了什么、还了没有」。
        try:
            from anima.cognitive.debt import get_debt_manager as _get_dm2
            _dt = getattr(getattr(core, "config", None), "debt_tracking", None)
            if getattr(_dt, "enabled", True):
                _pcfg2 = getattr(getattr(core, "config", None), "persistence", None)
                _pdd2 = getattr(_pcfg2, "data_dir", "data") if _pcfg2 else "data"
                _get_dm2(_pdd2).add(
                    content=intent,
                    source_group=str(ctx.group_id),
                    source_user=str(ctx.user_id),
                    beneficiary=str(ctx.sender_name or ""),
                    source_text=ctx.reply[:200],
                )
        except Exception:
            logger.debug("debt 记账失败，隔离")
        logger.info("%s议程已创建 group=%s user=%s", source, ctx.group_id, ctx.user_id)
    except Exception:
        logger.debug("议程创建失败")


# ── 收敛计划 M2：统一记忆检索聚合层（只读） ────────────────────────────
# 目标（宪法「经历塑造自我」）：六套并行记忆（chat_memory/vector/crossgroup/
# world_model/episodes/self_notes）此前无统一视图。本层为「读侧聚合」：
# cognition 记忆阶段可经 unified_recall() 一次取回跨域要点，写入路径不变。
# M3 保留期分层在统一读侧之上叠加，避免碎片化各改各的。
async def unified_recall(core, query: str, *, group_id=None, user_id=None,
                         limits: dict | None = None) -> dict:
    """聚合六套记忆域的要点，返回 {domain: [lines]}。单域失败不影响其它域。"""
    lim = {"vector": 3, "chat": 5, "facts": 5, "notes": 3, "episodes": 3}
    lim.update(limits or {})
    out: dict[str, list[str]] = {}
    try:
        vm = getattr(core, "vector_memory", None)
        if vm is not None and query:
            hits = await vm.search(query, top_k=lim["vector"],
                                   source_uin=user_id, source_gid=group_id)
            out["vector"] = [str(h) for h in (hits or [])][: lim["vector"]]
    except Exception:
        pass
    try:
        from anima.memory.chat_memory import get_chat_memory
        cm = get_chat_memory()
        entries = cm.get_context("group", int(group_id), user_id=user_id) if group_id else []
        out["chat"] = [str(e.get("content", ""))[:120] for e in entries[-lim["chat"]:]]
    except Exception:
        pass
    try:
        cg = getattr(core, "crossgroup", None)
        if cg is not None and user_id:
            mgr = cg if hasattr(cg, "user_memory") else None
            um = getattr(mgr, "user_memory", None) if mgr else None
            if um is not None:
                out["facts"] = [str(f.get("content", f))[:120]
                                for f in um.summary_in_group(int(user_id), int(group_id or 0))[: lim["facts"]]]
    except Exception:
        pass
    try:
        from anima.memory.episodes import get_episode_store
        est = get_episode_store()
        eps = est.recent(user_id=int(user_id or 0), limit=lim["episodes"])
        out["episodes"] = [str(e.get("summary", ""))[:120] for e in eps]
    except Exception:
        pass
    return out
