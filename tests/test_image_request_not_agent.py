# -*- coding: utf-8 -*-
"""纯生图请求不得被 agent 劫持（2026-10-26 生产事故）。

## 事故

```
16:11:33  群：帮我生成吴雨萌在天上飞的图
16:11:33  它：已记下这个多步任务（b37febecaf）…到私聊跟本喵说一次
```

**一条生图请求，被路由进了 Agent 多步任务流程**，卡在 `planning` / 0 步骤，
然后对话层连续三次编造「已经办妥啦」。

## 根因：`_IMAGE_NOUN_RE` 缺单字「图」

对照实测：

```
「帮我生成吴雨萌在天上飞的图」      → noun=False → is_agent_task=True  ❌
「帮我生成一张吴雨萌在天上飞的图片」 → noun=True  → is_agent_task=False ✅
```

差别只在「图」后面有没有「片/像」。旧列表收了复合词
（`一张图` / `张图` / `原图`），**漏掉最基础的单字形态**，
而中文里「…的图」「做图」「配图」「来张图」极常见。

代码里**本来就有** `_is_pure_image_request()` 这道豁免（09-26 加的），
只是它的名词正则不认单字「图」—— 修的是数据（正则），不是逻辑。

## 判据

1. 「…的图」「做张头像」「来张壁纸」等纯生图请求 ⇒ **不进 agent**；
2. 「看图」「图里」「图中」这类方位用法**不**触发豁免（没配生图动词）；
3. 含代码/脚本/批量等工程诉求的仍走 agent（`_IMAGE_EXEMPT_BLOCK_RE` 否决）；
4. 多步复合任务（「然后」「接着」）仍走 agent。
"""
from __future__ import annotations

import pathlib

import pytest

from anima.agent.intent import (
    _IMAGE_NOUN_RE,
    _is_pure_image_request,
    is_agent_task,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "anima" / "agent" / "intent.py"

#: 生产事故原句（逐字）
_ACCIDENT = "帮我生成吴雨萌在天上飞的图"


# ── 1. 事故原句必须走生图通道 ──────────────────────────────────────

def test_accident_text_is_pure_image_request():
    """🔴 核心：这是生图请求，不是多步任务。"""
    assert _is_pure_image_request(_ACCIDENT) is True
    assert is_agent_task(_ACCIDENT, 4) is False, \
        "生图请求仍被路由进 agent（会卡在 planning 然后编造完成）"


def test_single_char_tu_is_recognized():
    """单字「图」必须被 noun 正则认到。"""
    assert _IMAGE_NOUN_RE.search(_ACCIDENT) is not None


@pytest.mark.parametrize("text", [
    "帮我生成吴雨萌在天上飞的图",
    "做张头像",
    "来张壁纸",
    "生成一张海报",
    "帮我画个图",
    "P图一张",
])
def test_pure_image_requests_bypass_agent(text):
    assert _is_pure_image_request(text) is True, f"{text!r} 没被认成生图"
    assert is_agent_task(text, 4) is False, f"{text!r} 仍进 agent"


# ── 2. 方位用法不得误触发 ──────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "看图说话",
    "图里那个人是谁",
    "这张图什么意思",
    "图中那个东西",
])
def test_locative_usage_not_image_request(text):
    """「图里/图中/看图」是方位/动作，不是在要图生成。

    这些没配「生成/画/做/来」这类生图动词，本来也不会命中
    （`_is_pure_image_request` 要求 noun + verb 同时命中）。
    本条钉住：加了单字「图」之后依然不会误判。
    """
    assert _is_pure_image_request(text) is False, f"{text!r} 被误判成生图"


# ── 3. 工程/多步诉求仍走 agent ────────────────────────────────────

def test_code_request_still_goes_to_agent():
    """同句有代码/脚本诉求 ⇒ `_IMAGE_EXEMPT_BLOCK_RE` 否决豁免。"""
    t = "帮我写个脚本生成图片"
    assert _is_pure_image_request(t) is False
    assert is_agent_task(t, 4) is True


def test_batch_request_still_goes_to_agent():
    t = "批量生成100张表情包"
    assert _is_pure_image_request(t) is False


def test_multi_step_still_goes_to_agent():
    """复合任务（图只是其中一步）仍走 agent。"""
    t = "帮我生成一张图然后发到群里"
    assert _is_pure_image_request(t) is False
    assert is_agent_task(t, 4) is True


# ── 4. 权限门不得被绕过 ───────────────────────────────────────────

def test_non_owner_never_enters_agent():
    """非主人永远不进 agent（与本修复无关，但必须保持）。"""
    assert is_agent_task(_ACCIDENT, 0) is False
    assert is_agent_task(_ACCIDENT, 1) is False
    assert is_agent_task(_ACCIDENT, 3) is False


# ── 5. 源码级：豁免逻辑本身未被绕过 ───────────────────────────────

def test_exemption_still_called_in_is_agent_task():
    """`_is_pure_image_request` 必须仍在 `is_agent_task` 里被调用。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("def is_agent_task")
    body = src[i:i + 900]
    assert "_is_pure_image_request(t)" in body, "生图豁免被删了"
    assert "return False" in body
