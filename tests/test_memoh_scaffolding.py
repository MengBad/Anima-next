# -*- coding: utf-8 -*-
"""执行后端（Memoh）DSML 工具标记不得外泄（2026-11-07）。

## 生产实证

14:29~14:36，主人让 bot 按一个 SKILL.md 执行华为云积分任务。
`ask_memoh` 把重活交给 Memoh 容器，Memoh 的 assistant 消息里混着
`<｜DSML｜｜ calls> <｜DSML｜｜ invoke name="exec"> …` 这类**执行端内部协议标记**。

`_latest_assistant` 原样返回 → 进 ReAct 的 tool result → 模型把它当成
"自己刚说过的话"复述进交付正文 → Agent 交付走 `gateway.send` 直发，
**绕过整条 pipeline**，对话层护栏完全不生效 ⇒ 整段 DSML 标记发到 QQ：

    14:29:11  「[Memoh] 我先去取回那个文件…<｜DSML｜｜ calls>…」
    14:33:44  「[Memoh] <｜DSML｜｜ calls>… ls -la huawei-skill.md …」
    14:35:23  「[Memoh] <｜DSML｜｜ invoke… cat /tmp/huawei-skill.md …」
    14:36:16  「[Memoh] 我先做一次检查…<｜DSML｜｜ invoke… find /data …」

这些标记里还含 `command` 参数（内部路径/命令），同样不能外泄。

## 修法

两道闸：
  ① 源头 `memoh._strip_backend_scaffolding` —— 不让 DSML 进 ReAct 上下文
  ② 出站 `owner_agent._sanitize_delivery` —— 兜底，防其它路径再漏
"""
from __future__ import annotations

import pytest

from anima.tools.memoh import _strip_backend_scaffolding
from anima.agent.owner_agent import OwnerAgent


DSML_BLOCK = (
    "[Memoh] 我先去取回那个文件，弄清楚确切的任务要求。\n\n"
    "<｜DSML｜｜ calls>\n"
    '<｜DSML｜｜ invoke name="exec">\n'
    '<｜DSML｜｜ parameter name="command" string="true">'
    'curl -sL --max-time 60 "https://szb123.com/files/x.md" -o /tmp/x.md; '
    'echo "EXIT=$?"; wc -c /tmp/x.md</｜DSML｜｜ parameter>\n'
    "</｜DSML｜｜ invoke>\n"
    "</｜DSML｜｜ calls>"
)


def test_source_strips_dsml_block():
    out = _strip_backend_scaffolding(DSML_BLOCK)
    assert "DSML" not in out
    assert "curl" not in out, "command 参数（内部命令）不能留下"
    assert "[Memoh]" not in out
    assert "取回那个文件" in out, "人话正文要保留"


def test_delivery_sanitizer_strips_dsml_block():
    out = OwnerAgent._sanitize_delivery(DSML_BLOCK)
    assert "DSML" not in out
    assert "curl" not in out
    assert "[Memoh]" not in out
    assert "取回那个文件" in out


def test_multiple_dsml_blocks_all_removed():
    text = "先看看。\n" + DSML_BLOCK + "\n再查一次。\n" + DSML_BLOCK
    out = _strip_backend_scaffolding(text)
    assert "DSML" not in out and "curl" not in out
    assert "先看看" in out and "再查一次" in out


def test_unclosed_dsml_tag_removed():
    """截断的标记（生产里 14:35:23 那条就是 `</<｜DSML｜｜ calls>`）也清掉。"""
    text = "[Memoh] <｜DSML｜｜ invoke>\n" '<｜DSML｜｜ parameter name="command">cat /tmp/x.md</<｜DSML｜｜ calls>'
    out = OwnerAgent._sanitize_delivery(text)
    assert "DSML" not in out


@pytest.mark.parametrize("text", [
    "华为云成长中心的积分任务，本喵接着弄喵~",
    "这个文件是 SKILL.md，内容是 12345 字。",
    "本喵查了一下，下次再说。",
])
def test_normal_text_untouched(text):
    assert _strip_backend_scaffolding(text) == text
    assert OwnerAgent._sanitize_delivery(text) == text


def test_empty_after_strip_returns_empty():
    assert _strip_backend_scaffolding("<｜DSML｜｜ calls>x</｜DSML｜｜ calls>") == ""
