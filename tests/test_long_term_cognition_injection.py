"""P0-2：LongTerm → cognition 注入回归测试。

背景：LongTerm 三层（事件→知识→行为模式）的**读**接线原本已存在
（``memory._long_term_query`` → ``cognition`` 注入 ``long_term_context``），
但 ``memory.long_term_context`` 默认 false，QQ 主路径感知不到；
**写**接线则把每条用户消息都沉淀，噪音会把 knowledge/patterns 淹没。

本文件覆盖 P0-2 的两处改动：
1. 写：仅「偏好/决策句」或 Owner 私聊入库，并同步抽到知识层
2. 读：开关默认开，偏好句能被后续会话的 query 命中
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.memory.long_term import LongTermMemory
from anima.pipeline.stages.memory import (
    _is_preference_text,
    _is_owner_private,
    _long_term_should_write,
    _long_term_query,
)


def _core(tmp_path, write_mode: str = "preference", enabled: bool = True):
    return SimpleNamespace(
        config=SimpleNamespace(
            memory=SimpleNamespace(
                long_term_context=enabled,
                long_term_write=write_mode,
            ),
            persistence=SimpleNamespace(data_dir=str(tmp_path)),
        ),
        long_term=None,
    )


def _ctx(detail_type: str = "group", **meta):
    return SimpleNamespace(detail_type=detail_type, meta=dict(meta))


# ---------------------------------------------------------------------------
# 1. 偏好句识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "我喜欢简短回复",
    "我讨厌长篇大论",
    "以后都用中文回答",
    "记住我睡眠不好",
    "默认就别发图了",
    "我的原则是不主动提政治",
])
def test_preference_text_detected(text):
    assert _is_preference_text(text), f"应识别为偏好/决策句: {text}"


@pytest.mark.parametrize("text", [
    "今天天气不错",
    "哈哈哈哈哈",
    "在吗",
    "这个视频好好笑",
])
def test_casual_chat_not_preference(text):
    assert not _is_preference_text(text), f"不应把闲聊当偏好: {text}"


# ---------------------------------------------------------------------------
# 2. 写入筛选（三种模式）
# ---------------------------------------------------------------------------
def test_write_mode_preference_skips_casual_chat(tmp_path):
    core = _core(tmp_path, "preference")
    assert _long_term_should_write(core, _ctx(), "我喜欢简短回复")
    assert not _long_term_should_write(core, _ctx(), "今天天气不错")


def test_write_mode_all_writes_everything(tmp_path):
    core = _core(tmp_path, "all")
    assert _long_term_should_write(core, _ctx(), "今天天气不错")


def test_write_mode_off_writes_nothing(tmp_path):
    core = _core(tmp_path, "off")
    assert not _long_term_should_write(core, _ctx(), "我喜欢简短回复")
    assert not _long_term_should_write(
        core, _ctx("private", permission_level=4), "随便聊聊")


def test_owner_private_always_written(tmp_path):
    """Owner 私聊权重最高——即使句子不像偏好句也沉淀。"""
    core = _core(tmp_path, "preference")
    assert _long_term_should_write(core, _ctx("private", permission_level=4), "随便聊聊")
    assert _long_term_should_write(core, _ctx("private", is_owner=True), "嗯")


def test_owner_private_helper_rejects_group(tmp_path):
    assert not _is_owner_private(_ctx("group", permission_level=4))
    assert not _is_owner_private(_ctx("private", permission_level=2))


# ---------------------------------------------------------------------------
# 3. 读：偏好句写入后能被 query 命中（跨会话「记得」）
# ---------------------------------------------------------------------------
def test_long_term_query_hits_previous_preference(tmp_path):
    core = _core(tmp_path)
    lt = LongTermMemory(str(tmp_path))
    ev = lt.record_event("conversation", "我喜欢简短回复", tags=["conversation", "preference"])
    lt.extract_and_store(ev)
    core.long_term = lt

    res = _long_term_query(core, "简短回复")
    assert res is not None, "偏好句应能被后续 query 命中"
    assert res["has_data"] is True


def test_long_term_query_none_when_disabled(tmp_path):
    """开关关闭 → fail-closed，不注入任何长期记忆。"""
    core = _core(tmp_path, enabled=False)
    lt = LongTermMemory(str(tmp_path))
    lt.record_event("conversation", "我喜欢简短回复", tags=["preference"])
    core.long_term = lt

    assert _long_term_query(core, "简短回复") is None


def test_long_term_query_silent_on_empty_store(tmp_path):
    """空库不编造：返回 None 而非空结构。"""
    core = _core(tmp_path)
    assert _long_term_query(core, "任何问题") is None
