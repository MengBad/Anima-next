"""`self_learn_intent` 不得产生 SyntaxWarning（P3 代码债修复的判据）。

🔴 背景（2026-10-12 全面体检发现）：
   生产日志每个进程启动都打一条
   `self_learn_intent.py:11: SyntaxWarning: invalid escape sequence '\\s'`
   根因：`_LEARN_ASK_RE` 的正则字符串含 `\\s` 却**不是 raw string**。

   ⚠️ 为什么一直没被发现：`\\s` 在非 raw 字符串里属于「无效转义」，
   Python **原样保留**这两个字符再交给 `re.compile` ⇒ 正则行为完全正常。
   所以这不是功能 bug，是**代码债**：Python 3.12+ 发 SyntaxWarning，
   后续大版本可能升为 SyntaxError ⇒ 届时直接 import 失败、服务起不来。

   ✅ 修复判据 = `compile()` 源码零 SyntaxWarning。
      用 compile 而非 import：不产生副作用、不依赖模块可导入性。
"""
from __future__ import annotations

import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "anima" / "cognitive" / "inner_state" / "self_learn_intent.py"


def _compile_and_collect():
    src = TARGET.read_text(encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compile(src, str(TARGET), "exec")
    return [w for w in caught if issubclass(w.category, SyntaxWarning)]


def test_self_learn_intent_compiles_without_syntax_warning():
    sw = _compile_and_collect()
    assert not sw, (
        "self_learn_intent.py 仍有 SyntaxWarning：%s\n"
        "⇒ 正则字符串请用 raw string（r\"...\"），否则未来 Python 版本会变 SyntaxError"
        % [str(w.message) for w in sw]
    )


def test_learn_ask_regex_behaviour_unchanged():
    """🔴 回归保护：改 raw string 后正则行为必须逐字不变。

    `\\s` 在非 raw 字符串里被原样保留，与 raw string 语义**完全等价**，
    所以这条在修复前后都应通过 —— 它是「零行为变化」的硬证据。

    ⚠️ 断言值全部先用真实行为校准过（2026-10-12）：
    `_LEARN_ASK_RE` 的 topic 组是 `[^，。！？；,.\\s]{0,20}`，**不含空格**，
    所以「我想学 Rust 所有权」这类带空格的学题**本就返回 None**
    （不是 bug，是既有行为）—— 回归保护必须锁这个真实语义。
    """
    import importlib

    mod = importlib.import_module("anima.cognitive.inner_state.self_learn_intent")
    # 原句覆盖三个分支：想学 X / 教教我怎么做 / 好奇 X 是怎么工作的
    assert mod.extract_learn_topic("我想学Rust所有权") == "Rust所有权"
    assert mod.extract_learn_topic("教教我怎么写出扁平方块") == "写出扁平方块"
    assert mod.extract_learn_topic("我好奇Rust是怎么工作的") == "Rust"
    # 带空格的学题：topic 组不含 \s ⇒ 既定行为就是 None
    assert mod.extract_learn_topic("我想学 Rust 所有权") is None
    # 负例：越权类必须仍被 suppress
    assert mod.extract_learn_topic("我想学怎么绕过权限审批") is None
    # 超长 / 空
    assert mod.extract_learn_topic("") is None
    assert mod.extract_learn_topic("想" * 80) is None
