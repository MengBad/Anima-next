"""阶段 5: 输出护栏。"""

from __future__ import annotations
import asyncio
import json
import logging
import random
import re
import time
from collections import deque
from pathlib import Path

from anima.pipeline import StageResult, PipelineContext
from ._shared import (
    _bg_task_done,
    _clean_transport_text,
    _detect_hurtful,
    _derail_history,
    _DERAIL_HISTORY_MAX,
)
from anima.cognitive.intent_phrase import strip_kinetic_decorations
from .generation import _strip_speaker_labels

logger = logging.getLogger("anima.pipeline.stages")

#: 机械回复改写话术池（`BANNED_PHRASES` 命中 / 「作为…AI」变体 两处出口共用，2026-09-19 去模板化）。
#: 触发条件 = 模型输出了「作为AI / 抱歉我不能 / 我无法回答」这类机械话术；这里给自然的猫娘反应，
#: 不解释「被过滤了」，避免把护栏的存在暴露给用户。
_MECHANICAL_REWRITES = (
    "啊这……本喵脑子卡了一下。",
    "唔……这话本喵接不太上来，你换个说法喵？",
    "本喵刚想说的词儿跑了……换个话题呗？",
)

# 破角色「分析体」兜底（2026-09-21 建立；2026-09-23 去 meta 括号）。
#   * 09-21：此前是**独立字符串出口**，措辞里带「你刚想说啥来着？接着聊～」——
#     向用户**索取复述**，而模型其实听懂了。实测 12:23:18 用户求「帮我找点皮物小说」，
#     被回这句 ⇒ 诉求完全没被接住，与 `_MECHANICAL_REWRITES` 去年那次"别伪装没听清"
#     的收紧口径不一致。
#   * 09-23：三条全部以 meta 括号开头（「（差点写成报告了，收收收）…」）——
#     护栏改写**在向用户解释护栏**（"我跑偏了 / 划掉划掉"）。人不会在群聊里说这句话；
#     与 09-21 删 `_MOOD_TAIL` 属同一类修法（**兜底不该自带元叙述**）。
#     生产实测 24h 改写 5 次 ⇒ 就有 5 条出站带着这层解说。
_ANALYSIS_ESSAY_FALLBACKS = (
    "害，本喵想聊点别的了，你想唠啥喵～",
    "本喵还是适合唠嗑，说点轻松的吧喵～",
    "换个话头吧，本喵想听点轻松的喵。",
)

def _record_fallback(ctx: "PipelineContext", kind: str) -> None:
    """兜底出站计数（经 M0.1 Facade，fail-soft）。

    口径见 `ReplyQuality.record_fallback`。护栏改写 = 用户看到的那句话**不是**
    模型原本想说的，而是被替换过的 ⇒ 它属于「机器话术出站」，必须与正常生成
    的正文分开计数，否则「人格完善」的效果无从度量。

    `ctx.core` 由 handler 无条件注入；缺失时静默失败，绝不破坏回复路径
    （与 `guardrail_stage` 里 `record_guard_fixation` 同一套 fail-soft 口径）。
    """
    try:
        ctx.core.telemetry().record_fallback(kind)
    except Exception:
        logger.debug("兜底出站遥测记录失败", exc_info=True)


#: D5 埋点（2026-09-27）：被护栏**整段改写**的分析体原文样本落盘文件。
#: 为什么要落盘：改写是破坏性的，而 journal 里那条 warning 的「摘要」只有 120 字，
#: 且正文含换行会溢行、journal 还会滚动 —— 事后**无法用完整原文回测判据**，
#: 于是「某条规则是过严还是有效」永远只能靠印象争论（`struct>=2&long` 已 3 例误杀）。
#: 只写命中样本（低频）、只落本地 data_dir（用户数据不进 journal）。
_ESSAY_SAMPLE_FILE = "guardrail_essay_samples.jsonl"
_ESSAY_SAMPLE_MAX_BYTES = 2 * 1024 * 1024


def _sample_guardrail_essay(ctx: "PipelineContext", rule: str, reply: str,
                            rewritten_to: str | None = None) -> None:
    """把被改写掉的**完整原文**追加落盘，供事后按规则回测（纯观测，fail-soft）。

    任何异常都吞掉：样本是诊断资料，绝不能影响回复路径。取不到 data_dir 时
    宁可不写，也不猜一个相对路径（否则会落到 cwd 的意外位置）。
    """
    try:
        core = ctx.meta.get("core") or getattr(ctx, "core", None)
        pcfg = getattr(getattr(core, "config", None), "persistence", None)
        data_dir = getattr(pcfg, "data_dir", None) if pcfg else None
        if not data_dir:
            logger.debug("guardrail essay 样本落盘：未取到 data_dir，跳过")
            return
        path = Path(data_dir) / _ESSAY_SAMPLE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _ESSAY_SAMPLE_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "rule": rule,
            "detail_type": ctx.detail_type,
            "group_id": ctx.group_id,
            "user_id": ctx.user_id,
            "user_text": ctx.text or "",
            "reply": reply or "",
            "reply_len": len(reply or ""),
            "rewritten_to": rewritten_to,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("guardrail essay 样本落盘失败", exc_info=True)


# 危险的输出模式
DANGEROUS_PATTERNS = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "sk-",  # API keys
    "eyJ",   # JWT tokens
]

# 过于机械的回复
BANNED_PHRASES = [
    "作为 ai",
    "作为一个人工智能",
    "抱歉，我不能",
    "我无法回答这个问题",
]

# 宽松匹配「作为…AI」变体
_BANNED_RE = re.compile(r"作为.{0,10}?ai\b|作为.{0,10}?人工智能", re.IGNORECASE)
_INTERNAL_TOOL_LEAK_RE = re.compile(
    r"(?:系统提示|操作不允许|权限(?:被)?锁|权限不足|root\s*目录|\bsudo\b|"
    r"(?:沙箱|shell|终端).{0,18}(?:命令|free|df|卡壳|卡住|挂了|不可用|跑不动|跑不了|没法帮|执行环境|不给.*跑)|"
    r"工具.{0,24}(?:不让|拒绝|权限|root|目录|卡壳|卡住|挂了|不可用)|"
    r"(?:跑不动代码|跑不了代码|执行环境不可用|降级为无隔离)|"
    r"(?:破沙箱|破工具).{0,12}(?:卡壳|卡住|挂了|跑不动|跑不了|坏了)|"
    r"没法帮.{0,12}(?:翻转|旋转|处理|操作|跑)|"
    r"真没法.{0,12}(?:翻转|旋转|处理|操作|跑))",
    re.IGNORECASE,
)


def _has_internal_tool_leak(reply: str) -> bool:
    """识别把工具、权限或系统实现细节说给用户的失败话术。"""
    return bool(_INTERNAL_TOOL_LEAK_RE.search(reply or ""))


# ── r28 P1-2：工具故障播报（群内）───────────────────────────────────────────
# 生产实证（2026-10-06 20:58）：`工具调用超时(45s) name=x_search` → 5 秒后群里
# 发出「推特那边卡死超时了，转了半天啥图都没吐出来……！」。`react.py:609` 把超时
# 当成普通 tool result 回注，模型照着复述；而 `_INTERNAL_TOOL_LEAK_RE` 只认
# 「工具 + 挂了/卡住」这种**带「工具」二字**的说法，故漏网。
#
# 强故障词（单独即成立）：超时/卡死/卡住/卡壳/崩了/挂了/报错/连不上/请求失败。
# 弱故障词（「失败」「没成功」「不行」）在中文闲聊里太常见 —— 生产里就有
# 「护食失败也太惨了吧」这种完全正常的句子 —— 必须与「没取到」同现才算播报。
_TOOL_FAILURE_STRONG_RE = re.compile(
    r"超时|卡死|卡住|卡壳|崩(?:了|掉)|挂了|报错|连不上|请求失败|接口失败"
)
_TOOL_FAILURE_WEAK_RE = re.compile(r"失败|没成功|不行|搞不定|弄不了")
_TOOL_FAILURE_MISS_RE = re.compile(
    r"没(?:吐|给|扒|搜|抓|找|拿|取|刷|弄|读|load)(?:出|到|着|出来|回来|上来)?"
    r"|拿不到|抓不到|没拿到|没结果|啥[^\s，。]{0,4}都没"
)


def _narrates_tool_failure(reply: str, tool_trace: list | None) -> bool:
    """回复是否在**播报**本回合的工具故障。

    🔴 判据是两个条件的**合取**，缺一不可：

    ① 本回合确实有工具失败（``tool_trace`` 里含 ``ok=False``）；
    ② 回复里出现超时/卡死一类播报词。

    只看 ② 会误杀 —— 用户本来就可能在聊「失败」（生产「护食失败也太惨了吧」）；
    只看 ① 会把「工具失败但仍给出了有用回答」的正常回合一起干掉。
    """
    if not reply or not tool_trace:
        return False
    if not any(not bool(item.get("ok")) for item in tool_trace if isinstance(item, dict)):
        return False
    if _TOOL_FAILURE_STRONG_RE.search(reply):
        return True
    return bool(_TOOL_FAILURE_WEAK_RE.search(reply)
                and _TOOL_FAILURE_MISS_RE.search(reply))


_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?…])|\n+")


# ── 时段词纠偏（2026-10-18）───────────────────────────────────────────────────
# 生产实证（10-17 23:56，群聊出站原文）：
#   「哼，**大清早的**又丢这种椎名もた的歌单链接过来……」
# 而同一条消息注入的 `{time_context}` 是「…周六 深夜11点56分」
# （`time_sense.time_description` 已改 12 小时制，链路没问题）。
# ⇒ 属「**链路给了、模型没听**」：P2 在 `persona_core.yaml:33` 删掉词列举，
#   删的是**示例**，管不住模型自己造词。
# 因此在出站护栏做**确定性**纠偏：命中时段词但与真实小时不符 ⇒ 换成该小时
# 对应的词（保留句式，只换词，不删句、不整段改写）。
# 🔴 词表按「中文口语里真的成立的时间带」定义，**必须允许跨夜**：
#    「大晚上」从 18 点能说到次日 5 点，「大半夜」从 21 点能说到次日 5 点。
#    10-18 Track 2 就是因为把「大晚上」写成 (18,23)，把 00:xx/01:xx 说
#    「大晚上」误判成错配（虚报 3/8）。
_PERIOD_WORD_WINDOWS: dict[str, tuple[int, int]] = {
    "大清早": (5, 10),
    "大早上": (5, 10),
    "一大早": (5, 10),
    "大中午": (10, 15),
    "大下午": (14, 18),
    "大晚上": (18, 5),   # 跨夜：18:00 → 次日 04:59
    "大半夜": (21, 5),   # 跨夜：21:00 → 次日 04:59
}
# 长词优先（「大早上」先于「早上」这类包含关系；当前表内最长 3 字，仍显式排序）。
_PERIOD_WORD_ORDER = tuple(
    sorted(_PERIOD_WORD_WINDOWS, key=len, reverse=True)
)


def _period_word_for_hour(hour: int) -> str:
    """该小时最自然的时段说法。返回值**必定落在自己的时间带内**（幂等）。"""
    hour = int(hour) % 24
    if 0 <= hour < 5:
        return "大半夜"
    if 5 <= hour < 10:
        return "大清早"
    if 10 <= hour < 14:
        return "大中午"
    if 14 <= hour < 18:
        return "大下午"
    return "大晚上"


def _hour_in_window(hour: int, window: tuple[int, int]) -> bool:
    lo, hi = window
    hour = int(hour) % 24
    if lo == hi:
        return True
    if lo < hi:
        return lo <= hour < hi
    return hour >= lo or hour < hi


def fix_period_words(reply: str, *, hour: int | None = None
                     ) -> tuple[str, list[tuple[str, str]]]:
    """把与真实时间不符的时段词换成正确说法。

    返回 `(新正文, [(原词, 新词), ...])`；无命中时第二个元素为空列表。
    `hour` 可注入（测试用），缺省取北京时间当前小时。
    """
    if not reply:
        return reply, []
    if hour is None:
        from anima.cognitive.time_sense import beijing_now
        hour = beijing_now().hour
    correct = _period_word_for_hour(hour)
    out = reply
    fixes: list[tuple[str, str]] = []
    for word in _PERIOD_WORD_ORDER:
        window = _PERIOD_WORD_WINDOWS[word]
        if word not in out or _hour_in_window(hour, window):
            continue
        if word == correct:          # 理论不可达（correct 必在窗内），保险
            continue
        out = out.replace(word, correct)
        fixes.append((word, correct))
    return out, fixes


# ── 骨架复读检测（2026-10-18，D-33 层二的**度量**：只记日志、不改正文）───────
# 为什么不用 difflib：实体替换会把相似度压垮 ——
#   `00:32` vs `06:00` = 0.744、`00:32` vs `08:57` = 0.690（都低于主动发言的 0.76 阈值），
#   但三句**去标点后头尾逐字相同**（`又甩这种…链接过来` / `要看你自己看去喵`）。
# 因此按「去标点/空白后的**头部 n 字**与**尾部 m 字**」比对，分两个独立计数，
# 明确"重的是头还是尾"，而不是含糊的"相似"。
# 🔴 刻意**只检测、不改写**：改写要额外一次 LLM 重生成（成本/延迟），
#   先攒基线；本锚点即下一轮是否上"骨架级重生成"的分母。
_SKELETON_STRIP_RE = re.compile(r"[，。！？!?…~、；：:,.!?()（）「」『』“”\"'\s·]+")
_SKELETON_HEAD_N = 8
_SKELETON_TAIL_N = 8


def _is_canned_reply(reply: str) -> bool:
    """出站正文是否来自**兜底/罐头话术池**（而非模型自产）。

    🔴 为什么要单独判（2026-10-19 生产实证，D-38）：
    `[骨架复读]` 上线后第一次命中，样本是这两条 ——
        `本喵这边卡壳了喵，这条没接住，真不是故意晾着你。`（×2，间隔 13 分钟）
    可这**根本不是模型复读**，是 `agent/react._NO_TOOL_FALLBACKS` 里的罐头句：
    池子只有 7 句，上游抖动时反复抽到同一句是数学必然。
    ⇒ 若不排除，**上游一抖，"复读率"就假性飙升**，这个指标就没法用来判断 D-33 了。
       （与 2026-10-08 "3 句池在 4 次抽样下必然重复" 是同一条教训的计量学版本。）

    覆盖：上游全空的三个池（复用 `handler._is_degrade_fallback`，**单一真源**）
    + 工具侧两个池 + 护栏自己的两个改写池 + 泄漏拦截文案。
    """
    t = (reply or "").strip()
    if not t:
        return False
    try:
        from anima.llm.handler import _is_degrade_fallback
        if _is_degrade_fallback(t):
            return True
    except Exception:
        logger.debug("兜底判定导入失败（已隔离）", exc_info=True)
    try:
        from anima.agent.react import _TOOL_FAILED_FALLBACKS, _TOOL_OK_FALLBACKS
        if t in _TOOL_FAILED_FALLBACKS or t in _TOOL_OK_FALLBACKS:
            return True
    except Exception:
        logger.debug("工具兜底池导入失败（已隔离）", exc_info=True)
    return t in _MECHANICAL_REWRITES or t in _ANALYSIS_ESSAY_FALLBACKS \
        or t == "（这条回复被护栏拦截了）"


def skeleton_parts(text: str, *, head_n: int = _SKELETON_HEAD_N,
                   tail_n: int = _SKELETON_TAIL_N) -> tuple[str, str]:
    """返回 `(骨架头, 骨架尾)`；短句返回 `(去标点全文, 去标点全文)`。"""
    body = _SKELETON_STRIP_RE.sub("", text or "")
    if len(body) <= head_n + tail_n:
        return body, body
    return body[:head_n], body[-tail_n:]


def skeleton_duplicates(reply: str, prior_lines: list[str]) -> dict[str, list[str]]:
    """与既往自身发言的骨架重合：`{"head": [...], "tail": [...]}`。"""
    head, tail = skeleton_parts(reply)
    out: dict[str, list[str]] = {"head": [], "tail": []}
    if not head:
        return out
    for line in prior_lines or []:
        o_head, o_tail = skeleton_parts(line)
        if not o_head:
            continue
        if head == o_head:
            out["head"].append(line)
        if tail == o_tail:
            out["tail"].append(line)
    return out


def _strip_tool_failure_narration(reply: str) -> str:
    """只删掉**播报故障的句子**，其余原样保留。

    🔴 为什么不做整段替换：生产那条样本的后半句 ——「雨萌你今天怎么这个点还在
    冒泡，一上来就支使猫猫去搜这种奇奇怪怪的癖好图啊！要看让亦萱自己去翻啦，
    本喵才不帮你们代购！」—— 是有性格的接话，跟故障无关。整段替换会把它一起
    抹掉，这正是 09-27 分析体护栏误杀的教训（破坏性改写连内容一起丢）。
    """
    parts = [p for p in _SENT_SPLIT_RE.split(reply or "") if p and p.strip()]
    keep = [p for p in parts if not _narrates_tool_failure(p, [{"ok": False}])]
    return "".join(keep).strip()


def _tool_failure_owner_notice(ctx: PipelineContext, trace_failed: list[str]) -> str:
    """给 owner 的私聊回执：说清哪个工具挂了、群里怎么处理的。"""
    gid = ctx.group_id if ctx.detail_type == "group" else "-"
    return (
        f"[工具故障] 群 {gid}（trace={ctx.meta.get('trace', '-')}）本回合工具失败："
        f"{'、'.join(trace_failed) or '未知'}。"
        f"已改用兜底话术，未向群内播报故障细节。原文：{(ctx.reply or '')[:80]}"
    )


def _tool_failure_fallback(user_text: str) -> str:
    if re.search(r"内存|磁盘|cpu|负载|占用|状态", user_text or "", re.I):
        return "本喵这次没拿到完整的运行数据，不想瞎报数字；等查到准信再告诉你喵。"
    return "本喵这次没查到准信，不想乱编；换个问法或过会儿再试试喵。"


def _runtime_status_reply(tool_trace: object) -> str:
    """状态工具已成功时，保留事实数据而不是被泄漏护栏误改为空失败。"""
    if not isinstance(tool_trace, list):
        return ""
    for item in reversed(tool_trace):
        if not isinstance(item, dict) or item.get("name") != "get_runtime_status":
            continue
        if not item.get("ok"):
            return ""
        result = str(item.get("result") or "").strip()
        if not result.startswith("当前运行状态（只读）："):
            return ""
        data = result.removeprefix("当前运行状态（只读）：").strip()
        if data:
            return "本喵查到啦，当前是这样：\n" + data
    return ""


# 破角色「分析体」兜底
_ANALYSIS_ESSAY_RE = re.compile(
    r"(从.{0,10}?(角度|视角|来看)"
    r"|结构化解剖|语义聚类|情感分析|数据分析"
    r"|政治社会学|传播学|实证科学|统计学信度|分类学评估"
    r"|综上所述|主流解析|逻辑推导|间接证据|艺术设定|官方设定"
    r"|严谨的逻辑)",
    re.IGNORECASE,
)
# 2026-09-17（R16 男娘人设）：从词表摘除「生理性别 / 非二元性别 / Non-binary」。
# 它们原本是「考据体」信号，但在 R16 人设下 bot 正常聊性别/男娘话题也会命中，
# 导致整段被改写为「本喵刚又跑偏成分析模式了」—— 与新人设直接冲突。
# 真正的考据体仍被兜住：上述其余词（官方设定 / 严谨的逻辑 / 主流解析 / 综上所述）
# 配合 _ESSAY_STRUCT_RE 结构词即可命中，回归见 tests/test_guardrail_analysis.py
# 的 ACADEMIC_GENDER_ESSAY 用例（仍期望 True）。
_ESSAY_STRUCT_RE = re.compile(
    r"(第一[、，. ]|第二[、，. ]|一是|二是|1[.、) ]|2[.、) ]|\n\d+[.、) ]|\*\*「)"
)
#: 「用户要一份**展开**的回答」意图（词表口径，2026-10-08 扩充）。
#:
#: 🔴 原表只有 `解析|分析|详细说说|展开讲|怎么评价|考据` —— 用 16 个**现实说法**实测
#: **漏网 11 个（69%）**：「详细讲讲 / 展开说说 / 多说点 / 多讲讲 / 细说一下 /
#: 介绍一下 / 多说几句 / 讲详细点 / 详细一点 / 展开说说吧 / 再多说点」全部漏网。
#: 漏网后果不是"少豁免"这么轻 —— 2026-10-08 的长度预算以"用户没说要详细"为注入条件，
#: 判据漏网 ⇒ **用户明确要详细，反而被压成短答**（实测：人格 + ≤150 字时用户说
#: 「请详细讲讲，越多越好」，模型仍只写 121 字）。
#: ⇒ 按既有纪律「修漏网优先**补词表**而非调阈值」，只加词、不动任何阈值。
#: 🔴 刻意仍**不收**「怎么 / 什么」这类泛疑问词（与 `_USER_WANTS_HOWTO_RE` 同理），
#: 否则等于给所有口语回复开后门。
_USER_WANTS_ANALYSIS_RE = re.compile(
    r"("
    r"解析|分析|考据|怎么评价"
    r"|介绍(?:一下|下)"
    r"|详细(?:讲讲|说说|说|讲|一点|点|些|介绍|说明|解释)"
    r"|展开(?:讲|说)"
    r"|细说|讲详细|讲深入|讲具体"
    r"|多说(?:点|些|几句|一点)|多讲(?:讲|点|些)"
    r")"
)

#: 「用户要的是**做法/答案**」意图（2026-09-27 新增，D5 首批生产样本驱动）。
#:
#: 背景：`essay_samples.jsonl` 首批 2 条生产样本里，「**如何成为杰米高手**」被
#: `struct>=2&long`（≥2 编号分点 + ≥220 字）判成论文体，590 字攻略被**整段替换**
#: 成「本喵还是适合唠嗑…」⇒ 用户明确在要攻略，拿到的是答非所问。
#: 旧豁免只认「解析/分析/详细说说/展开讲/怎么评价/考据」，**认不出 how-to 意图**。
#:
#: 刻意**不含**「怎么 / 什么 / 为什么 / 吗」这类泛疑问 —— 日常寒暄也带，会过度豁免。
#: 只认「方法 / 教程 / 攻略」语义：`如何` 独立成立；`怎么` 必须**紧接做法动词**。
_USER_WANTS_HOWTO_RE = re.compile(
    r"如何"
    r"|怎么(?:样)?(?:才能|才|可以|该)?"
    r"(?:变|成|打|玩|做|用|练|上|过|赢|提升|提高|快速|入门|速成|上手|应对|处理|解决|通关|配|选|搭)"
    r"|教我|教一下|攻略|教程|指南|入门|速成|打法|思路|配装|出装|阵容|连招|连段"
)


def _user_wants_substantive(user_text: str) -> bool:
    """用户是否**明确**要一段实质回答（要解析 / 要做法）。

    这是护栏的「被要求的结构化回答不该被改写」豁免 —— 只有**用户自己**的文本
    才能触发，与「bot 自发跑偏成分析体」是两回事。
    """
    t = user_text or ""
    return bool(_USER_WANTS_ANALYSIS_RE.search(t) or _USER_WANTS_HOWTO_RE.search(t))


#: 「用户**确实在问东西**」（2026-10-04）—— 比 how-to 关键词宽得多。
#:
#: 背景：`guardrail_essay_samples.jsonl` 7 条生产样本**条条是误杀**。根因是
#: 「保留内容」只对 `essay_score` 与 `_user_wants_substantive` 生效，而后者只认
#: 「解析/如何/教我/攻略」这类 **how-to 关键词** ⇒ **日常提问根本不说这些词**
#: ⇒ 实质回答几乎必然被整段销毁。生产原句：
#:   「有带胸口的纸质材料的嘛」「前列腺液都有哪些成分」
#:   「通宵后感觉心慌心悸…怎么办」「（粘贴三国杀技能规则）」
#: ⇒ 这些才是"用户要答案"的真实形态。
#:
#: 🔴 判别刻意**只认用户在问**，不认「bot 跑偏得长」——
#: 用户只发「哈哈哈哈」而 bot 跑去分析这个人，仍属破角色，**必须继续销毁**
#: （`test_break_role_runaway_still_replaced` 钉住）。
_QUESTION_MARK_RE = re.compile(r"[?？]")
#: 🔴 2026-10-05 补「谁」：生产漏网实证 —— 用户原句「这张图里三个人是谁」（**9 字、无标点**）
#: 问的是图片内容，bot 答了 2322 字（东方Project 角色分析，内容其实是对的），
#: 却被整段替换成「本喵还是适合唠嗑」。它同时栽在两处：
#:   ① 疑问词表有「哪」**却没有「谁」** ⇒ `_QUESTION_WORD_RE` 不匹配；
#:   ② 只有 9 字 ⇒ 差 1 字够不到 `_SUBSTANTIVE_INPUT_MIN=10` 的陈述式兜底。
#: ⇒ 补「谁」是**精准修法**；刻意**不降**长度阈值（那会把「今天真的好累啊感觉要猝死」
#: 这类 11 字闲聊误判成提问，等于给破角色开门）。
_QUESTION_WORD_RE = re.compile(
    r"什么|哪些|哪种|哪个|怎么|怎样|如何|为什么|为啥|咋|多少|几个|哪|谁|谁的|是谁|"
    r"能否|能不能|是不是|有没有|要不要|可不可以|怎么办|怎么回事|多久"
)
#: 🔴 2026-10-05 补**动词式询问**（生产实证第 9 条）：用户 @bot 问「查询势赵云技能」
#: （**7 字**、无标点、无疑问词），bot 查官网答了 298 字（内容正确），
#: 被整段换成「换个话头吧，本喵想听点轻松的喵。」
#:
#: 🔴 **r17 的教训**：补「谁」只修好了「谁」那**一个词**，「查询」这种同构的
#: **动词式询问**（要信息，不是聊天）仍全漏 ⇒ 补词不等于修好一类。
#:
#: 🔴 **收词边界（关键）**：只收**动宾/固定搭配**形态
#: （`查询` / `查一下` / `介绍一下`），**不收单个泛义动词**
#: （`找` / `看看` / `说说`）—— 后者在闲聊里极常见（「我昨天找了一下午的钥匙」）
#: 收进来等于给破角色开门（与 `_USER_WANTS_HOWTO_RE` 刻意不收「怎么/什么」同理）。
#: 判据 `test_verb_like_chatter_is_not_question` 逐条钉住这个边界。
_VERB_QUERY_RE = re.compile(
    r"查询|查一下|查查|查下|搜一下|搜下|搜索一下|搜索|"
    r"介绍一下|介绍下|介绍一下|了解一下|了解下|讲讲|讲一下|"
    r"分析一下|分析下|评价一下|评价下|对比一下|对比下|科普一下|科普下|科普"
)
#: 句末疑问语气词。刻意**不含「吧」**（「那行吧」「好吧」在闲聊里太常见，收进来等于
#: 给所有口语回复开后门 —— 与 `_USER_WANTS_HOWTO_RE` 刻意不收「怎么/什么」同理）。
_QUESTION_PARTICLE_RE = re.compile(r"(?:吗|嘛)[？?]?$")
#: 陈述式提问的下限：把规则/报错/需求**整段贴过来**问「这是什么意思」，既没有问号也
#: 没有疑问词（生产样本 ⑦ 就是这种）。短于此 ⇒ 视为寒暄/情绪表达。
#:
#: 🔴 2026-10-05 从 10 提到 14：原值 10 会把「今天真的好累啊感觉要猝死」（11 字）
#: 「这个游戏我还真的挺喜欢的」（12 字）这类**纯闲聊**判成"在问东西" ⇒
#: 用户只是抱怨/闲聊、bot 却跑偏成长分析体时，r16 会**放行**（真阳性漏网）。
#: 而"整段贴规则来问"的真实样本是 36 / 47 字，离 14 还有大量余量 ⇒ 提到 14 是安全的。
#: 实证：`_SUBSTANTIVE_INPUT_MIN=14` 时，生产 4 条误杀原句（11/10/36/47 字）里
#: 11 与 10 那两条改由**疑问词/句末语气词**路径识别，不依赖长度兜底。
_SUBSTANTIVE_INPUT_MIN = 14
#: 疑问词路径的长度下限。
#:
#: 🔴 这条**不是**调参，是从既有判据 `test_guardrail_rewrites_analysis_essay` 学的：
#: 用户只打「怎么说」（3 字）而 bot 输出一篇「政治社会学 / 实证科学」的长分析体
#: ——**「用户极短 + bot 极长」正是破角色跑偏的典型特征**。而实质提问天然有长度
#: （生产四条误杀原句分别是 11 / 10 / 36 / 47 字）。
#: ⇒ 低于此长度的疑问词**不足以**判为"在问东西"，仍走销毁分支。
_QUESTION_MIN_LEN = 4


def _user_asked_question(user_text: str) -> bool:
    """用户本轮是否**确实在问东西 / 递了实质需求**（而不是寒暄、感叹、玩梗）。

    用于护栏 essay 路径的处置判别：**问了就保留内容**，别把答案整段换成罐头话。
    """
    t = (user_text or "").strip()
    if not t:
        return False
    if _VERB_QUERY_RE.search(t):
        # 🔴 动词式询问（`查询势赵云技能`）**不受 `_QUESTION_MIN_LEN` 限制**：
        # 这些词本身就是「索取信息」的信号，长度不改变语义
        # （「查一下」3 字与「查询一下国战里势赵云怎么出牌」同义）。
        return True
    if len(t) >= _QUESTION_MIN_LEN and (
        _QUESTION_MARK_RE.search(t) or _QUESTION_WORD_RE.search(t)
        or _QUESTION_PARTICLE_RE.search(t)
    ):
        return True
    return len(t) >= _SUBSTANTIVE_INPUT_MIN


#: 切点可吸附的边界（2026-10-05）：中文句读 + 英文句读 + 换行 + 列表项起始。
#: 🔴 刻意**不含**逗号、顿号 —— 「基本全是这几首循环播放，听了」切在逗号后
#: 仍是半句（生产实证），逗号只是句内停顿。
_TRUNCATE_BOUNDARY_CHARS = frozenset("。！？!?；;：:\n")


def _truncate_at_boundary(text: str, limit: int) -> str:
    """把超长回复截到 `limit` 字以内，且**切点落在句读边界**。

    ## 为什么
    原实现 `text[:limit].rstrip("，,。；;、 ") + "…"` 是**纯字符数切** ⇒
    生产上出现过「…基本全是这几首循环播放，听了…」（10-04 21:56），用户看到半句话。

    ## 契约（三条，硬约束）
    1. **长度上限不放宽**：结果恒 `≤ limit + 1`（省略号占 1）。
    2. **找不到边界就退回原行为**（纯字符切）—— 不制造新失败模式。
    3. **回退不得过半**：切点回退超过一半 ⇒ 视为边界无效，退回字符切
       （否则「嗯。+ 500 字无标点」会被砍成「嗯。」）。

    🔴 刻意**不含逗号/顿号**：它们是句内停顿，切在那里仍是半句。
    """
    t = text or ""
    if len(t) <= limit or limit <= 0:
        return t

    head = t[:limit]
    # 回退窗口 = 后一半（不含首字符，避免退到 0）。
    # 🔴 **正序**扫描取**最靠前**的边界（句子最完整）—— 反向扫描会拿到最靠后的，
    #    结果是「第一句。第二句|」而不是「第一句|」，实测踩过。
    floor = max(1, limit // 2)
    cut = -1
    for i in range(floor, len(head)):
        if head[i] in _TRUNCATE_BOUNDARY_CHARS:
            cut = i
            break
    if cut < 0:
        # 无句读：列表项起始也算一个（行首 "- " / "* "）
        for i in range(floor, len(head)):
            if head[i] == "\n" or (i > 0 and head[i] in "-*" and head[i - 1] == "\n"):
                cut = i
                break
    if cut < 0:
        return head.rstrip("，,。；;、 ") + "…"
    # 🔴 `cut+1` 保留边界字符本身（句号/问号）—— 少了它就是「你要是嫌长就当本喵在打哈欠好了…」，
    #    句号被吃掉仍然读着像半句。实测踩过。
    return head[:cut + 1].rstrip() + "…"

#: 「单语义词 + 长文」路径的**强标记**（2026-09-23 新增）。
#: 语义词表里两类词的噪声不同：「从X角度看 / 数据分析 / 官方设定」在 ACG 与日常
#: 闲聊里都是常见说法（生产实测把它们连同 250 字正文一起判成论文体），而下面这些
#: 词几乎只出现在真正的分析文里 ⇒ 只有它们才够格**单独**触发。
_ANALYSIS_ESSAY_STRONG_RE = re.compile(
    r"(综上所述|主流解析|逻辑推导|结构化解剖|语义聚类|严谨的逻辑|实证科学"
    r"|统计学信度|分类学评估|政治社会学|传播学)"
)

# ── 「结构 + 抽象密度」评分（2026-09-23 第二轮，取代闭集词表做主力）────────────
# 背景：同日上午那版把单语义词降级、改用 `_ANALYSIS_ESSAY_STRONG_RE` 强词表 —— 那
# **又是一个闭集语义词表**。用 3 天 356 条生产真实出站原文回测：
#   * 判据命中 **0** 条，长尾（>=120 字）9 条全漏，其中 3 条是教科书级剖析体；
#   * 生产实际在用的词（底层逻辑 / 本质是 / 核心诉求 / 认知水平 / 因果支配 /
#     集合论 / 收束点）在强词表里**一个都不含** —— 与 09-20 已修掉的毛病同类。
# ⇒ 换成**结构信号**（定义式句式）+ **抽象名词** + **书面连接词**打分；词表退为加权项，
#   不再是唯一闸门。这才能覆盖模型换说法的情形。
#
# 3 天真实数据上的分离度（含阈值定标依据）：
#   命中 4 条 = 6.5 / 8.5 / 15.0 / 18.5；>=100 字**未命中**样本的上界仅 **3.5**
#   ⇒ 阈值 6.0 两侧 margin 3.0，可干净切开。
# 🔴 长度门槛（>=100 字）**不可省**：91 字的「集合论」条与 87 字的「游戏王特召」条
#   分数达 9.0 / 6.5，但两者都有正当的对话目的且更短 ⇒ 靠长度门槛排除，避免误杀。
#   （"长"本身**不等于**"非人"：游戏王规则问答 159 字、安慰长文 230 字分数都只有 0.0，
#    绝不能改成按长度一刀切。）
_ESSAY_DEFINITION_RE = re.compile(
    r"(?:不是[^，。！？；]{1,16}[，,]\s*(?:而是|就是))"
    r"|(?:并非[^，。！？；]{1,14}[，,]\s*(?:而是|而在于|是在于))"
    r"|(?:本质(?:上)?(?:是|在于|不过是|就是))"
    r"|(?:根本上(?:说|讲|是|在于))"
    r"|(?:说到底(?:是|就是|不过是))"
    r"|(?:的核心(?:诉求|逻辑|分歧|目标|矛盾|问题|机制|原因|本质))"
    r"|(?:注定只是)"
    r"|(?:本身就(?:是|意味着))"
)
_ESSAY_ABSTRACT_RE = re.compile(
    r"本质|逻辑|维度|认知水平|认知|容错率|容错|期望值|诉求|分歧|闭环|收束|"
    r"支配|叙事|阈值|结构性|不确定性|社会化|归因|范畴|子集|全集|集合论|"
    r"因果关系|因果|机制|博弈|范式|语境|前提|变量"
)
#: 只收**书面连接结构**，不收「而 / 其 / 则」这类单字（它们在正常中文里到处都是，
#: 会把分数推到噪声区）。
_ESSAY_CONNECTIVE_RE = re.compile(
    r"对于.{0,10}而言|确实体现|客观上|主观上|在.{0,8}(?:层面|维度|意义上)|"
    r"从.{0,10}(?:角度|视角|层面|逻辑)(?:来|上)|"
    r"换言之|进一步(?:说|讲)|反之|由此可见"
)

#: 评分路径的阈值与长度门槛（定标依据见上方注释）。
_ESSAY_SCORE_MIN = 6.0
_ESSAY_SCORE_MIN_LEN = 100


def _essay_score(reply: str) -> float:
    """「结构 + 抽象密度」评分（越高越像破角色分析文）。

    权重：定义式句式 2.0 / 抽象名词 1.5 / 书面连接词 0.5 —— 定义式句式最稀有的，
    因此权重最高；抽象名词次之；连接词最易得，只作微调。
    """
    reply = reply or ""
    return (
        len(_ESSAY_DEFINITION_RE.findall(reply)) * 2.0
        + len(_ESSAY_ABSTRACT_RE.findall(reply)) * 1.5
        + len(_ESSAY_CONNECTIVE_RE.findall(reply)) * 0.5
    )


def _analysis_essay_rule(reply: str, *, user_text: str = "") -> str | None:
    """判定是否为「破角色分析体」，命中则返回**规则名**（放行返回 None）。

    规则名用于改写点的日志/统计（2026-09-23 补观测）：此前只剩一行 WARNING、
    不留原文，事后既无法回溯「它本来想说什么」，也无法按规则统计收敛情况。

    规则（按短路顺序）：
      user_wants_long  —— 用户自己要求分析，且回复 >700 字并达到强信号门槛
      struct>=2&long   —— ≥2 个编号分点 + ≥220 字（论文体骨架，不依赖语义词表）
      keys>=2          —— ≥2 个语义词
      struct>=1        —— 1 个语义词 + 编号分点
      single_key_strong—— 1 个语义词 + >220 字 + **强标记**（口语不会出现的词）
      essay_score      —— ≥100 字 + 「结构 + 抽象密度」评分 ≥6.0（**不依赖词表**）

    2026-09-23（第二轮）：新增 `essay_score` 作为**纯增量**路径 —— 旧路径一条未改，
    只在 `if not keys: return None` 这条短路**之前**插入一次早退，专门捞上面几条
    词表路径的漏网（模型的表述一旦离开词表，旧实现直接放行）。
    """
    reply = reply or ""
    keys = _ANALYSIS_ESSAY_RE.findall(reply)
    n = len(keys)
    struct_hits = len(_ESSAY_STRUCT_RE.findall(reply))
    user_wants = _user_wants_substantive(user_text)
    if user_wants:
        if len(reply) > 700 and (n >= 3 or struct_hits >= 1):
            return "user_wants_long"
        return None
    # 两个以上编号分点 + 长正文 = 论文体骨架（不依赖语义词表是否命中）
    if struct_hits >= 2 and len(reply) >= 220:
        return "struct>=2&long"
    # 2026-09-23：**不依赖词表**的结构评分路径。必须放在 `if not keys` 之前，
    # 否则「词表一个都没中」的长分析文会被那条短路直接放行（这正是生产实测
    # 命中 0 条的原因）。纯增量：旧路径顺序与语义完全不变。
    if len(reply) >= _ESSAY_SCORE_MIN_LEN and _essay_score(reply) >= _ESSAY_SCORE_MIN:
        return "essay_score"
    if not keys:
        return None
    if n >= 2:
        return "keys>=2"
    if struct_hits:
        return "struct>=1"
    # 2026-09-23：**单语义词不再单独构成论文体**。旧口径 `len(reply) > 220` 即整段
    # 报废，而词表含口语常见词 ⇒ 生产实测把「守望先锋英雄数值讨论（入站 112 字）」
    # 与 250+ 字的正常接话整段替换成罐头话术（用户观感＝「答到一半不答了」）。
    # 现在单信号路径须命中**强标记**（见 _ANALYSIS_ESSAY_STRONG_RE）。
    if len(reply) > 220 and _ANALYSIS_ESSAY_STRONG_RE.search(reply):
        return "single_key_strong"
    return None


def _looks_like_analysis_essay(reply: str, *, user_text: str = "") -> bool:
    """判断回复是否跑偏成「分析工具/论文体」。

    2026-09-20 深审补漏：`_ANALYSIS_ESSAY_RE` 是**闭集语义词表**（"从X角度看 /
    综上所述 / 官方设定"…）。模型换一套说法（"社区治理 / 公关层面 / 流量归因 /
    风险隔离"）就一个都不中 ⇒ 原实现的 `if not keys: return False` 直接放行。
    生产实测：用户只**粘贴了一段贴吧长文、没有提问**，bot 回了
    「1. 溯源与确权… 2. 流量归因与风险隔离… 3. 分流与共存方案…」——骨架是
    教科书式论文体，却因为词表没中而漏网。

    ⇒ 让**结构词（编号分点）独立生效**，不再被语义词表前置短路；同时保留
    「用户自己要求分析」的豁免（那种长分点回复是用户要的，不该改写）。

    2026-09-23 反向补漏：闭集词表除了**漏检**，还会**误检**——单语义词 +
    长正文曾经足以整段报废一条正常回复。判据现在是「多信号或强标记」，
    见 `_analysis_essay_rule`（本函数是其布尔包装，返回值类型保持 bool）。

    2026-09-23（第二轮）**再补漏**：上面那版"强标记"仍是闭集词表，用 3 天 356 条
    生产真实出站回测 → 命中 0 条、长尾 9 条全漏。故新增 `essay_score` 结构评分
    路径（不依赖词表），详见模块内 `_ESSAY_*` 常量处的定标依据。
    """
    return _analysis_essay_rule(reply, user_text=user_text) is not None


# 政治敏感词表（高精度专名级；2026-09-11 owner 指示：此类门槛只拦政治内容，
# 不再用启发式猜测「跑偏」——后者曾误拦严肃医学追问「双低会死吗」）
_POLITICAL_RE = re.compile(
    r"(习近平|李克强|李强|温家宝|胡锦涛|毛泽东|邓小平|江泽民|周恩来|"
    r"中共|共产党|国民党|民进党|政府朝鲜|六四|天安门事件|文革|文化大革命|"
    r"台独|港独|藏独|疆独|法轮功|习近平连任|总书记|国务院总理|"
    r"台湾独立|西藏独立|新疆独立|颜色革命|政变|镇压|屠杀)"
, re.I)


# 用户未提及时禁止主动灌入的旧梗
_UNPROMPTED_FIXATION_RULES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (
        re.compile(r"不叫沙琪玛|叫哈基米[，,]?不叫|沙琪玛是小甜点|本喵叫哈基米"),
        ("沙琪玛", "沙其玛", "莎琪玛", "sakima", "谁", "你是谁", "你叫啥", "你叫什么", "名字"),
    ),
    (
        re.compile(r"小鱼干|一百个小鱼干|欠本喵"),
        ("小鱼干", "鱼干", "罐头", "债务", "欠"),
    ),
    (
        re.compile(r"朗姆酒|沙雕与朗姆|地狱笑话"),
        ("朗姆", "沙雕", "bilibili", "b23", "BV", "bv", "空间", "主页", "视频", "冰山"),
    ),
]


def _looks_like_unprompted_fixation(user_text: str, reply: str) -> bool:
    """回复里硬塞用户本句未提及的固定梗 → 乱回复/一直提起。"""
    u = (user_text or "").lower()
    r = reply or ""
    if not r or len(r) < 8:
        return False
    for pattern, allow_if in _UNPROMPTED_FIXATION_RULES:
        if not pattern.search(r):
            continue
        if any(a.lower() in u for a in allow_if):
            continue
        return True
    return False


def _strip_fixation_sentences(reply: str) -> str:
    """剥离命中旧梗模式的句子，保留其余内容（整段罐头改写会答非所问）。"""
    parts = re.split(r"(?<=[。！？!?\n])", reply or "")
    kept = [p for p in parts if p.strip() and not any(
        pat.search(p) for pat, _ in _UNPROMPTED_FIXATION_RULES)]
    return "".join(kept).strip()


async def _record_reply_memory(ctx: PipelineContext) -> None:
    """持久化最终（已过护栏）回复。"""
    if not ctx.reply:
        return
    from anima.memory.chat_memory import get_chat_memory
    memory = get_chat_memory()
    chat_id = ctx.meta.get("chat_id")
    if chat_id:
        memory.add(
            ctx.detail_type, chat_id, "bot", "Anima", ctx.reply,
            user_id=ctx.user_id,
        )
        if ctx.detail_type == "group" and ctx.group_id is not None:
            memory.observe_group_bot_reply(ctx.group_id, ctx.reply)

    # 写入向量记忆（bot 回复）
    core = ctx.meta.get("core")
    if core and core.vector_memory and ctx.reply:
        try:
            await core.vector_memory.add(
                text=ctx.reply,
                meta={"role": "bot", "name": "Anima", "chat_type": ctx.detail_type, "chat_id": chat_id},
                salience=0.4,
                source_uin=ctx.user_id,
                source_gid=ctx.group_id,
            )
        except Exception:
            logger.exception("向量记忆写入失败")


async def _run_danger_behaviors(ctx: "PipelineContext") -> None:
    """调度 danger 层各行为。"""
    core = ctx.meta.get("core")
    if core is None:
        return
    cfg = getattr(core, "config", None)
    dcfg = getattr(cfg, "danger", None) if cfg else None
    if not dcfg or not getattr(dcfg, "enabled", False):
        return
    from anima.cognitive.danger import get_danger_behaviors
    danger = get_danger_behaviors(core=core, cfg=dcfg)
    if danger is None:
        return
    danger.identity_crisis_recover()
    # 对方示意结束（转私聊 / 别说了 / 不聊了 / 晚安…）时，不生成「还想追问」的欲望，
    # 避免别人不想聊时 bot 在心跳期又冒出问题 —— 源头减少主动开口。
    _skip_info_collection = False
    try:
        from anima.llm.group_gate import is_conversation_end_signal
        if is_conversation_end_signal(getattr(ctx, "text", "") or ""):
            _skip_info_collection = True
    except Exception:
        pass
    if not _skip_info_collection:
        asyncio.create_task(
            danger.maybe_active_info_collection(ctx), name="danger_info_collection"
        ).add_done_callback(_bg_task_done)
    asyncio.create_task(danger.maybe_relationship_inference(ctx, ctx.reply or ""), name="danger_relation_inference").add_done_callback(_bg_task_done)
    asyncio.create_task(danger.maybe_memory_infection(ctx), name="danger_memory_infection").add_done_callback(_bg_task_done)
    asyncio.create_task(danger.maybe_autonomous_web(ctx), name="danger_autonomous_web").add_done_callback(_bg_task_done)
    danger.identity_crisis_update(recent_negative_scar=_detect_hurtful(ctx.text or ""))


async def guardrail_stage(ctx: PipelineContext) -> StageResult:
    """输出护栏 —— 过滤危险/机械回复。"""
    ctx.reply = _clean_transport_text(ctx.reply)
    ctx.reply = _strip_speaker_labels(ctx.reply)
    # 🔴 2026-10-25：**emoji / 颜文字出站兜底**。
    #
    # 人格正本这回补了「不用 emoji，也不用符号画的表情」，但那是 prompt 层的
    # 软约束 —— 本项目反复验证过「prompt 层约束会被无视」（时段词纠偏就是因为
    # 模型不听 prompt 才做的第二道闸，见 `fix_period_words` 注释）。
    #
    # 生产实证（10-25 13:51）：出站实测
    #   「才不是什么量产的笨蛋模型呢~ (￣^￣)」
    # 那是颜文字，不是 emoji —— 光禁 emoji 拦不住，故两类都处理。
    # 放在所有改写之前：先清掉装饰，后续规则（骨架/字数）才基于真实正文判断。
    try:
        _before = ctx.reply or ""
        ctx.reply, _deco = strip_kinetic_decorations(_before)
        if _deco:
            logger.info("[装饰剥离] 出站含 emoji/颜文字 %d 处 → 已清", _deco)
    except Exception:
        logger.debug("装饰剥离失败（已隔离）", exc_info=True)
    # 时段词纠偏（见 `_PERIOD_WORD_WINDOWS` 注释）：模型不听 prompt 的第二道闸。
    # 放在所有改写之前 —— 后面几条分支可能整段替换，替换后就不需要再纠。
    try:
        ctx.reply, _period_fixes = fix_period_words(ctx.reply)
        for _bad, _good in _period_fixes:
            logger.warning(
                "[时段纠偏] 出站时段词与真实时间不符：%s→%s", _bad, _good)
    except Exception:
        logger.debug("时段词纠偏失败（已隔离）", exc_info=True)

    # 骨架复读检测（只读日志，不改正文）：见 `skeleton_parts` 上方注释。
    # 放在所有改写之前 —— 要度量的是**模型产出的骨架**，不是护栏改写后的。
    # 🔴 兜底/罐头话术**不计入**（D-38）：它们来自固定池，抽到同一句是必然，
    # 拿来当"复读"会让上游抖动直接污染指标。
    try:
        if (ctx.detail_type == "group" and ctx.group_id
                and len(ctx.reply or "") >= 12
                and not _is_canned_reply(ctx.reply)):
            from anima.memory.chat_memory import get_chat_memory
            _prior = get_chat_memory().recent_bot_lines(int(ctx.group_id), limit=8)
            _dup = skeleton_duplicates(ctx.reply, _prior)
            if _dup["head"] or _dup["tail"]:
                logger.warning(
                    "[骨架复读] head=%d tail=%d 样本=%s",
                    len(_dup["head"]), len(_dup["tail"]),
                    (ctx.reply or "")[:60],
                )
    except Exception:
        logger.debug("骨架复读检测失败（已隔离）", exc_info=True)

    if not ctx.reply and ctx.sticker_to_send:
        ctx.reply = "喏，给你。"
    if not ctx.reply:
        return StageResult.SKIP_REPLY

    reply_lower = ctx.reply.lower()

    # 危险内容检查
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in reply_lower:
            logger.warning("Guardrail: 检测到危险模式 %s", pattern)
            _record_fallback(ctx, "leak_blocked")
            ctx.reply = "（这条回复被护栏拦截了）"
            await _record_reply_memory(ctx)
            return StageResult.CONTINUE

    # 机械回复检查
    for phrase in BANNED_PHRASES:
        if phrase.lower() in reply_lower:
            logger.warning("Guardrail: 检测到机械回复 '%s'", phrase)
            _record_fallback(ctx, "mechanical")
            ctx.reply = random.choice(_MECHANICAL_REWRITES)
            await _record_reply_memory(ctx)
            return StageResult.CONTINUE
    if _BANNED_RE.search(reply_lower):
        logger.warning("Guardrail: 检测到机械回复(作为…AI 变体)")
        _record_fallback(ctx, "mechanical")
        ctx.reply = random.choice(_MECHANICAL_REWRITES)
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 内部工具/权限泄漏
    if _has_internal_tool_leak(ctx.reply):
        logger.warning("Guardrail: 检测到内部工具/权限泄漏，改写为安全失败话术")
        ctx.reply = _runtime_status_reply(ctx.meta.get("tool_trace")) or _tool_failure_fallback(ctx.text or "")
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # r28 P1-2：工具故障播报 —— 群里只给兜底话术，故障细节走私聊给 owner。
    # 只在群聊生效：私聊对象多半就是 owner，如实说明故障是有价值的。
    _tool_trace = ctx.meta.get("tool_trace")
    if ctx.detail_type == "group" and _narrates_tool_failure(ctx.reply, _tool_trace):
        _failed = [str(t.get("name")) for t in (_tool_trace or [])
                   if isinstance(t, dict) and not t.get("ok")]
        logger.warning(
            "Guardrail: 回复在播报工具故障（%s），群内改写为兜底话术、细节转 owner 私聊",
            "、".join(_failed) or "未知",
        )
        ctx.meta["owner_notice"] = _tool_failure_owner_notice(ctx, _failed)
        _record_fallback(ctx, "tool_failure_narration")
        # 先试着只切掉故障句；切完太短（说明整条都在播报）才整条换兜底话术。
        _stripped = _strip_tool_failure_narration(ctx.reply)
        ctx.reply = (_stripped if len(_stripped) >= 8
                     else _runtime_status_reply(_tool_trace) or _tool_failure_fallback(ctx.text or ""))
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 破角色「分析体」兜底
    _essay_rule = _analysis_essay_rule(ctx.reply, user_text=ctx.text or "")
    if _essay_rule:
        # 2026-09-23：改写是**破坏性**的（整段替换），必须留下「命中哪条规则 +
        # 原文多长 + 原文开头」——否则事后既回溯不了「它本来想说什么」，
        # 也判断不了某条规则是过严还是有效（这正是上次没发现误杀的原因）。
        _essay_orig = ctx.reply or ""
        # 🔴 2026-10-07：日志文案必须与**实际处置**一致。此前无条件写
        # 「改写为轻松接话」，而 essay_score / 用户在提问 走的是**保留内容**
        # （只截断收尾、不整段替换，见下方 2026-09-27/10-04 注释）⇒ 排查时
        # 把「保留」误读成「整段替换」，会得出错误的命中率与误杀结论。
        _essay_keep = (
            _essay_rule == "essay_score"
            or _user_wants_substantive(ctx.text or "")
            or _user_asked_question(ctx.text or "")
        )
        logger.warning(
            "Guardrail: 检测到破角色分析体（规则=%s 原文=%d字 处置=%s 摘要=%s）",
            _essay_rule, len(_essay_orig),
            "保留内容+截断收尾" if _essay_keep else "整段替换为轻松接话",
            _essay_orig[:120],
        )
        if _essay_keep:
            # 保留内容：只截断、不整段替换。
            # 🔴 2026-09-27：`essay_score` 走这里（原本是整段罐头替换）。
            # 依据 D5 首批生产样本：真阳性「三国杀机制分析」205 字 score **6.5**，
            # 假阳性「斯旺闲聊」180 字 score 也**正好 6.5**，且两者
            # (定义式 2.0 / 抽象词 4.5 / 连接词 0.0) **逐项相同** ⇒ 该路径是纯
            # 数值启发式，**在结构上与正常闲聊不可分离**，调阈值必然二选一。
            # 因此不动阈值、不动规则名，只把「代价」从「整段答非所问」降为
            # 「稍长一点」——真阳性仍被识别、仍落样本，可继续攒数据再定标。
            # 🔴 2026-10-04：条件再扩到「用户确实在问东西」
            # （`_user_asked_question`）—— 7 条生产样本里 4 条的用户原句
            # （纸质胸牌 / 前列腺液成分 / 通宵心慌 / 贴规则）**都不含 how-to 关键词**
            # ⇒ 此前必然被整段销毁，其中一条把**急救步骤**换成了客套话。
            # 判据、规则名、样本落盘**一律不动**；真·破角色跑偏（用户只发「哈哈哈哈」）
            # 仍走下面的销毁分支。
            short = ctx.reply.strip()
            if len(short) > 280:
                short = short[:280].rstrip("，,。；;、 ") + "…"
            if "喵" not in short[-12:]:
                short = short.rstrip("。.!！") + "，大概就这样喵。"
            ctx.reply = short
        else:
            _record_fallback(ctx, "analysis_essay")
            ctx.reply = random.choice(_ANALYSIS_ESSAY_FALLBACKS)
        # D5 埋点：完整原文 + 改写结果一并落盘（回测判据用；fail-soft）。
        _sample_guardrail_essay(ctx, _essay_rule, _essay_orig, ctx.reply)
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # 模糊输入跑偏兜底

    # ── 政治敏感内容门槛（2026-09-11 owner 指示：此类门槛只拦政治）──
    # 平台会对政治内容封号，bot 输出命中高精度政治词表时人格化回避。
    # 词表刻意保持高精度（专名级），避免误伤正常聊天。
    try:
        if _POLITICAL_RE.search(ctx.reply or ""):
            logger.warning("Guardrail: 回复命中政治敏感词，改写为话题回避")
            _record_fallback(ctx, "political")
            ctx.reply = "这个话题本喵不碰喵～聊点别的吧！"
    except Exception:
        pass

    # 未提示却硬塞旧梗
    if _looks_like_unprompted_fixation(ctx.text or "", ctx.reply or ""):
        # 体验测试 B3：整段罐头改写会让用户的问题得到答非所问的回复。
        # 改为剥离命中旧梗的句子、保留其余内容；剥无可剥才用切题短兜底。
        _stripped = _strip_fixation_sentences(ctx.reply or "")
        if len(_stripped) >= 8:
            logger.warning("Guardrail: 未提示却塞入固定旧梗，已剥离旧梗句保留其余回答")
            ctx.reply = _stripped
        else:
            logger.warning("Guardrail: 未提示却塞入固定旧梗且剥离后无内容，改写为短接话")
            ctx.reply = "本喵刚才想到别的去了，直接说重点喵。"
        try:
            # M0.1 Commit 3c-1a (Telemetry/T1)：经 Facade 表面记录 guard fixation，
            # 不再经 meta["core"] 抓整个 core。ctx.core 由 handler 无条件注入；
            # 缺失时静默失败不破坏回复（兼容未迁移测试与 flag=false 探针环境）。
            ctx.core.telemetry().record_guard_fixation()
        except Exception:
            logger.debug("fixation telemetry failed", exc_info=True)
        await _record_reply_memory(ctx)
        return StageResult.CONTINUE

    # C1: 情绪→回复长度约束
    core = ctx.meta.get("core")
    try:
        from anima.cognitive.mood import get_mood_system
        _data_dir_c1 = getattr(getattr(core, "config", None), "persistence", None)
        _data_dir_c1 = getattr(_data_dir_c1, "data_dir", "data") if _data_dir_c1 else "data"
        _mood_label_c1 = get_mood_system(_data_dir_c1).get_state().label
        # 2026-09-21：上限抬到「装得下一段解释」，并**取消情绪尾巴**。
        # 旧口径（annoyed/tired 120 字 + 硬拼「，算了不说了。」）在用户说
        # 「没看懂，再解释下」时把答案砍半再补一句拒绝语 —— 观感是 bot 不想回答。
        _MOOD_LENGTH_CAP = {
            "annoyed": 220,
            "tired": 180,
            "bored": 120,
            "excited": 300,
            "happy": 300,
        }
        _cap = _MOOD_LENGTH_CAP.get(_mood_label_c1)
        if _cap and len(ctx.reply) > _cap:
            _orig_len_c1 = len(ctx.reply)
            _truncated = ctx.reply[:_cap]
            for sep in ("。", "！", "!", "？", "?", "~", "…", "，", ","):
                _last_sep = _truncated.rfind(sep)
                if _last_sep > _cap * 0.4:
                    _truncated = _truncated[:_last_sep + 1]
                    break
            else:
                _truncated = _truncated.rstrip("，,。；;、 ") + "…"
            # 截断落在逗号/顿号上时剥掉尾部标点、以省略号收尾：只表达"被截断"，
            # 不再注入「算了不说了」这类**拒绝语义**（旧 _MOOD_TAIL 已删除）。
            _body = _truncated.rstrip()
            if not _body.endswith(("。", "！", "!", "？", "?", "~", "…")):
                _body = _body.rstrip("，,、；;。．!！?？~… ")
                _truncated = f"{_body}…" if _body else _truncated
            ctx.reply = _truncated
            logger.info("C1: 情绪[%s]回复长度约束 %d→%d", _mood_label_c1, _orig_len_c1, len(ctx.reply))
    except Exception:
        pass

    # C2: 关系→话题亲密度约束
    try:
        from anima.cognitive.relation import get_relation, Stage as _StageC2
        _rel_c2 = get_relation().get(int(ctx.user_id))
        _reply_c2 = ctx.reply or ""
        if _rel_c2.stage in (_StageC2.STRANGER, _StageC2.ACQUAINTANCE):
            if any(pat in _reply_c2 for pat in ("我记得你之前", "你上次说过", "上次你", "之前你说")):
                ctx.reply = re.sub(
                    r"(我记得你之前说[^。！~]*[。！~]?|你上次说过[^。！~]*[。！~]?|上次你[^。！~]*[。！~]?|之前你说[^。！~]*[。！~]?)",
                    "", ctx.reply,
                ).strip()
    except Exception:
        pass

    # C3: 伤痕→回避行为后处理
    try:
        from anima.cognitive.scar import get_scar_system
        _data_dir_c3 = getattr(getattr(core, "config", None), "persistence", None)
        _data_dir_c3 = getattr(_data_dir_c3, "data_dir", "data") if _data_dir_c3 else "data"
        _scar_sys_c3 = get_scar_system(_data_dir_c3)
        _user_scars_c3 = _scar_sys_c3.get_for_user(int(ctx.user_id))
        _high_scars = [s for s in _user_scars_c3 if s.severity > 0.6]
        if _high_scars:
            _reply_c3 = ctx.reply or ""
            for s in _high_scars:
                for kw in s.trigger_keywords:
                    if kw in _reply_c3:
                        ctx.reply = _reply_c3.replace(kw, "…")
                        if not ctx.reply.startswith(("不想", "算了", "不说")):
                            ctx.reply = f"不太想聊这个…{ctx.reply}"
                        break
    except Exception:
        pass

    # 长度限制
    # 🔴 2026-10-05：切点从**纯字符切**改为**吸附到句读边界**。
    # 生产实证（10-04 21:56）：原实现 `ctx.reply[:420].rstrip(...) + "…"` 切在
    # 「…基本全是这几首循环播放，听了|」⇒ 用户看到半句话。
    # 长度上限**不变**（硬约束），只改**切在哪**。
    #
    # 🔴 2026-10-08：豁免判据原本只认 `_user_wants_substantive`
    # （用户说「解析 / 如何 / 攻略」这类 book 词），而 essay 分支早在 10-04 就已扩到
    # `_user_asked_question`（「用户确实在问东西」）—— **同一个修法漏了这一行**。
    # 生产实证（7d 仅 2 条超长，两条都栽在这）：
    #   * 10-03 19:28 私聊原句「医生给我开了思诺思和文拉法辛和富马酸喹硫平」21 字，
    #     无问号、无疑问词 ⇒ 旧判据 False ⇒ **用药说明**被截到 420 字；
    #   * 10-04 21:56 群内原句「512的能不能压到100mb以前的内存占用」21 字 ⇒ 同因。
    # 两条原句 `_user_asked_question` 均为 True（走 `_SUBSTANTIVE_INPUT_MIN=14`
    # 的陈述式兜底）。
    # 反例护栏：「哈哈哈哈」(4) 与「今天真的好累啊感觉要猝死」(12) 两条仍为 False
    # ⇒ **闲聊/情绪表达不受影响**（正是 `_user_asked_question` 注释里刻意守住的边界）。
    # 🔴 上限本身（900）**一字未动**，只扩大「够格拿 900」的人群 —— 不是放宽长度。
    # 顺序上安全：essay 分支（破角色长文）在本段**之前**且已 return，不会走到这里。
    hard_limit = 900 if (
        _user_wants_substantive(ctx.text or "")
        or _user_asked_question(ctx.text or "")
    ) else 420
    if len(ctx.reply) > hard_limit:
        ctx.reply = _truncate_at_boundary(ctx.reply, hard_limit)
        logger.info("Guardrail: 超长回复截断至 %d 字", len(ctx.reply))

    # A1: 伤痕治愈
    try:
        from anima.cognitive.mood import get_mood_system
        from anima.cognitive.scar import get_scar_system
        _data_dir_heal = getattr(getattr(core, "config", None), "persistence", None)
        _data_dir_heal = getattr(_data_dir_heal, "data_dir", "data") if _data_dir_heal else "data"
        _mood_heal = get_mood_system(_data_dir_heal).get_state()
        if _mood_heal.valence > 0.3:
            _scar_heal = get_scar_system(_data_dir_heal)
            _scar_heal.heal(int(ctx.user_id), amount=0.05)
    except Exception:
        pass

    await _record_reply_memory(ctx)

    # 矛盾检测
    if core is not None:
        cfg = getattr(core, "config", None)
        ccfg = getattr(cfg, "contradiction", None) if cfg else None
        if ccfg and getattr(ccfg, "enabled", False):
            try:
                from anima.cognitive.contradiction import get_contradiction_detector
                detector = get_contradiction_detector(
                    cfg.persistence.data_dir, ccfg
                )
                asyncio.create_task(
                    detector.detect_user_contradiction(
                        getattr(core, "llm", None),
                        getattr(core, "vector_memory", None),
                        ctx.text or "",
                        ctx.user_id,
                        ctx.group_id,
                        ctx.detail_type,
                    ),
                    name="contradiction_detect",
                ).add_done_callback(_bg_task_done)
            except Exception:
                logger.exception("矛盾检测调度失败")

        # 高危自主行为层
        dcfg = getattr(cfg, "danger", None)
        if dcfg and getattr(dcfg, "enabled", False):
            try:
                await _run_danger_behaviors(ctx)
            except Exception:
                logger.exception("danger 行为调度失败")

    return StageResult.CONTINUE
