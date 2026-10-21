"""写文件产物贴回会话契约（2026-10-21）。

## 起因（owner 实测提出）

它把分析结果写到 `/opt/anima/data/output/desires_summary.txt`，然后在 QQ 里回：

> 「具体的小结本喵都写在文件里啦，自己去看吧喵~」

**但那个文件在服务器上，用户在 QQ 里根本打不开。** 用户什么也没拿到。

根因：`write_file` 只返回 `已生成文件: <绝对路径>`（服务器路径），
出站层没有任何「把产物内容贴回会话」的通道 —— 生图有
（`generated_image_paths`），文本没有。

## 本改动

1. `write_file` 把 `{name, content}` 登记进 tool call context 的
   `generated_text_files`；
2. generation 阶段把它同步到 `ctx.generated_text_files`（与生图同一手法）；
3. `_build_reply_segments` 调 `_format_text_attachments()` 把内容贴到回复末尾。

## 本测试锁死的边界

1. 短内容 ⇒ 原样贴出，带文件名；
2. 超预算 ⇒ 截断 + 如实说明「全文已保存到文件里」（不假装完整）；
3. **绝不贴服务器绝对路径**（用户打不开，且属内部细节）；
4. 空内容/非法条目 ⇒ 安全跳过，不崩；
5. 无写文件时 ⇒ 完全不影响常规回复（零行为变更）。
"""
import types

from anima.llm.handler import MAX_TEXT_ATTACH_CHARS, _format_text_attachments
from anima.tools.builtin.file import make_file_tool


# ── 1. 格式化逻辑 ─────────────────────────────────────────────────────


def test_short_content_attached_verbatim():
    out = _format_text_attachments([{"name": "a.txt", "content": "短内容"}], 600)
    assert "a.txt" in out
    assert "短内容" in out


def test_over_budget_truncated_with_honest_note():
    out = _format_text_attachments([{"name": "b.txt", "content": "x" * 1000}], 100)
    assert "全文已保存到文件里" in out, "超预算必须如实说明，不能假装完整"
    assert "…" in out


def test_never_leaks_server_absolute_path():
    """🔴 用户打不开服务器路径，且属内部细节 —— 绝不贴。"""
    out = _format_text_attachments(
        [{"name": "c.txt", "content": "ok", "path": "/opt/anima/data/output/c.txt"}], 600
    )
    assert "/opt/anima" not in out
    assert "data/output" not in out


def test_empty_and_malformed_entries_safe():
    assert _format_text_attachments([], 600) == ""
    assert _format_text_attachments([None, "x", {}], 600) == ""
    assert _format_text_attachments([{"name": "e.txt", "content": "   "}], 600) == ""


def test_multiple_files_under_budget_all_shown():
    out = _format_text_attachments(
        [{"name": "1.txt", "content": "AAA"}, {"name": "2.txt", "content": "BBB"}], 600
    )
    assert "1.txt" in out and "2.txt" in out
    assert "AAA" in out and "BBB" in out


# ── 2. write_file 登记进 call context ─────────────────────────────────


def test_write_file_registers_text_artifact(tmp_path):
    """write_file 必须把内容登记进 call context，否则出站层拿不到。"""
    from anima.tools import get_call_context
    from anima.tools import _CALL_CONTEXT

    core = types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path))
        )
    )
    tool = make_file_tool(core)

    token = _CALL_CONTEXT.set({})
    try:
        ret = tool("note.txt", "分析结论：一切正常")
        ctx = get_call_context()
    finally:
        _CALL_CONTEXT.reset(token)

    assert "已生成文件" in ret
    files = ctx.get("generated_text_files") or []
    assert len(files) == 1, f"应登记 1 个文本产物，实得 {files}"
    assert files[0]["name"] == "note.txt"
    assert files[0]["content"] == "分析结论：一切正常"


def test_write_file_still_writes_disk(tmp_path):
    """登记产物不得影响落盘本身。"""
    core = types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path))
        )
    )
    make_file_tool(core)("x.txt", "内容")
    assert (tmp_path / "output" / "x.txt").read_text(encoding="utf-8") == "内容"


# ── 3. 零行为变更（无产物时不改动回复）──────────────────────────────────


def test_no_artifacts_means_no_change():
    """没有写文件时，格式化结果为空 ⇒ 出站层不会改动 reply。"""
    assert _format_text_attachments([], MAX_TEXT_ATTACH_CHARS) == ""
