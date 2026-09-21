# -*- coding: utf-8 -*-
"""bot 侧承诺判据 —— 第三份词表的收口（2026-09-20 复检）。

背景
----
上两轮把 `_shared._PROMISE_RE` 与 `episodes._COMMITMENT_HINT` 统一到
`constants.COMMITMENT_RE`，并据此清了生产假账（`commitments 14→0`）。
但 `episodes.py:105` 的判据是**按角色分叉**的：

    has_commit = _COMMITMENT_HINT.search(text) or (role == "bot" and _BOT_PROMISE_HINT.search(text))
                                                                        ^ 第三份独立实现

`_BOT_PROMISE_HINT` 没跟着收紧，仍含裸 `本喵会` —— 于是
「哼哼，本喵会的本事可多着呢」这类**能力陈述**在 `role="bot"` 时照样被记成承诺
⇒ 刚清掉的假账会从这个口子重新长出来。

（"清账后不再产生新假账"当时只有推理支撑，本轮把它拿去验证 —— **推理是错的**。
 `re.compile` 全库枚举后，承诺词表共 **3 处定义**：`COMMITMENT_RE`（共享真源）、
 `_shared._RETRY_PROMISE_RE`（须与 `_FAILURE_RE` 共现）、`_BOT_PROMISE_HINT`。）

本文件钉住四件事：
1. bot 侧与 user 侧**必须**共用同一份编译好的词表（源码级 + 行为级）；
2. 能力陈述（`会` + 名词化 / `会` = 会做）不再入账；
3. 真承诺不误伤（不能靠"什么都不匹配"过关）；
4. `_capture_commitment` 的 `due_at` 不再由裸时间词决定（第三处裸时间词）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from anima.constants import COMMITMENT_RE
from anima.core.persistence import PersistenceProvider

_ROOT = Path(__file__).resolve().parents[1]


def _mem(tmp_path):
    from anima.memory.episodes import EpisodicMemory

    return EpisodicMemory(PersistenceProvider(str(tmp_path)))


# ---------------------------------------------------------------------------
# 1. 单一真源：bot 侧不得再有第二份词表
# ---------------------------------------------------------------------------
def test_bot_side_has_no_second_lexicon():
    """`episodes` 模块内不得存在独立于 `COMMITMENT_RE` 的承诺词表。

    用 `getattr` 而不是 import：名字被删掉时 `import` 会直接 ImportError，
    而这条断言要表达的是"**不该有**这个名字"，两者语义不同。
    """
    from anima.memory import episodes as _ep

    assert not hasattr(_ep, "_BOT_PROMISE_HINT"), (
        "bot 侧又出现了独立的承诺词表 —— 它不会跟着 COMMITMENT_RE 一起收紧"
    )


def test_episodes_side_uses_the_shared_lexicon():
    """`episodes` 引用的判据**必须就是** `COMMITMENT_RE` 本身（`is` 判定）。"""
    from anima.memory import episodes

    assert episodes._COMMITMENT_HINT is COMMITMENT_RE


def test_source_has_a_single_predicate_line():
    """源码级：`:105` 那行的判据不得再出现按角色分叉的第二入口。"""
    src = (_ROOT / "anima" / "memory" / "episodes.py").read_text(encoding="utf-8-sig")
    line = next(ln for ln in src.splitlines() if "has_commit = bool(" in ln)
    assert '_BOT_PROMISE_HINT' not in line, f"bot 侧第二判据又回来了: {line.strip()!r}"
    assert '_COMMITMENT_HINT.search(text)' in line


# ---------------------------------------------------------------------------
# 2. 非承诺陈述不得入账（生产原文）
# ---------------------------------------------------------------------------
# 覆盖五类「裸动词核」陷阱：能力（会）/ 声明（说好）/ 记忆（记得）/ 拒绝（帮不了）
# / 话题结构（来…去）。全部是 2026-09-21 用**出站正文**回归抓到的真实误判。
BOT_NON_COMMITMENT_REPLIES = [
    # 群成员档案里的自我吹嘘：`会` + `的` 名词化 = "我知道的本事"
    "哼哼，本喵会的本事可多着呢",
    "哼哼，本喵会的本事可多着呢，搜点图这种小事自然不在话下喵~",
    # `会` = 会做（能力），不是"会去做"
    "把我的斗笠和八字胡气歪了，……但还好我会功夫，尽管眯着眼也没有砸到田里的老黄牛。",
    # 话语标记：丑话说前头
    "碗里可还是空的呢！先说好，粗蛋白低于40%的膨化粮本喵连闻都不闻喵。",
    # 🔴 裸 `本喵记得` = 陈述记忆（不是承诺；带动作词才成立）
    "本喵记得你上次说的那个事",
    # 🔴 裸 `本喵帮` = 拒绝（"帮不了"），语义与承诺正好相反
    "少来，本喵帮不了你",
    # 🔴 裸 `本喵来` = 话题结构（"本喵来…本喵去"），不是"我来做某事"。
    #    真实事故来源：词表"修好"后 23h 内又被它造出 1 条假债
    #    （21:35 建账 → 06:00 兑现成一次答非所问的主动追问）。
    "而且天天本喵来本喵去的，老实交代！",
]


@pytest.mark.parametrize("text", BOT_NON_COMMITMENT_REPLIES)
def test_bot_non_commitment_reply_is_not_captured(text, tmp_path):
    """bot 自己说的非承诺句不得生成承诺（"清账后又长回来"的那条路）。"""
    mem = _mem(tmp_path)
    mem.observe("group", 727631526, 2752719822, "bot", text)
    assert mem.commitments == [], f"非承诺句被记成承诺: {text!r}"


def test_bot_farewell_is_not_a_commitment(tmp_path):
    """告别/闲聊（含裸时间词）在 bot 侧同样不入账。"""
    mem = _mem(tmp_path)
    mem.observe("group", 727631526, 2752719822, "bot", "哈欠……困死了，快去睡觉，明天再折腾……")
    assert mem.commitments == []


# ---------------------------------------------------------------------------
# 3. 真承诺必须仍然入账（防"把词表删空"的假绿）
# ---------------------------------------------------------------------------
BOT_REAL_PROMISES = [
    # 🔴 2026-09-21 第三轮：裸 `本喵记得` 已并入①构式（必须后接动作词）。
    #   原用例「本喵记得你上次说的那个事」是**陈述记忆**而非承诺，已移入负例；
    #   带动作的「本喵记得明天帮你查」才是真承诺。
    "本喵记得明天帮你查",
    "本喵会帮你盯着部署结果的",
    "行，本喵会帮你盯着部署结果，到时候告诉你",
]


@pytest.mark.parametrize("text", BOT_REAL_PROMISES)
def test_bot_real_promise_is_still_captured(text, tmp_path):
    mem = _mem(tmp_path)
    mem.observe("group", 727631526, 2752719822, "bot", text)
    assert len(mem.commitments) == 1, f"真承诺被漏掉了: {text!r}"


def test_user_side_still_works(tmp_path):
    """收紧 bot 侧不得连带影响用户侧。"""
    mem = _mem(tmp_path)
    mem.observe("group", 727631526, 15622, "user", "回头帮你查一下")
    assert len(mem.commitments) == 1


# ---------------------------------------------------------------------------
# 4. `due_at` 不再由裸时间词决定（第三处裸时间词）
# ---------------------------------------------------------------------------
def test_due_at_is_uniform_six_hours(tmp_path):
    """无论句子里有没有「明天 / 今晚 / 下次」，`due_at` 都是统一的 +6h。

    修复前：`明天→+20h`、`今晚→+4h`、`稍后·待会·回头→+2h`、`下次→+24h`。
    这是**第三处**裸时间词（前两处在 `COMMITMENT_RE` 与 `parse_due_hint`）。
    `+6h` 与 `_shared._PROMISE_FOLLOWUP_DELAY_SEC` 同一口径。
    """
    now = time.time()
    for i, text in enumerate((
        "本喵记得明天帮你查",
        "本喵记得今晚提醒你",
        "本喵记得下次帮你带",
        "本喵记得回头帮你看",
    )):
        mem = _mem(tmp_path / f"case{i}")
        mem.observe("group", 727631526, 2752719822, "bot", text)
        assert len(mem.commitments) == 1, f"真承诺被漏掉: {text!r}"
        delta = mem.commitments[0].due_at - now
        assert 6 * 3600 - 60 <= delta <= 6 * 3600 + 60, (
            f"{text!r} 的 due_at 仍是裸时间词算的（delta={delta / 3600:.1f}h，应为 6h）"
        )


def test_capture_commitment_source_has_no_bare_temporal_branch():
    """源码级：`_capture_commitment` 里不得再按裸时间词分支算 `due_at`。"""
    src = (_ROOT / "anima" / "memory" / "episodes.py").read_text(encoding="utf-8-sig")
    body = src.split("def _capture_commitment")[1].split("def resolve(")[0]
    code = "\n".join(ln.split("#")[0] for ln in body.splitlines())
    for word in ("明天", "明日", "今晚", "稍后", "待会", "回头", "下次"):
        assert f'"{word}"' not in code and f"'{word}'" not in code, (
            f"`_capture_commitment` 仍在按裸时间词 {word} 算 due_at"
        )
    assert "due_at = time.time() + 6 * 3600" in code


# ---------------------------------------------------------------------------
# 5. debt 到期议程不得回贴 `due_hint` 原文（自我放大回路的另一半）
# ---------------------------------------------------------------------------
def test_debt_due_agenda_does_not_echo_raw_due_hint():
    """源码级：`autonomy` 的 debt 到期议程不得把 `due_hint` **原文**贴进主动发言。

    「约好 等下，你怎么也抢本喵的自称啊」就是这么来的 —— 一句斗嘴原话被
    `parse_due_hint` 原样存进 `due_hint`（那时还自认 `fuzzy=False`），
    再由这里**无条件回贴**进主动发言文本，于是 bot 张口引用一件不存在的事。
    源头已收窄，但模板本身必须一起改，否则任意 `due_hint` 仍会被原样搬出来。
    """
    src = (_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8-sig")
    # 剥注释再判 —— 否则"解释这个坑"的注释本身会命中（这次就踩了）。
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "（约好" not in code, "debt_due 议程又在回贴 due_hint 原文"
    assert "别装作没这回事" not in code, "训斥式措辞又回来了（与人格不符）"
