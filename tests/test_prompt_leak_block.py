# -*- coding: utf-8 -*-
"""prompt / 内部资料泄露的出站拦截（2026-10-25 生产事故后新增）。

## 事故

2026-10-25 17:41:33，私聊，用户问「华初是谁」。出站 **213 字**：

```
（遵照）…的指令，思考华初是谁。
华初：QQ:1371704917，雨萌（主人）的对象，也是 MTF；对她友好、带点好奇。
要求：
- 严格遵循短句、一句话、不写论文体、不写第一第二第三、口语化。
- 猫娘口吻，带点自然、傲娇或随意的语气。
- 回答「华初是谁」：直接说她是雨萌的对象呀，或者随口带一句。
- 注意现在。

回复草稿：
 你家那位嘛，怎么，连自己对象是谁都要本喵提醒呀喵？
或者短一点：
 你家那位嘛，明知故问什么呀喵~
```

**一次泄露了三样东西**：
1. 人格正本 `:95-97` 的关系资料，**含 QQ 号 `1371704917`**；
2. 我们写进 prompt 的**风格要求**（「不写论文体」「口语化」）；
3. 模型**自己的草稿过程**。

## 根因

人格正本把 `QQ:1562290139` / `QQ:1371704917` 这类**账号标识**写进了角色设定。
模型把这些「资料」当成可复述的素材 ⇒ 直接抄出来。

**人格侧已改**（删掉 QQ 号，改为口头称呼），但 prompt 层不可靠
⇒ 出站必须再拦一道。

## 为什么不是「删掉号码继续发」

这类输出通常是「把内部资料整段抄出来」，删掉号码剩下的仍是大段泄密文本
（要求清单、草稿过程）。发出去比拒发更像事故。⇒ **整条拒发**。
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from anima.pipeline.stages.guardrail import _detect_prompt_leak, _is_account_like_digits

_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 事故原文（脱敏后写进测试：号码改成明显假号，但形态一致）
_ACCIDENT = """的指令，思考华初是谁。
华初：QQ:1371704917，雨萌（主人）的对象，也是 MTF；对她友好、带点好奇。
要求：
- 严格遵循短句、一句话、不写论文体、不写第一第二第三、口语化。
- 猫娘口吻，带点自然、傲娇或随意的语气。
- 注意现在。

回复草稿：
 你家那位嘛，怎么，连自己对象是谁都要本喵提醒呀喵？"""


# ── 1. 事故文本必须被拦 ─────────────────────────────────────────────

def test_accident_text_is_blocked():
    assert _detect_prompt_leak(_ACCIDENT), "事故原文没被拦下来"


def test_bare_qq_number_is_blocked():
    assert _detect_prompt_leak("1562290139")
    assert _detect_prompt_leak("吴雨萌的QQ是1562290139")
    assert _detect_prompt_leak("她的号是 1371704917 哦")


def test_phone_number_is_blocked():
    """11 位手机号是隐私，一律拦。"""
    assert _detect_prompt_leak("电话 13812345678")
    assert _detect_prompt_leak("我手机13812345678")


def test_instruction_markers_are_blocked():
    assert _detect_prompt_leak("要求：\n1. 不要加引号\n回复草稿：好")
    assert _detect_prompt_leak("不写论文体、口语化。注意现在")


# ── 2. 正常人话必须放行（误伤是最贵的 bug）──────────────────────────

@pytest.mark.parametrize("text", [
    "哼，怎么又骂人……信不信本喵一爪子拍过去喵！",
    "吴雨萌嘛，少来套路本喵喵！",
    "这是我杯子",
    "今天17点40分的比赛看了吗",
    "2026年10月25日",
    "B站那个 BV1xx411c7mD 视频不错",
])
def test_normal_replies_pass(text):
    assert _detect_prompt_leak(text) == "", f"误伤正常人话：{text!r}"


@pytest.mark.parametrize("text", [
    "这个显卡 120000 块吧",        # 金额
    "第 123456 楼说的对",          # 楼层
    "这个番有 1234567 播放",       # 播放量
    "共 123456 人看过",            # 数量
    "他粉丝 2345678 个",           # 粉丝数
])
def test_quantity_numbers_pass(text):
    """🔴 实测误伤（第一版写完立刻测出来的）。

    光看「长度 ≥6」不够 —— 数量词/序数词/单位词之后的长数字是**量**不是**标识**。
    """
    assert _detect_prompt_leak(text) == "", f"把数量当账号拦了：{text!r}"


def test_long_number_without_context_is_account():
    """没有任何上下文的长数字串按账号处理（宁可拦）。"""
    assert _detect_prompt_leak("156229013913")


# ── 3. 上下文判据的具体形态 ─────────────────────────────────────────

def _m(text: str):
    """找到 text 里第一个长数字的 match 对象。"""
    import re
    m = re.search(r"\d{6,}", text)
    assert m is not None, f"{text!r} 里没有长数字"
    return m


def test_qq_prefix_makes_it_dangerous():
    t = "QQ:1371704917"
    assert _is_account_like_digits(t, _m(t)) is True


def test_counter_prefix_makes_it_safe():
    for t in ("第 123456 楼", "共 123456 人", "有 1234567 播放"):
        assert _is_account_like_digits(t, _m(t)) is False, f"{t!r} 被误判为账号"


# ── 4. 人格侧：账号标识不得写进角色设定 ──────────────────────────────

@pytest.fixture(scope="module")
def persona() -> str:
    return yaml.safe_load(
        (_ROOT / "config" / "persona_core.yaml").read_text(encoding="utf-8")
    )["system_prompt"]


def test_persona_has_no_qq_numbers(persona):
    """🔴 根因修复：人格正本里不得出现 QQ 号。

    模型会把这些「资料」当可复述素材抄进出站（事故原文实证）。
    改成口头称呼（「你的主人是吴雨萌」）而不是 `QQ:1562290139`。
    """
    import re
    found = re.findall(r"\d{5,}", persona)
    assert not found, f"人格里仍有长数字（会被当资料抄出来）：{found}"


def test_persona_still_knows_who_they_are(persona):
    """删号码不等于删关系 —— 「知道谁是谁」这个能力必须保留。"""
    assert "吴雨萌" in persona
    assert "华初" in persona
    assert "主人" in persona


def test_persona_forbids_reporting_accounts(persona):
    """必须显式禁止报账号/账号编号。"""
    assert "绝不报出任何人的 QQ 号" in persona


# ── 5. 护栏接线 ────────────────────────────────────────────────────

def test_guardrail_blocks_before_other_rewrites():
    """泄漏拦截必须在所有改写**之前**（改写可能把标记弄丢）。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    body = src.split("async def guardrail_stage", 1)[-1]
    assert "_detect_prompt_leak" in body
    i_leak = body.index("_detect_prompt_leak")
    i_deco = body.index("strip_kinetic_decorations")
    assert i_leak < i_deco, "泄漏拦截必须在装饰剥离之前"


def test_guardrail_returns_skip_reply_on_leak():
    """泄漏时整条拒发，不是删掉号码继续发。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    body = src.split("async def guardrail_stage", 1)[-1]
    seg = body[body.index("_detect_prompt_leak"): body.index("_detect_prompt_leak") + 900]
    assert "SKIP_REPLY" in seg, "泄漏时没有拒发"
    assert 'ctx.reply = ""' in seg, "泄漏时没有清空正文"


def test_guardrail_logs_leak_without_content():
    """🔴 日志只记原因和长度，**绝不记泄露内容本身**（否则 journal 也泄密）。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(
        encoding="utf-8")
    start = src.index("logger.error(") if "logger.error(" in src else -1
    i = src.index("[prompt泄漏拦截]")
    # 取 logger.error(...) 的整个调用（从 logger.error( 到配对的 )）
    j = src.rindex("logger.error(", 0, i)
    k = src.index(")", src.index("len(ctx.reply or \"\")", i))
    seg = src[j:k + 1]
    assert "{leak}" not in seg, "用了 f-string 拼正文"
    assert "leak" in seg, "没记原因"
    assert "%d" in seg or "len(" in seg, "没记长度"
    # 日志参数里不得出现 ctx.reply 本身（只允许 len(ctx.reply ...)）
    log_args = seg.replace('len(ctx.reply or "")', "")
    assert "ctx.reply" not in log_args, "日志参数里带了正文内容"
