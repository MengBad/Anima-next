"""P1 回归：分段发送语境感知 —— 用户插话调侃时中断后续分段。

- is_teasing_interrupt：判定插话是否为调侃/打断；
- _should_interrupt_for_teasing：封装「上一分段后用户是否插话调侃」的查询，
  供 handler 发送循环在发出后续段前调用。
"""
import pytest

from anima.llm.handler import is_teasing_interrupt, _should_interrupt_for_teasing


# ---------------------------------------------------------------------------
# is_teasing_interrupt
# ---------------------------------------------------------------------------
def test_teasing_detected():
    assert is_teasing_interrupt("你心里话别说出来") is True
    assert is_teasing_interrupt("别自言自语了") is True
    assert is_teasing_interrupt("你跟谁聊呢") is True
    assert is_teasing_interrupt("打住") is True


def test_teasing_not_detected_on_normal():
    assert is_teasing_interrupt("哈哈这个好笑") is False
    assert is_teasing_interrupt("那我们继续聊吧") is False
    assert is_teasing_interrupt("") is False


# ---------------------------------------------------------------------------
# _should_interrupt_for_teasing
# ---------------------------------------------------------------------------
def test_interrupt_true_on_teasing():
    class _Ch:
        def latest_inbound_since(self, gid, ts):
            return (999, "你心里话别说出来")

    assert _should_interrupt_for_teasing(_Ch(), 1, 0.0) is True


def test_interrupt_false_on_normal_reply():
    class _Ch:
        def latest_inbound_since(self, gid, ts):
            return (999, "哈哈这个好笑")

    assert _should_interrupt_for_teasing(_Ch(), 1, 0.0) is False


def test_interrupt_false_when_no_inbound():
    class _Ch:
        def latest_inbound_since(self, gid, ts):
            return None

    assert _should_interrupt_for_teasing(_Ch(), 1, 0.0) is False


def test_interrupt_false_when_channel_none():
    assert _should_interrupt_for_teasing(None, 1, 0.0) is False


def test_interrupt_false_when_channel_lacks_buffer():
    class _Ch:
        pass

    assert _should_interrupt_for_teasing(_Ch(), 1, 0.0) is False
