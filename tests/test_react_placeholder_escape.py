"""占位串逃逸（placeholder escape）回归测试 —— 2026-09-18。

## 缺陷

`ReActLoop.run()` 在「本轮 content 空 + `_finalize_content()` 纯对话重试也空」
（上游全候选皆空）时会返回**内部占位串** `（思考中...）`：

    anima/agent/react.py:166
        return self._apply_freshness_footer(content or "（思考中...）")

两个独立问题把它放大成用户可见缺陷：

**① 装饰让下游守卫失效。** `_apply_freshness_footer()` 是**追加**（`content + _FRESHNESS_FOOTER`），
于是出口变成 `（思考中...）（以上来自联网检索；……）`；而下游两处守卫都按**精确相等**
判定（`reply.strip() in {"（想了一下不知道说什么...）", "（思考中...）"}`）→ 装饰后**不再命中**
→ 占位串原样直达用户。

**② 三个消费方，只有一个有守卫。**

| 消费方 | 守卫 | 后果 |
|---|---|---|
| `pipeline/stages/generation.py:718/756` | 有（精确相等）→ 被装饰后失效 | 用户看到占位串 |
| `agent/executor.py:447` | **无** | 占位串成为该 step 的「结果」 |
| `core/tasks.py:156` | **无** | 占位串进 `task.evidence`，且任务被判 **`completed`** |

⇒ 修法必须落在**源头**（ReAct 不再产出占位串），并把守卫收敛成**能容忍装饰的共享判定**；
只在 `generation.py` 补一个字符串是治不好的。

## 本文件钉住的行为

1. ReAct 在「全空」时不得返回占位串（含被装饰的）；
2. `_apply_freshness_footer()` 不得装饰占位串；
3. `is_placeholder_reply()` 必须容忍装饰（前缀判定，不是精确相等）；
4. **机制守卫**：全库不得再出现 `{"（想了一下不知道说什么...）", "（思考中...）"}` 这种精确集合守卫；
5. `OwnerTaskAgent` 不得把占位串结果判成 `completed`。
"""

import re
from pathlib import Path

import pytest

from anima.agent import react as react_mod
from anima.agent.react import ReActLoop, _FRESHNESS_FOOTER
from anima.tools import LEVEL_USER, Tool, ToolRegistry

ROOT = Path(__file__).resolve().parent.parent

#: 缺陷出口的真实形态（占位串 + 时效脚注直接相接，`_FRESHNESS_FOOTER` 无前导换行）
#: 2026-09-23：脚注文案已改（去报告体），此处**从实时模板派生**，避免夹具与文案漂移
#: 后夹具变成「历史形态」而断言假绿。
FOOTER_SAMPLE = _FRESHNESS_FOOTER.format(date="2026-09-18")
DECORATED_PLACEHOLDER = f"（思考中...）{FOOTER_SAMPLE}"
PLACEHOLDER = "（思考中...）"
LEGACY_PLACEHOLDER = "（想了一下不知道说什么...）"


def _predicate():
    """新 API 尚不存在时返回 None（让测试**失败**而不是收集期报错，RED 信号更清楚）。"""
    return getattr(react_mod, "is_placeholder_reply", None)


class AllEmptyLLM:
    """首轮要求搜网（成功产事实），此后 content 与纯对话重试**都为空**。

    对应上游「全候选皆空」：09-18 已把空补全纳入 failover，但**所有**候选都空时仍会
    走到这里 —— 这正是占位串唯一的可达路径。
    """

    def __init__(self):
        self.structured_rounds = 0

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
        self.structured_rounds += 1
        if self.structured_rounds == 1:
            return {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "web_search", "arguments": '{"q": "天气"}'}}
            ]}
        return {"content": "", "tool_calls": []}

    async def chat(self, messages, temperature=0.8, max_tokens=512):
        return ""


class NoToolAllEmptyLLM(AllEmptyLLM):
    """一个工具都不调，且 content / 纯对话重试皆空。"""

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
        self.structured_rounds += 1
        return {"content": "", "tool_calls": []}


def make_registry():
    registry = ToolRegistry()
    registry.register(Tool(
        "web_search", "search the web", {"q": "query"},
        lambda q="": "搜索结果：今天晴，18℃", min_level=LEVEL_USER,
    ))
    return registry


# --------------------------------------------------------------------------- 1
@pytest.mark.asyncio
async def test_all_empty_after_freshness_tool_does_not_escape_placeholder():
    """搜网成功但最终 content 与重试皆空 → 出口绝不能是（含被装饰的）占位串。"""
    react = ReActLoop(AllEmptyLLM(), make_registry(), permission_level=LEVEL_USER)
    result = await react.run([{"role": "system", "content": "test"}])

    assert PLACEHOLDER not in result, f"占位串逃逸: {result!r}"
    assert LEGACY_PLACEHOLDER not in result
    assert result.strip(), "必须有面向用户的出口，不能是空串"


# --------------------------------------------------------------------------- 2
@pytest.mark.asyncio
async def test_all_empty_without_tools_does_not_escape_placeholder():
    """无工具调用、全空的路径（react.py:162-166 早退）同样不得吐出占位串。"""
    react = ReActLoop(NoToolAllEmptyLLM(), make_registry(), permission_level=LEVEL_USER)
    result = await react.run([{"role": "system", "content": "test"}])

    assert PLACEHOLDER not in result, f"占位串逃逸: {result!r}"
    assert result.strip()


# --------------------------------------------------------------------------- 3
def test_freshness_footer_does_not_decorate_placeholder():
    """时效脚注是给**真实回答**补时间口径的；占位串不是回答，不得被装饰。

    装饰是本次缺陷的关键放大器：它让下游精确相等守卫静默失效。
    """
    react = ReActLoop(AllEmptyLLM(), make_registry(), permission_level=LEVEL_USER)
    react._freshness_tools_used.add("web_search")  # 模拟本轮查过外部事实

    out = react._apply_freshness_footer(PLACEHOLDER)

    assert FOOTER_SAMPLE not in out, f"占位串被脚注装饰了: {out!r}"
    out2 = react._apply_freshness_footer(LEGACY_PLACEHOLDER)
    assert FOOTER_SAMPLE not in out2


# --------------------------------------------------------------------------- 4
def test_is_placeholder_reply_handles_decoration():
    """共享判定必须容忍装饰 —— 装饰只能是**追加**，所以用前缀判定而非精确相等。"""
    pred = _predicate()
    assert pred is not None, "react.is_placeholder_reply 不存在（守卫仍是精确相等集合）"

    assert pred(PLACEHOLDER) is True
    assert pred(DECORATED_PLACEHOLDER) is True          # ★ 本次缺陷的核心
    assert pred(LEGACY_PLACEHOLDER) is True
    assert pred(f"  {PLACEHOLDER}  ") is True           # 空白容忍
    assert pred("") is True                             # 空 = 无内容
    assert pred("   ") is True
    assert pred("今天晴，18℃") is False
    assert pred(FOOTER_SAMPLE) is False


# --------------------------------------------------------------------------- 5
def test_no_exact_placeholder_set_guard_remains():
    """机制守卫：全库不得再有精确集合形式的占位守卫。

    产生端一改，将来若又有人把装饰加回来，精确守卫就会**静默失效**；
    这条测试让「守卫退化为精确相等」在 CI 里直接变红。

    ⚠️ 扫描**包含注释**（不剥注释）—— 这是刻意的 fail-closed 取向：注释里复现该形式
    同样会红，代价是改一句注释，收益是不必依赖 tokenize 剥注释的正确性。
    因此**注释里不要复现精确集合的写法**（描述成「精确集合」即可）。

    ⚠️ **域 = 仓库树（`ROOT/anima`），这是刻意的**：仓库树 = 实际投递的代码。
    生产上还存在**仓库没有**的历史遗留文件（`anima/pipeline/stages.py`，2204 行、
    被 `pipeline/stages/` 包遮蔽的死代码），它带着同一份陈旧守卫。那是死代码，
    且本次修复在**源头**（ReAct 不再产出占位串）→ 死代码里的陈旧守卫无害。
    **不要**为了让它变绿而把域放宽成「生产树里所有 .py」—— 那会让门去管不该管的东西。
    """
    # ⚠️ 注意串里有**全角右括号** `）`：`"（思考中...）"` 的 `）` 在引号**内**。
    # 首版正则漏掉它 → 守卫恒过（假绿）。下面 _SAMPLE 就是防这种腐烂的自检。
    exact_set = re.compile(
        r"\{\s*[\"']（想了一下不知道说什么\.\.\.）[\"']\s*,\s*[\"']（思考中\.\.\.）[\"']\s*\}"
        r"|\{\s*[\"']（思考中\.\.\.）[\"']\s*,\s*[\"']（想了一下不知道说什么\.\.\.）[\"']\s*\}"
    )
    # 自检：正则必须真的能命中历史形态，否则这条守卫是假的
    _SAMPLE = 'if reply.strip() in {"（想了一下不知道说什么...）", "（思考中...）"}:'
    assert exact_set.search(_SAMPLE), "守卫正则失效（无法命中历史形态）—— 假绿风险"

    offenders = []
    for path in sorted((ROOT / "anima").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if exact_set.search(text):
            offenders.append(str(path.relative_to(ROOT)).replace("\\", "/"))
    assert not offenders, "这些文件仍在用精确集合判占位串（应改用 is_placeholder_reply）：%s" % offenders


# --------------------------------------------------------------------------- 6
@pytest.mark.asyncio
async def test_owner_task_agent_not_completed_on_placeholder(monkeypatch, tmp_path):
    """占位串进 `core/tasks.py` 时不得把任务判成 `completed`（假完成）。

    `core/tasks.py:156-161` 对 `ReActLoop.run()` 的返回值**没有占位守卫**：
    占位串会进 `task.evidence`，同时 `task.status = "completed"`。
    """
    import types

    from anima.core.persistence import PersistenceProvider
    from anima.core.tasks import OwnerTaskAgent, TaskStore

    store = TaskStore(PersistenceProvider(str(tmp_path)))
    task = store.create("整理一份报告", 42, ["收集材料"])

    class PlaceholderLoop:
        tool_trace = [{"result": "ok", "ok": True}]

        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return DECORATED_PLACEHOLDER

    monkeypatch.setattr("anima.agent.react.ReActLoop", PlaceholderLoop)
    core = types.SimpleNamespace(tasks=store, llm=object(), tools=object())
    result = await OwnerTaskAgent(core).execute(task)

    assert PLACEHOLDER not in result, f"占位串进了任务结果: {result!r}"
    assert store.get(task.id).status != "completed", "空结果被记成「已完成」"
