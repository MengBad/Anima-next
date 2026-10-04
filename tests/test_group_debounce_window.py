# -*- coding: utf-8 -*-
"""群防抖窗口（2026-10-03 调参）：默认窗口收紧 + 合并可观测。

背景（只读取证，72h / 1454 条群消息）：
- 同群同用户相邻间隔仅 **11.2% ≤ 2s**（median 37s）；
- 离线复现 GroupDebouncer：窗口 2.0s→1.0s 时回合数仅 1307→1359（**+4%**，且多出的回合
  大多仍被闸门 skip），却让**每条群消息少等 1.0s**（p50 `entry_to_pipeline` 2354ms）；
- 短句等待从 3.0s → 1.5s（聊天里短句占多数，收益最大）。

⇒ 结论：原 2.0s 窗口**过度配置**；收紧到 1.0s，并让「合并了几条」可观测
（此前 `GroupDebouncer` 完全不打日志，合并率只能靠离线模拟猜 —— 本次审核踩到）。
"""
import asyncio
import logging
import time

import pytest

from anima.core.conversation_assembler import GroupDebouncer, _PendingConversation


def _pending(text: str) -> _PendingConversation:
    p = _PendingConversation(events=[{"text": text}])
    p.first_at = time.monotonic()
    return p


# ── 1. 默认窗口 = 1.0s（本次调参的锁定值）─────────────────────────────

def test_default_group_wait_is_one_second():
    d = GroupDebouncer()
    assert d.wait_sec == pytest.approx(1.0), "群防抖默认窗口应为 1.0s（2026-10-03 调参）"
    assert d.adaptive is True


def test_default_group_wait_via_config():
    """代码默认（load_config 无参 ⇒ 纯默认）也必须同步。"""
    from anima.core.config import load_config
    cfg = load_config()
    assert cfg.debounce.group_wait_sec == pytest.approx(1.0)


# ── 2. 自适应档位随窗口一起缩放 ──────────────────────────────────────

def test_adaptive_short_message_delay():
    d = GroupDebouncer()  # wait_sec=1.0, short_msg_threshold=10
    # 短句（≤10 字）：min(1.0*1.5, 3.0) = 1.5
    assert d._compute_delay(_pending("哈哈哈")) == pytest.approx(1.5)


def test_adaptive_long_message_delay():
    d = GroupDebouncer()
    # 长句（>50 字）：max(1.0*0.5, 1.0) = 1.0
    assert d._compute_delay(_pending("字" * 60)) == pytest.approx(1.0)


def test_normal_message_uses_base_window():
    d = GroupDebouncer()
    # 中间长度（11–50 字）：基础窗口 1.0
    assert d._compute_delay(_pending("这是一个二十来个字的普通句子测试长度")) == pytest.approx(1.0)


def test_explicit_wait_sec_still_respected():
    """显式传参的既有用法不受默认值影响。"""
    d = GroupDebouncer(wait_sec=0.3, max_wait_sec=1.0)
    assert d.wait_sec == pytest.approx(0.3)
    assert d._compute_delay(_pending("这是一个二十来个字的普通句子测试长度")) == pytest.approx(0.3)


# ── 3. 合并必须留痕（此前完全不可观测）───────────────────────────────

@pytest.mark.asyncio
async def test_merge_is_logged(caplog):
    d = GroupDebouncer(wait_sec=0.05, max_wait_sec=0.3)
    with caplog.at_level(logging.INFO, logger="anima.core.conversation_assembler"):
        first = asyncio.create_task(
            d.ingest({"group_id": 123, "user_id": 456, "text": "第一句"})
        )
        await asyncio.sleep(0.01)
        second = await d.ingest({"group_id": 123, "user_id": 456, "text": "第二句"})
        merged = await first

    assert second is None, "第二句应被合并"
    assert merged["text"] == "第一句\n第二句"
    assert any("防抖" in r.message and "2" in r.message for r in caplog.records), \
        f"合并应记日志，实际：{[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_single_message_no_merge_log(caplog):
    """单条消息（未合并）不应刷「合并」日志。"""
    d = GroupDebouncer(wait_sec=0.02, max_wait_sec=0.1)
    with caplog.at_level(logging.INFO, logger="anima.core.conversation_assembler"):
        out = await d.ingest({"group_id": 1, "user_id": 2, "text": "只有一条"})
    assert out is not None
    assert not any("防抖" in r.message for r in caplog.records), \
        f"单条不应记合并日志：{[r.message for r in caplog.records]}"
