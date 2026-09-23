# -*- coding: utf-8 -*-
"""承诺判别精度 + 显示名 + 口头语时间 + 论文体补漏（2026-09-20 深审修复）。

背景（两轮出站审计）
--------------------
用户直觉：「bot 完全不理解这些语句的意思」。深挖发现主因不在语义层，而在
**承诺判别吃了裸时间词** —— pipeline 的 `_PROMISE_RE` 与 `memory/episodes.py` 的
`_COMMITMENT_HINT` 各自实现了一份"承诺检测"，两边都把「下次 / 等下 / 今晚 /
到时候」这类词**单独**当成"bot 答应了某事"。于是：

    斗嘴回复「下次再乱喵，本喵真拿尾巴抽你了」
      → 记成承诺 → 建 promise_followup 议程（not_before_at=0.0）
      → 13 秒后自主发出「之前答应了 Rezon 一件事，看看是否需要跟进」

生产实测：`commitments.json` **14/14 条全是假账**；`agenda.json` 33 条里 11 条
`promise_followup` 同源；`debts.json` 7 条里 1 条的 `due_hint` 是一句斗嘴的原话。

本文件把**生产真实原文**钉成守护用例：
1. 假承诺必须**不再**命中；真承诺必须**仍然**命中（不能靠"什么都不匹配"过关）；
2. 两处实现必须共用**同一份**词表（防止再次分叉）；
3. 口头语不得给出精确 `due_ts`；
4. 群名片必须优先于 QQ 昵称（"Rezon" vs "Aki"）；
5. 论文体检测：结构词（编号分点）**独立生效**，不被闭集语义词表短路。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from anima.constants import COMMITMENT_RE
from anima.cognitive.debt import parse_due_hint
from anima.util.name_clean import clean_display_name, sender_display_name

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. 假承诺：生产真实原文（逐字取自 data/debts.json、data/agenda.json、
#    data/commitments.json）—— 修复前**全部**被当成承诺
# ---------------------------------------------------------------------------
PROD_FALSE_PROMISES = [
    # debts.json 里 due_hint 就是这句斗嘴原话
    "等下，你怎么也抢本喵的自称啊！不过键盘托归我可不许反悔，本喵这就上去把肚皮摊平，今晚谁也别想碰键盘一个键喵！",
    # debts/agenda 里：「下次再乱喵，本喵真拿尾巴抽你了」
    "哈？少来碰瓷！两脚兽身上连根猫毛都没有，学猫叫充其量算高仿盗版，还好意思跟本喵比先来后到？下次再乱喵，本喵真拿尾巴抽你了。",
    "谁骂你了呀，少在那装可怜……下次收敛点就好嘛~",
    # 「让本喵」裸词
    "本喵不就活生生地在跟你聊天嘛！这么大一只超级无敌可爱的美少女猫娘你看不到嘛喵！\n\n难道你还想让本喵穿过网线从屏幕里钻出去抓你",
    "哼，知道谢就好，下次可别指望本喵随叫随到了喵！",
    # commitments.json：日常闲聊 / 告别
    "哈欠……困死了，快去睡觉，明天再折腾……",
    "别踩电源键喵 那台还在挂着任务，按下去今晚就别想睡了",
    "你真要试明天再去测啦，都几点了，快去睡觉……",
    "想得美！本喵踩上去了哪里还有自己挪窝的道理，当然是就地圈成一团直接睡你身上喵！\n\n这叫不可抗力占领高地懂不懂，到时候你翻身都算违规操作，敢乱动一下本喵就拿爪子扒拉你脸！",
    # 旧测试里被误标为"承诺"的告别语
    "下次再聊",
    # 负向断言：否定句不是承诺
    "我不会骗你的",
    # 一般闲聊
    "今天天气不错",
    "你好",
    "本喵觉得不行",
    # 🔴 2026-09-20 二次回测（用生产三本账**全量**原文跑新词表）新增：
    #   这三条是**修好之后仍被新词表命中**的假账 —— 证明"收紧一次"不够。
    #   `本喵会的本事` / `还好我会功夫` 是「会 = 会做（能力）」，不是「会去做」；
    #   `先说好` 是话语标记（=丑话说前头），不是约定。
    "哼哼，本喵会的本事可多着呢，搜点图这种小事自然不在话下喵~",
    "把我的斗笠和八字胡气歪了，害我从梯田上滚了下来，……但还好我会功夫，尽管眯着眼也没有砸到田里的老黄牛。",
    "碗里可还是空的呢！先说好，粗蛋白低于40%、拿肉粉和高淀粉填充物糊弄的膨化粮本喵连闻都不闻喵。",
    # 循环产物：autonomy 自己给债务议程插的标签「（约好 <due_hint>）」
    "你答应过的事：之前答应了 Rezon 一件事，看看是否需要跟进。（约好 等下，你怎么也抢本喵的自称啊）",
    # 🔴🔴 2026-09-21 第三轮：前两轮只收了 `本喵会`，`帮 / 来 / 记得` 仍是裸词 ⇒
    #   "修好"后 23h 内又被**下面这句原文**造出 1 条假债
    #   （21:35 建账 → 06:00 兑现成一次答非所问的主动追问，占当日自主发言 25%）。
    #   原文逐字取自 NapCat 出站正文 —— 回测必须扫**真实输入域**，不是记忆库摘要。
    "而且天天本喵来本喵去的，老实交代，你是不是背着我偷偷进货猫娘成分了！",
    # 裸 `本喵帮` = 拒绝（"帮不了"），语义与承诺正好相反
    "少来，本喵帮不了你",
    # 裸 `本喵记得` = 陈述记忆（带动作词才算承诺）
    "本喵记得你上次说的那个事",
]


@pytest.mark.parametrize("text", PROD_FALSE_PROMISES)
def test_production_false_promises_no_longer_match(text):
    assert not COMMITMENT_RE.search(text), f"裸时间词又造出了假承诺: {text!r}"


# ---------------------------------------------------------------------------
# 2. 真承诺必须仍然命中 —— 防止"把词表删空"这种假绿
# ---------------------------------------------------------------------------
TRUE_PROMISES = [
    "回头帮你查一下",
    "本喵去查查",
    "帮你看看",
    "记下了",
    "我会在明天把测试结果发出来",     # tests/test_episodes.py 用例
    "提醒我今晚检查部署日志",         # tests/test_episodes.py 用例
    "行，本喵会帮你盯着部署结果，到时候告诉你",  # tests/test_episodes.py 用例
    "明天帮你查一下开播时间",
    "等下本喵帮你看看到底怎么回事",
    "我这就去搜一下",
    "回头给你带个小鱼干",
    # 🔴 收紧 `会` 之后必须仍然过的真承诺（`会` 后面真的接了动作）
    "本喵会帮你记着的",
    "明天我会把图发给你",
    "今晚我会提醒你早点睡",
]


@pytest.mark.parametrize("text", TRUE_PROMISES)
def test_real_promises_still_match(text):
    assert COMMITMENT_RE.search(text), f"真承诺被漏掉了: {text!r}"


def test_both_implementations_share_one_lexicon():
    """🔴 单一真源：pipeline 与 memory **必须**是同一个编译好的正则对象。

    本次缺陷的成因就是两处各写一份、各自吃到裸时间词后**各自**跑偏。
    这里用 `is` 判定（不是 `==`），保证将来任何人再复制一份都会红。
    """
    from anima.pipeline.stages._shared import _PROMISE_RE
    from anima.memory.episodes import _COMMITMENT_HINT

    assert _PROMISE_RE is COMMITMENT_RE
    assert _COMMITMENT_HINT is COMMITMENT_RE


def test_no_bare_temporal_word_in_lexicon():
    """反向守卫：第一人称短语段里**不允许**出现裸时间词。

    能通过上面 `is` 检查但被人改坏的情形是：有人把 `明天|下次|…` 直接塞进
    第一段（那段不需要动作词共现）。这里从**编译后的 pattern** 上确认时间词
    只出现在第二段（其后紧跟 `{0,14}` 间隔约束 + 动作词）。
    """
    strong = COMMITMENT_RE.pattern.split("|(?:明天")[0]
    for word in ("下次", "明天", "今晚", "到时候", "等下", "稍后", "待会", "回头"):
        assert f"|{word}|" not in strong, f"裸时间词 {word} 混进了第一人称短语段"


def test_no_bare_self_reference_verb_in_lexicon():
    """🔴 同类枚举守卫：`我/本喵 + 承诺动词核` 不得作为**裸词**出现。

    2026-09-21 第三轮事故：前两轮只收了 `会`，`帮 / 来 / 记得` 仍是裸词 ⇒
    "修好"后 23h 内又被 `本喵来` 造出假债。裸动词核对每个词都有同一种陷阱
    （名词化 / 否定 / 话题结构 / 陈述记忆）：
      `会的本事` / `帮不了` / `来…去` / `记得你说过`

    故本条把"动词核必须带动作约束"钉在 pattern 上 —— 将来再加 `本喵X` 类词会红。
    """
    strong = COMMITMENT_RE.pattern.split("|(?:明天")[0]
    for verb in ("本喵帮", "本喵来", "本喵记得", "我来", "我会", "哈基米会"):
        assert f"|{verb}|" not in strong, (
            f"裸动词核 {verb} 又混进了第一人称段 —— 它必须走 "
            f"`(?:我|本喵|哈基米)(?:会|帮|来|记得)(?![不的])(?=…动作)` 构式"
        )
    assert r"(?:会|帮|来|记得)(?![不的])" in strong, (
        "承诺动词核的负向断言 / 动作共现约束不见了"
    )


# ---------------------------------------------------------------------------
# 3. 口头语不得给精确时间（parse_due_hint）
# ---------------------------------------------------------------------------
def test_colloquial_hints_are_fuzzy_not_precise():
    """「等下 / 稍后 / 待会」只表示"待会儿再说"，**不硬算时间戳**。

    修复前 `parse_due_hint("等下…")` 返回 `(now+2h, …, fuzzy=False)` —— 自认
    精确，债务于是被当成"2 小时后就到期"，并被 `reminder` 工具原样打印成
    「约好 等下，你怎么也抢本喵的自称啊…」推到群里。
    """
    now = 1_000_000.0
    for hint in ("等下", "稍后", "待会", "等下，你怎么也抢本喵的自称啊"):
        ts, resolved, fuzzy = parse_due_hint(hint, now=now)
        assert ts is None, f"{hint!r} 不应给出精确时间戳"
        assert fuzzy is True, f"{hint!r} 必须标模糊"
        assert resolved == hint


def test_explicit_times_stay_precise():
    """明确时间不受影响（不能因为收紧口头语就把真时间也弄丢）。"""
    now = 1_000_000.0
    ts, _, fuzzy = parse_due_hint("我明天帮你查", now=now)
    assert ts is not None and not fuzzy and abs(ts - (now + 86400)) < 5
    ts, _, fuzzy = parse_due_hint("3小时后提醒我", now=now)
    assert ts is not None and not fuzzy and abs(ts - (now + 3 * 3600)) < 5


# ---------------------------------------------------------------------------
# 4. 显示名：群名片优先于 QQ 昵称
# ---------------------------------------------------------------------------
def test_group_card_wins_over_qq_nickname():
    """群里用群名片「Aki」，bot 却叫 QQ 昵称「Rezon」—— 群友当场纠正过。"""
    assert sender_display_name({"card": "Aki", "nickname": "Rezon"}, 15622) == "Aki"


def test_falls_back_to_nickname_then_uid():
    assert sender_display_name({"card": "", "nickname": "Rezon"}, 15622) == "Rezon"
    assert sender_display_name({"card": "  ", "nickname": ""}, 15622) == "15622"
    assert sender_display_name({}, 15622) == "15622"


def test_sender_display_name_is_robust_to_garbage():
    """sender 缺失/类型异常不得抛（fail-soft，与仓库其余读取点一致）。"""
    assert sender_display_name(None, 7) == "7"
    assert sender_display_name("not-a-dict", 7) == "7"


def test_card_wins_inside_clean_display_name_decor_stripping():
    """名片也走既有的装饰括号清洗（[汉化组] 之类不是名字本体）。"""
    assert clean_display_name("[DL版]Aki") == "Aki"
    assert sender_display_name({"card": "[DL版]Aki", "nickname": "Rezon"}, 1) == "Aki"


def test_handlers_use_the_shared_resolver():
    """源码级：两处 sender_name 取值都必须走 `util.name_clean` 的共享解析。"""
    for rel in ("anima/llm/handler.py", "anima/llm/commands.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8-sig")
        assert "sender_display_name" in src, f"{rel} 未使用共享解析（会叫 QQ 昵称）"
        assert '.get("nickname"' not in src, f"{rel} 里仍有裸 nickname 取值"


def test_bot_social_relationship_uses_display_name():
    """源码级：`bot_social` 记录 Bot 关系时也得用显示名（群名片优先）。

    🔴 2026-09-20 复检残留面：`event["sender"]["nickname"]` 直读 —— 与 `handler` /
    `commands` 同类缺陷。注意本文件里 `suggest_bots_from_nickname` 的 `m["nickname"]`
    是**成员列表里的昵称本身**（用于 bot 识别启发式），语义不同，不在本条范围内。
    """
    src = (_ROOT / "anima" / "cognitive" / "bot_social.py").read_text(encoding="utf-8-sig")
    assert "sender_display_name" in src, "bot_social 仍在直读 QQ 昵称当显示名"


def test_astrbot_compat_sender_name_prefers_card():
    """行为级：AstrBot 兼容层的 `sender_name` 也必须群名片优先。

    修复前 `sender_name=sender.get("nickname", "")` —— 与 P0-3 同源（"Rezon" vs "Aki"）。
    `sender_info` 里的 `nickname` / `card` 原始字段必须**原样保留**（是 API 契约）。
    """
    from anima.compat.astrbot import AstrMessageEvent

    ev = AstrMessageEvent.from_onebot({
        "user_id": 1, "group_id": 2, "message_id": 3, "raw_message": "hi",
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "sender": {"user_id": 1, "nickname": "Rezon", "card": "Aki", "role": "member"},
    })
    assert ev.sender_name == "Aki", "兼容层又用 QQ 昵称当 sender_name 了"
    assert ev.sender_info.nickname == "Rezon", "原始 nickname 字段不得丢（是契约）"
    assert ev.sender_info.card == "Aki"


def test_astrbot_compat_falls_back_when_card_empty():
    """私聊 / 未设群名片时回退 QQ 昵称（不能变成空串）。"""
    from anima.compat.astrbot import AstrMessageEvent

    ev = AstrMessageEvent.from_onebot({
        "user_id": 1, "message_id": 3, "raw_message": "hi",
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "sender": {"user_id": 1, "nickname": "Rezon", "card": "", "role": "member"},
    })
    assert ev.sender_name == "Rezon"


# ---------------------------------------------------------------------------
# 5. 论文体：结构词（编号分点）独立生效
# ---------------------------------------------------------------------------
PROD_ESSAY_LEAK = (
    "这篇申诉发言在社区治理和公关层面上堪称典范！面对二次元同名角色突发流量冲击传统体育垂直社区的典型冲突，"
    "发言者没有激化群体对立，反而在逻辑上建立了极其清晰的三层处理架构："
    "1. 溯源与确权：明确阿隆佐·莫宁的历史贡献与社区原生定位，捍卫原本的文化基底；"
    "2. 流量归因与风险隔离：理智拆解「外部热度流入」与「恶意串子夺权」的区别，避免迁怒于普通二次元玩家；"
    "3. 分流与共存方案：提出设立二级分区以及向定向贴吧做路由引流，用机制实现异质文化的物理隔离。"
)


def test_essay_structure_fires_without_semantic_keys():
    """生产漏网案例：用户只**粘贴长文没提问**，bot 回了三段编号论文体。

    修复前 `if not keys: return False` 前置 ⇒ 闭集语义词表一个不中就直接放行。
    """
    from anima.pipeline.stages.guardrail import _looks_like_analysis_essay

    assert _looks_like_analysis_essay(PROD_ESSAY_LEAK) is True


def test_essay_structure_does_not_misfire_on_casual_chat():
    """收紧方向必须只打论文体，不误伤日常（否则会把正常回复整段改写）。"""
    from anima.pipeline.stages.guardrail import _looks_like_analysis_essay

    assert _looks_like_analysis_essay("哈哈哈你这人也太逗了，接着聊呗～") is False
    assert _looks_like_analysis_essay("从这个角度来看你赢了") is False
    assert _looks_like_analysis_essay(
        "今天天气真好，我就在想从这个角度你肯定会喜欢的，所以我们出去玩吧，"
        "天气这么好不出去浪费了，真的。") is False


def test_user_requested_analysis_keeps_its_exemption():
    """用户自己说「帮我解析一下」时，短回复不该被判论文体。"""
    from anima.pipeline.stages.guardrail import _looks_like_analysis_essay

    short = "本喵觉得大概是官方故意写得含糊啦，别太较真喵。"
    assert _looks_like_analysis_essay(short, user_text="帮我解析一下设定") is False


# ---------------------------------------------------------------------------
# 6. 承诺跟进议程：不得"刚说完就追问"
# ---------------------------------------------------------------------------
class _FakeCfg:
    def __init__(self, data_dir):
        self.persistence = type("P", (), {"data_dir": str(data_dir)})()
        self.debt_tracking = type("D", (), {"enabled": False})()


class _FakeCore:
    def __init__(self, data_dir):
        self.config = _FakeCfg(data_dir)
        self.calls = []

    def propose_agenda(self, group_id, text, *, priority=0.5, not_before_at=0.0,
                       expires_at=0.0, source="system"):
        self.calls.append(dict(group_id=group_id, text=text, priority=priority,
                               not_before_at=not_before_at, expires_at=expires_at,
                               source=source))
        return type("Agenda", (), {"id": "ag_t"})()


def _ctx(reply, core, tmp_path):
    from anima.pipeline import PipelineContext

    ctx = PipelineContext(
        event={}, text="随便说说", user_id=1, group_id=727631526,
        detail_type="group", sender_name="Aki", meta={},
    )
    ctx.reply = reply
    ctx.meta["core"] = core
    return ctx


def test_promise_followup_is_delayed_not_instant(tmp_path):
    """🔴 修复的直接动因：10:44:10 记下 → 10:44:23（13 秒后）就发出去了。

    `not_before_at` 必须严格晚于"现在"（对齐 `episodes` 的 due_at 口径），
    且 `expires_at` 必须晚于 `not_before_at`（否则议程在可发言前就过期 ⇒ 永不触发）。
    """
    from anima.pipeline.stages._shared import _detect_promise_and_schedule

    core = _FakeCore(tmp_path)
    before = time.time()
    _detect_promise_and_schedule(_ctx("回头帮你查一下开播时间喵", core, tmp_path))

    assert core.calls, "真承诺应创建议程"
    call = core.calls[0]
    assert call["source"] == "promise_followup"
    assert call["not_before_at"] >= before + 3600, "承诺跟进不得立刻可发"
    assert call["expires_at"] > call["not_before_at"], "议程会在可发言前过期"


def test_false_promise_creates_no_agenda(tmp_path):
    """斗嘴回复不得再创建议程（这是"bot 突然说起不相干事"的源头）。"""
    from anima.pipeline.stages._shared import _detect_promise_and_schedule

    core = _FakeCore(tmp_path)
    _detect_promise_and_schedule(
        _ctx("哈？少来碰瓷！下次再乱喵，本喵真拿尾巴抽你了。", core, tmp_path))
    assert core.calls == []


def test_shared_source_passes_not_before_at():
    """源码级守卫：`not_before_at` 必须在调用点显式传入（防止被改回去）。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "_shared.py").read_text(encoding="utf-8-sig")
    assert "_PROMISE_FOLLOWUP_DELAY_SEC" in src
    assert "not_before_at=_not_before" in src


def test_debt_content_records_what_is_owed(tmp_path):
    """`Debt.content` 必须回答"欠了什么"，而不是议程提醒话术。

    2026-09-21 深审：此前存 `intent`（"之前答应了 X 一件事，看看是否需要跟进。
    当时的回复是：…"）⇒ ① `autonomy` 的主动发言模板
    `你之前答应过的事：{content}。看看现在能不能做了` 会拼出双重冗余的口水句，
    用户看不出到底欠了什么；② 债库无法人工复核真假（假账洗白的最后一环）。
    """
    from anima.cognitive.debt import get_debt_manager
    from anima.pipeline.stages._shared import _detect_promise_and_schedule

    class _Cfg:
        persistence = type("P", (), {"data_dir": str(tmp_path)})()
        debt_tracking = type("D", (), {"enabled": True})()

    class _Core:
        def __init__(self):
            self.config = _Cfg()

        def propose_agenda(self, *a, **k):
            return type("A", (), {"id": "ag"})()

    _detect_promise_and_schedule(_ctx("回头帮你查一下开播时间喵", _Core(), tmp_path))

    pend = get_debt_manager(str(tmp_path)).pending()
    assert len(pend) == 1, "真承诺应记一条债"
    content = pend[0].content
    assert "看看是否需要跟进" not in content, f"债 content 仍是议程话术: {content!r}"
    assert "帮你查" in content, f"债 content 没回答'欠了什么': {content!r}"


# ---------------------------------------------------------------------------
# 7. 兜底话术不得伪装成"没听清"
# ---------------------------------------------------------------------------
def test_no_tool_pool_stops_asking_for_verbatim_repeat():
    """安全出口撤出「你再说一遍 / 你重发一下」。

    实测 G5/G7：露骨内容 → 「本喵刚卡住一下，你再说一遍喵？」→ 用户原样重发
    → 撞同一堵墙。用"没听清"表达"我不接"是最伤观感的。
    """
    from anima.agent.react import _NO_TOOL_FALLBACKS, _TOOL_FAILED_FALLBACKS, _TOOL_OK_FALLBACKS
    from anima.pipeline.stages.guardrail import _MECHANICAL_REWRITES

    banned = ("再说一遍", "重发", "再说一次", "重复一遍")
    for text in (*_NO_TOOL_FALLBACKS, *_TOOL_FAILED_FALLBACKS, *_TOOL_OK_FALLBACKS,
                 *_MECHANICAL_REWRITES):
        hits = [w for w in banned if w in text]
        assert not hits, f"兜底话术仍在索取原样复述 {hits}: {text!r}"


def test_no_tool_pool_offers_an_exit():
    """撤掉"再说一遍"不等于变成纯情绪句 —— 但那个出口也不能是"换个话题"（2026-09-22）。

    该池是「上游静默返空」的通用出口（三候选模型全 200-空 ⇒ failover 穷尽），
    病因与用户说了什么**无关**。所以正确形态是：**自认是本喵这边的岔子**，而不是把
    用户的话题 / 说法当成问题让人重来 —— 那是甩锅，用户读到就是「bot 嫌我聊的话题」。
    """
    from anima.agent.react import _NO_TOOL_FALLBACKS

    for text in _NO_TOOL_FALLBACKS:
        assert any(w in text for w in ("本喵这边", "本喵刚", "本喵这")), \
            f"缺自认责任: {text!r}"
        assert "本喵" in text and "喵" in text, f"丢了人格口吻: {text!r}"
        hits = [w for w in ("换个话题", "换个说法", "换句话", "换个问法") if w in text]
        assert not hits, f"仍在甩锅话题 {hits}: {text!r}"


def test_react_source_no_longer_hardcodes_repeat_wording():
    """源码级：防止有人把旧措辞原样写回。"""
    src = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    body = src.split("_NO_TOOL_FALLBACKS = (")[1].split(")")[0]
    assert not re.search(r"再说一遍|重发一下", body)
