"""人格 × 各模块「配合」完善（2026-09-22）。

审计（`reports/2026-09-22_persona_module_integration_audit.md`，只读）定位两处断点：

**A. 主动发言人格贫化（结构级）**
`autonomy._build_active_speech_system` 只喂 `time_context` / `desire_context`，
于是正本里的 `{self_notes}` / `{world_context}` / `{scar_context}` 被
`assemble_persona_system` 替换成**空串**。主动发言是人格最外显的场景
（没人问、自己开口），却看不到自己的**自我笔记**（人格漂移主产物）与
**当前情绪**。本次补 **内容类** 片段（`self_notes` + `mood_note`），
**不动** 2026-09-12 有意去重的**护栏类**片段（身份锁定/角色守卫）——
两者性质不同：前者是"素材"，后者是"最高优先级指令"，重复堆叠才会剧本化。

**C. `conversation_tone` 同 key 双注入（代码级）**
语气检测（`detect_conversation_tone`）与复读/戏弄检测（`_detect_copycat`）
各注入一次、**共用同一个 key**，而 `pipeline/context.compose_system`
**不按 key 去重** ⇒ 同轮可能向 system 塞进两段 `## 对方现在的语气`
（内容不同、标题重复）。本次合并为**单条**注入，两段检测仍各自隔离。

**E（本轮）：主动发言补 `world_context`**
5 个占位符里，`world_context` 是**唯一群级语义有效**的一个：它由
`world_model.prompt_fragment()` 产出、**与 user 无关**。而 `relation` / `scar`
需要 `user_id`（`prompt_fragment(user_id)`），模板里的「这个人」在**群级**
主动发言里指向不明 ⇒ **刻意不注入**（本文件有负向守卫把该决策钉死）。
区别于 B/D：主动发言走**独立 LLM 调用路径、不进 `ContextComposer` 预算池**
⇒ 本项**非零和**，不挤占他人预算。
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from anima.core import autonomy
from anima.pipeline import stages
from anima.pipeline.stages import cognition

_ROOT = Path(__file__).resolve().parents[1]


def _code(rel: str) -> str:
    """剥注释后再断言 —— 解释性注释会把源码守卫变成假红/假绿。"""
    src = (_ROOT / rel).read_text(encoding="utf-8-sig")
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


def _ctx(text: str, messages=None) -> SimpleNamespace:
    """`_conversation_tone_fragment` 只读 ctx.text / ctx.messages。"""
    return SimpleNamespace(text=text, messages=list(messages or []))


# ===========================================================================
# A-1 装配器契约：`_build_active_speech_system` 接受并落地内容类片段
# ===========================================================================

def test_active_speech_injects_self_notes():
    system = autonomy._build_active_speech_system(
        "2026-09-22 05:00 (凌晨)", "欲望片段", "发言守则文本",
        self_notes="## 自我笔记（随经历演化）\n本喵最近在琢磨怎么开罐头",
    )
    assert "本喵最近在琢磨怎么开罐头" in system


def test_active_speech_self_notes_default_blanks_placeholder():
    """不传时占位符必须被替换为空串，绝不残留裸 `{self_notes}`。"""
    system = autonomy._build_active_speech_system("T", "D", "R")
    for key in ("self_notes", "world_context", "scar_context"):
        assert "{" + key + "}" not in system


def test_active_speech_injects_mood_note():
    system = autonomy._build_active_speech_system(
        "T", "D", "R", mood_note="## 你现在的情绪\n有点犯困，懒得搭理人",
    )
    assert "有点犯困，懒得搭理人" in system


def test_active_speech_omits_mood_when_blank():
    """mood 为空（中性不注入）时不得出现空标题。"""
    system = autonomy._build_active_speech_system("T", "D", "R", mood_note="")
    assert "你现在的情绪" not in system


def test_active_speech_backcompat_three_positional_args():
    """向后兼容：既有调用形状（3 个位置参数）不得破。

    `tests/test_persona_single_source.py::test_active_speech_system_uses_yaml_persona`
    依赖这一形状。
    """
    system = autonomy._build_active_speech_system("TIME_X", "DESIRE_X", "RULES_X")
    assert "TIME_X" in system and "DESIRE_X" in system and "RULES_X" in system


def test_active_speech_keeps_guardrail_dedup():
    """护栏类不得因本轮改动回潮：09-12 去重的四段 suffix 仍不在主动发言里。"""
    system = autonomy._build_active_speech_system(
        "T", "D", "R",
        self_notes="## 自我笔记（随经历演化）\n今天被夸了",
        mood_note="## 你现在的情绪\n有点得意",
    )
    assert "身份锁定（最高优先级）" not in system
    assert "角色守卫（最高优先级）" not in system


# ===========================================================================
# A-2 取值接线：`_active_speech_persona_fragments` fail-soft 且真的读模块
# ===========================================================================

def _core(data_dir: str = "/tmp/data") -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(persistence=SimpleNamespace(data_dir=data_dir))
    )


def test_fragments_helper_reads_learning_and_mood(monkeypatch):
    import anima.cognitive.learning as learning
    import anima.cognitive.mood as mood
    import anima.cognitive.world_model as world_model

    monkeypatch.setattr(
        learning, "get_learning",
        lambda *a, **k: SimpleNamespace(prompt_fragment=lambda: "NOTE_X"),
    )
    monkeypatch.setattr(
        mood, "get_mood_system",
        lambda *a, **k: SimpleNamespace(prompt_fragment=lambda: "MOOD_X"),
    )
    monkeypatch.setattr(
        world_model, "get_world_model",
        lambda *a, **k: SimpleNamespace(prompt_fragment=lambda: "WORLD_X"),
    )

    notes, mood_note, world_ctx = autonomy._active_speech_persona_fragments(_core())
    assert notes == "NOTE_X"
    assert mood_note == "MOOD_X"
    assert world_ctx == "WORLD_X"


def test_fragments_helper_fail_soft(monkeypatch):
    """任一子系统炸掉都不得抛出、不得拖垮主动发言。"""
    import anima.cognitive.learning as learning
    import anima.cognitive.mood as mood
    import anima.cognitive.world_model as world_model

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(learning, "get_learning", _boom)
    monkeypatch.setattr(mood, "get_mood_system", _boom)
    monkeypatch.setattr(world_model, "get_world_model", _boom)

    notes, mood_note, world_ctx = autonomy._active_speech_persona_fragments(_core())
    assert notes == "" and mood_note == "" and world_ctx == ""


def test_autonomy_call_site_passes_fragments():
    """源码级守卫：主动发言装配点必须真的把三个片段传下去。"""
    src = _code("anima/core/autonomy.py")
    assert "self_notes=self_notes_ctx" in src
    assert "mood_note=mood_note_ctx" in src
    assert "world_context=world_ctx" in src
    assert "_active_speech_persona_fragments(" in src


def test_active_speech_fragment_log_survives():
    """可观测性守卫：主动发言 system 不落盘，留痕日志是唯一的活体证据面。"""
    src = _code("anima/core/autonomy.py")
    assert "主动发言人格片段" in src
    assert "world=" in src


# ===========================================================================
# C 语气片段：合并为单条
# ===========================================================================

def test_tone_fragment_carries_tone_only():
    frag = cognition._conversation_tone_fragment(_ctx("你滚开！"))
    assert frag.startswith("## 对方现在的语气")
    assert "冲" in frag


def test_tone_fragment_carries_copycat_only():
    """连续短消息戏弄 ⇒ 只有复读提示，仍带统一标题。"""
    frag = cognition._conversation_tone_fragment(_ctx(
        "哈哈哈哈哈哈哈哈哈哈",
        [{"role": "user", "content": "在吗"}, {"role": "user", "content": "在吗"},
         {"role": "user", "content": "在吗"}],
    ))
    assert frag.startswith("## 对方现在的语气")
    assert "戏弄" in frag


def test_tone_fragment_merges_both_hits_into_one_block():
    """同时命中语气与复读 ⇒ **只有一段**，标题只出现一次。"""
    frag = cognition._conversation_tone_fragment(_ctx(
        "我烦死了，滚！你滚开！",
        [{"role": "user", "content": "在吗"}, {"role": "user", "content": "在吗"},
         {"role": "user", "content": "在吗"}],
    ))
    assert frag.count("## 对方现在的语气") == 1
    assert "冲" in frag          # 语气检测的那段
    assert "戏弄" in frag        # 复读检测的那段


def test_tone_fragment_empty_when_nothing_hits():
    frag = cognition._conversation_tone_fragment(_ctx("今天天气不错"))
    assert frag == ""


def test_tone_fragment_isolates_detector_failure(monkeypatch):
    """语气检测炸掉不得吞掉复读检测（反之亦然）—— 两段各自隔离。"""
    import anima.cognitive.mood as mood

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(mood, "detect_conversation_tone", _boom)
    frag = cognition._conversation_tone_fragment(_ctx(
        "哈哈哈哈哈哈哈哈哈哈",
        [{"role": "user", "content": "在吗"}, {"role": "user", "content": "在吗"},
         {"role": "user", "content": "在吗"}],
    ))
    assert "戏弄" in frag


def test_single_conversation_tone_add_site():
    """源码级守卫：`conversation_tone` 只允许有一个注入点（同 key 不得双写）。"""
    src = _code("anima/pipeline/stages/cognition.py")
    assert src.count('add_system_fragment("conversation_tone"') == 1


def test_cognition_uses_tone_fragment_helper():
    src = _code("anima/pipeline/stages/cognition.py")
    assert "_conversation_tone_fragment(ctx)" in src
    assert "_conversation_tone_fragment" in inspect.getsource(stages.cognition)


# ===========================================================================
# E（本轮）：主动发言补 world_context —— 唯一群级语义有效的占位符
# ===========================================================================

def test_active_speech_injects_world_context():
    system = autonomy._build_active_speech_system(
        "T", "D", "R",
        world_context="## 你对世界的看法\n- [事实] 人类喜欢囤积罐头",
    )
    assert "人类喜欢囤积罐头" in system
    assert "{world_context}" not in system


def test_active_speech_world_omitted_when_blank():
    """世界观为空（无达标条目）时不得留下空标题占位。"""
    system = autonomy._build_active_speech_system("T", "D", "R", world_context="")
    assert "你对世界的看法" not in system


def test_active_speech_backcompat_after_world_param():
    """`world_context` 必须带默认值 —— 既有 3 位置参数形状不破。"""
    system = autonomy._build_active_speech_system("TIME_Y", "DESIRE_Y", "RULES_Y")
    assert "TIME_Y" in system and "DESIRE_Y" in system and "RULES_Y" in system


def test_active_speech_tolerates_world_failure(monkeypatch):
    """世界观子系统异常不得拖垮主动发言（它跑在 30s 心跳里）。"""
    import anima.cognitive.learning as learning
    import anima.cognitive.mood as mood
    import anima.cognitive.world_model as world_model

    def _boom(*a, **k):
        raise RuntimeError("world boom")

    monkeypatch.setattr(world_model, "get_world_model", _boom)
    monkeypatch.setattr(
        learning, "get_learning",
        lambda *a, **k: SimpleNamespace(prompt_fragment=lambda: "N"),
    )
    monkeypatch.setattr(
        mood, "get_mood_system",
        lambda *a, **k: SimpleNamespace(prompt_fragment=lambda: "M"),
    )

    notes, mood_note, world_ctx = autonomy._active_speech_persona_fragments(_core())
    assert (notes, mood_note, world_ctx) == ("N", "M", "")


def test_active_speech_never_injects_user_scoped_fragments():
    """负向守卫（**决策锁定**）：主动发言是**群级**出口，没有目标 user。

    `relation.prompt_fragment(user_id)` / `scar.prompt_fragment(user_id)` 是
    user 维度片段 —— 模板里的「这个人」在群级出口会**指向不明**，注入反而制造
    歧义（把某个人的敏感点张冠李戴到整群面前）。故刻意不注入。
    若将来有人把二者接进主动发言，本守卫失败 ⇒ 强制重新决策，而非悄悄溜进去。
    """
    import ast
    import inspect as _inspect

    params = set(_inspect.signature(autonomy._build_active_speech_system).parameters)
    assert "world_context" in params
    assert not ({"scar_context", "relation_ctx", "relation_context"} & params)

    tree = ast.parse((_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8-sig"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_active_speech_persona_fragments"
    )
    # 剥掉 docstring 再比对：解释性文字**允许**说明这个决策，代码里不允许出现这两个模块。
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    assert "scar" not in code and "relation" not in code, code
