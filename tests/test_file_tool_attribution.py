"""文件工具回执归因契约（2026-10-21）：「读不了」不许被说成「不存在」。

## 背景（两次生产实证）

A) 文件**过大**（900KB > 200KB 上限）此前与「路径非法」同为裸 `ValueError`
   ⇒ 模型把「读不了」误解成「不存在」，该误解还被**验收器固化成失败理由**
   （实证：任务 `e65e69ac31` 2 次重规划后失败，失败理由写成
   「文件根本不存在」—— 而 health_history.json 确实存在，只是太大）。

B) `list_workspace_files` 只返回最近修改的 ``limit`` 个，D-37 的回执把它当
   **完整清单**交给模型 ⇒ 模型拿到残缺清单后误判文件不存在（实证：
   health_history.json 因修改时间不在最新 40 名内被截掉）。

## 本测试锁死的边界

1. 「过大」→ 给**可操作回执**（不抛异常），回执必须说「文件确实存在」；
2. 「路径非法」→ 仍如实抛 `ValueError`（这类是调用方的错，绝不软化）；
3. 文件清单被截断 → 回执必须带「共 N 个 / 只列出 M 个 / 未列出≠不存在」；
4. 未截断时 → 不出现"未列出的不等于不存在"这类误导噪音；
5. **成功路径零改动**：能正常读的文件，返回内容一字不变。

刻意不测：路径非法以外的其他 ValueError（保持原样如实抛）。
"""
from anima.tools.builtin.file import (
    make_read_file_tool,
    list_workspace_files,
    count_workspace_files,
    read_file,
)


class _Cfg:
    def __init__(self, data_dir):
        self.persistence = _Persist(data_dir)


class _Persist:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Core:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)


# ── 1. 「过大」给可操作回执，且必须说明文件确实存在 ─────────────────────


def test_oversize_file_returns_actionable_receipt(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "big.json").write_text("x" * 5000, encoding="utf-8")

    core = _Core(str(d))
    out = make_read_file_tool(core)(  # 上限 100，文件 5000 → 过大
        "big.json", max_bytes=100
    )

    # 走既有白名单机制（D1 口径），不是自由散文
    assert "工具暂时不可用" in out, f"应走 tool_unavailable：{out!r}"
    assert "过大" in out and "超出单次读取上限" in out, f"必须说明真实原因：{out!r}"
    assert "文件确实存在" in out, f"必须澄清文件存在（否则模型会误判不存在）：{out!r}"
    # 可选项
    assert "分片读取" in out or "其他工具" in out, f"必须给可操作建议：{out!r}"


def test_oversize_receipt_tells_model_not_to_claim_missing(tmp_path):
    """回执要明确禁止「声称文件不存在」—— 实证里模型正是这么干的。"""
    d = tmp_path / "data"
    d.mkdir()
    (d / "big.json").write_text("y" * 3000, encoding="utf-8")

    core = _Core(str(d))
    out = make_read_file_tool(core)("big.json", max_bytes=10)

    assert "不要用「文件不存在」来解释" in out, f"必须显式禁止该误判：{out!r}"


def test_oversize_receipt_carries_no_internal_detail(tmp_path):
    """🔴 D1 边界：回执不得夹带字节数/路径/异常名等内部细节。"""
    d = tmp_path / "data"
    d.mkdir()
    (d / "big.json").write_text("z" * 4000, encoding="utf-8")

    core = _Core(str(d))
    out = make_read_file_tool(core)("big.json", max_bytes=50)

    # 具体字节数与绝对路径都属内部细节，不得进模型可见串
    assert "4000" not in out and "50" not in out, f"不得带具体字节数：{out!r}"
    assert str(d) not in out, f"不得带绝对路径：{out!r}"
    assert "Value" not in out and "Error" not in out, f"不得带异常名：{out!r}"


# ── 2. 「路径非法」仍如实抛出，绝不软化 ──────────────────────────────────


def test_illegal_path_still_raises(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "ok.txt").write_text("hi", encoding="utf-8")

    core = _Core(str(d))
    tool = make_read_file_tool(core)
    try:
        tool("../../etc/passwd")
    except ValueError as exc:
        assert "过大" not in str(exc), "路径非法必须保持原样抛出，不得被当成过大"
    else:
        raise AssertionError("路径非法应当抛出 ValueError（这类是调用方的错）")


# ── 3. 清单截断必须说出口 ───────────────────────────────────────────────


def test_truncated_listing_declares_itself_incomplete(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    # 造 60 个文件，远超默认 limit=40
    for i in range(60):
        (d / f"f{i}.txt").write_text("x", encoding="utf-8")

    available = list_workspace_files(str(d))
    total = count_workspace_files(str(d))

    assert total == 60, f"总数应为 60，实得 {total}"
    assert len(available) < total, "前置：清单必须被截断才能验证提示"

    # 触发 FileNotFoundError 拿到回执
    core = _Core(str(d))
    out = make_read_file_tool(core)("nonexistent.json")

    assert f"共 {total} 个文件" in out, f"回执必须给出真实总数：{out!r}"
    assert "未列出的不等于不存在" in out, f"必须声明清单残缺：{out!r}"
    assert "可能被截断" in out, f"必须提示可能被截断：{out!r}"


def test_untruncated_listing_has_no_misleading_noise(tmp_path):
    """未截断时不许出现「未列出≠不存在」—— 那会教模型怀疑清单。"""
    d = tmp_path / "data"
    d.mkdir()
    for i in range(5):
        (d / f"f{i}.txt").write_text("x", encoding="utf-8")

    core = _Core(str(d))
    out = make_read_file_tool(core)("nonexistent.json")

    assert len(list_workspace_files(str(d))) == count_workspace_files(str(d)), "前置：未截断"
    assert "未列出的不等于不存在" not in out, f"未截断时不该有该提示：{out!r}"


# ── 4. 成功路径零改动 ──────────────────────────────────────────────────


def test_success_path_unchanged(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.txt").write_text("hello 世界", encoding="utf-8")

    core = _Core(str(d))
    out = make_read_file_tool(core)("a.txt")

    assert out == "hello 世界", f"成功路径必须逐字节不变：{out!r}"


def test_read_file_direct_success(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "b.txt").write_text("raw", encoding="utf-8")

    assert read_file("b.txt", str(d)) == "raw"
