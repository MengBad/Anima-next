# -*- coding: utf-8 -*-
"""r27：时效披露门的**两个静默失效**（2026-10-06 生产审计发现）。

## ① 名字写错：`_FRESHNESS_TOOLS` 写的是 **Provider id**，LLM 调的是 **schema name**

生产事故：集合里写 ``web_search``，而 LLM 实际调用的是 ``search_web``。

- Tool ``web_search`` 挂了 ``capability_ids=["search.web"]``
  ⇒ capability 模式下**其 Tool 名对 LLM 不可见**，LLM 看到的是
  ``schema_name_for("search.web") == "search_web"``（`capability/index.py:26`）。
- **7d journal 实证**：``name=web_search`` **0** 次；``name=search_web`` **22** 次。
- 后果：联网搜索回合**既不注入时效约束、也不追加时效脚注** ——
  正是那句注释要防的「让无日期的检索结论看起来像永恒事实」。

🔴 **为什么原来的判据全绿**：`test_web_failover_freshness_p23.py` 里有 8 处
``_tool_call("web_search", …)`` —— **判据与生产常量用了同一个错名字**，
两边永远一致 ⇒ 测试永远绿、生产永远坏。
⇒ 本文件的守卫**锚在真实注册表上**（``create_default_registry().get_schemas()``），
不拿被测常量去构造输入。

## ② 触发条件太松：`ok=True` 不等于「取到内容」

``_tool_result_ok()`` 判的是「非空 ∧ 无失败标记」⇒ **「本视频没有字幕」这类
「成功但没内容」也过**，于是脚注照加，出现

    正文：「这个视频没挂字幕，本喵抓不到内容喵！」
    脚注：「（刚在网上翻的喵，那边没写日期，就当是 2026-10-06 看到的吧）」

**72h 实测 14 条脚注里 12 条如此**；且 72h 内 ``ok=False`` = **0** 次 ——
这不是工具失败，是「成功但没内容」。

⇒ r27 增加 ``_has_usable_content`` 门，阈值取自生产 ``result_len`` 的**双峰分布**
（7d，60 次调用）：无内容 19~95 字符、有内容 309~13019 字符 ⇒ 取 **200** 落在空档。
"""
from __future__ import annotations

import pytest

from anima.agent.react import (
    _FRESHNESS_FOOTER,
    _FRESHNESS_GUIDANCE,
    _FRESHNESS_MIN_CONTENT_CHARS,
    _FRESHNESS_TOOLS,
    ReActLoop,
    _has_usable_content,
)
from anima.capability.index import schema_name_for
from anima.tools import LEVEL_USER, create_default_registry


# ======================================================================
# ① 名字守卫：必须锚在**真实注册表**上
# ======================================================================

#: 不在 `create_default_registry()` 里、由运行期动态注册的时效工具。
#: 显式列出，**防止名字拼错后被 `continue` 静默跳过守卫**。
_DYNAMICALLY_REGISTERED = {
    "bilibili_read",          # 由 builtin 在 core 就绪后挂载
    "skill_web_brief",        # 由 SkillRegistry 依 manifest 注册
    "skill_member_profile",
    "skill_group_roster",
}


def _llm_facing_names() -> set[str]:
    """LLM 真正会看到/调用的 function name 集合（与生产同一构造路径）。"""
    reg = create_default_registry()
    return {s["function"]["name"] for s in reg.get_schemas(LEVEL_USER, capability_mode=True)}


def test_freshness_tools_are_llm_facing_names():
    """🔴 核心守卫：`_FRESHNESS_TOOLS` 里凡注册表认识的名字，必须是 LLM 会调用的名字。

    本条在修复前**必然失败** —— 它锚在真实注册表上，不依赖被测常量自证。
    """
    reg = create_default_registry()
    llm_names = _llm_facing_names()
    bad = []
    for name in sorted(_FRESHNESS_TOOLS):
        tool = reg.tools.get(name)
        if tool is None:
            continue                     # 动态注册，见下一条判据
        if name not in llm_names:
            cids = getattr(tool, "capability_ids", None) or []
            hint = f"（该 Tool 声明 capability_ids={cids}，" \
                   f"LLM 看到的是 {schema_name_for(cids[0])!r}）" if cids else ""
            bad.append(f"{name!r} 不在 LLM 名字里{hint}")
    assert not bad, (
        "时效工具名与 LLM 实际调用的名字不符 ⇒ 时效约束与脚注会**静默失效**：\n  "
        + "\n  ".join(bad)
    )


def test_provider_id_web_search_is_not_the_llm_name():
    """把这次的根因**逐字钉住**：`web_search` 是 Provider id，不是 LLM 名字。"""
    llm_names = _llm_facing_names()
    assert "search_web" in llm_names, "LLM 应看到 search_web"
    assert "web_search" not in llm_names, (
        "web_search 是 Provider/Tool id（挂了 capability_ids=['search.web']），"
        "capability 模式下对 LLM 不可见"
    )
    assert "search_web" in _FRESHNESS_TOOLS
    assert "web_search" not in _FRESHNESS_TOOLS, (
        "把 Provider id 写进 _FRESHNESS_TOOLS ⇒ 时效门对它永不生效（2026-10-06 生产事故）"
    )


def test_every_freshness_name_is_accounted_for():
    """每个 freshness 名要么是注册表 Tool 名、要么是 LLM schema 名、要么在**显式列出的动态集**里。

    防的是「名字拼错 ⇒ 三处都查不到 ⇒ 上一条 `continue` 静默放行」。
    """
    reg = create_default_registry()
    llm_names = _llm_facing_names()
    unknown = {
        n for n in _FRESHNESS_TOOLS
        if n not in reg.tools and n not in llm_names and n not in _DYNAMICALLY_REGISTERED
    }
    assert not unknown, (
        f"未归类的时效工具名（拼错了？或新增未登记）：{sorted(unknown)}"
    )


# ======================================================================
# ② 内容门：`ok=True` ≠ 「取到内容」
# ======================================================================

#: 生产实测**无内容**结果的形态与量级（19~95 字符）。
_BILIBILI_NO_SUBTITLE = "本视频没有字幕，无法提取内容。"
_EMPTY_X_SEARCH = "没有找到相关结果"


def test_has_usable_content_rejects_contentless_results():
    """生产那三种「成功但没内容」的形态都必须**过不了内容门**。"""
    for sample in (_BILIBILI_NO_SUBTITLE, _EMPTY_X_SEARCH, "", "ok", "   "):
        assert not _has_usable_content(sample), f"无内容结果被误判为有内容：{sample!r}"


def test_has_usable_content_accepts_production_lengths():
    """生产实测**有内容**结果的量级（309~13019）必须**过门**。"""
    for n in (309, 1240, 3362, 13019):
        assert _has_usable_content("字" * n), f"{n} 字符的真实结果被误拦"


def test_threshold_sits_in_the_measured_gap():
    """🔴 阈值必须落在生产实测的**双峰空档**里（无内容上界 95 / 有内容下界 309）。

    这条把「阈值是拍脑袋定的」变成可证伪的：一旦有人改动阈值越过实测边界，
    判据会红，强制回去看真实分布。
    """
    contentless_upper = 95      # 7d 实测：bilibili_read / x_search / query_web_memory 的最大值
    substantive_lower = 309     # 7d 实测：x_search 的最小真实结果
    assert contentless_upper < _FRESHNESS_MIN_CONTENT_CHARS < substantive_lower, (
        f"阈值 {_FRESHNESS_MIN_CONTENT_CHARS} 不在实测空档 ({contentless_upper}, {substantive_lower}) 内"
    )


# ----------------------------------------------------------------------
# ②b 端到端：内容门真的改变了 ReAct 行为
# ----------------------------------------------------------------------

def _tool_call(name, args="{}", call_id="c1"):
    return {"id": call_id, "function": {"name": name, "arguments": args}}


class _ScriptedLLM:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
        self.calls.append([m.get("content") for m in messages])
        return self.steps.pop(0) if self.steps else {"content": "兜底", "tool_calls": []}

    async def chat(self, messages, temperature=0.8, max_tokens=512, **kw):
        return "兜底"


class _CtxComposer:
    def compact_tool_result(self, text, **kw):
        return text


def _make_loop(llm, result):
    class _Tools:
        def get_schemas(self, level, **kw):
            return []

        async def call(self, name, args, level):
            return result

    loop = ReActLoop(llm, _Tools(), max_rounds=3, permission_level=LEVEL_USER)
    loop._context_composer = _CtxComposer()
    return loop


@pytest.mark.asyncio
async def test_contentless_tool_result_adds_no_footer():
    """🔴 BUG B 的回归：工具**没取到内容**时，不得追加时效脚注。

    修复前：正文「没抓到内容」+ 脚注「刚在网上翻的…就当是今天看到的吧」= 自相矛盾。
    """
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("bilibili_read", '{"url":"x"}')]},
        {"content": "这个视频没挂字幕，本喵抓不到内容喵。", "tool_calls": []},
    ])
    loop = _make_loop(llm, _BILIBILI_NO_SUBTITLE)
    out = await loop.run([{"role": "user", "content": "这啥"}])

    assert out == "这个视频没挂字幕，本喵抓不到内容喵。", \
        f"没取到内容却加了脚注 ⇒ 自相矛盾：{out!r}"
    assert _FRESHNESS_FOOTER.split("{date}")[0] not in out


@pytest.mark.asyncio
async def test_contentless_tool_result_injects_no_guidance():
    """没取到内容时连时效约束也不该注入（约束是给「有材料」的回合用的）。"""
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("bilibili_read", "{}")]},
        {"content": "没拿到内容", "tool_calls": []},
    ])
    loop = _make_loop(llm, _BILIBILI_NO_SUBTITLE)
    await loop.run([{"role": "user", "content": "q"}])

    second = llm.calls[1]
    assert not any((m or "") == _FRESHNESS_GUIDANCE for m in second), \
        "无内容的回合不应注入时效约束"


@pytest.mark.asyncio
async def test_substantive_tool_result_still_adds_footer():
    """正向对照：**真取到内容**时脚注照旧要加（内容门不能把该响的也掐掉）。"""
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("search_web", '{"query":"x"}')]},
        {"content": "结论是甲。", "tool_calls": []},
    ])
    loop = _make_loop(llm, "搜索结果正文。" * 40)      # 240 字符
    out = await loop.run([{"role": "user", "content": "q"}])

    assert out.startswith("结论是甲。")
    assert _FRESHNESS_FOOTER.split("{date}")[0] in out, "取到内容时脚注不该消失"
