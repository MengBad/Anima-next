"""B 站兴趣触发源契约（2026-10-24）。

## 这块是什么

* `bilibili_discover`：**能力**（搜 + 元数据清单）
* `bilibili_read`：**深读**（有字幕读字幕，无字幕用简介兜底）
* **本模块：动机**（什么时候去刷、刷完怎么分享）

## owner 让「你来判断」时的三个决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 分享到哪 | **私聊** | 群里刷屏招人烦；自主产出默认落私聊（与报备一致） |
| 多久一次 | **每天最多 1 次**（22h 间隔） | B 站不是刚需，多了就是打扰 |
| 关键词 | 从**它自己的欲望**里取 | 让它刷自己在意的东西，而非固定词表 |

## 🔴 诚实约束

候选只有元数据（标题/UP/播放/时长），**没看内容**。
分享文案必须写明「只看了标题和简介」 —— **绝不能写成「我看了这个视频」**。

## 本测试锁死

1. 时段闸门（仅 8–23 点）；
2. 22h 间隔（不是 24h，避免每日漂移导致隔天跳过）；
3. 状态独立存储（不污染 auto_trigger.json）；
4. **诚实文案**；
5. 每日最多 1 次；
6. 默认关闭。
"""
import time

import pytest

from anima.agent.bilibili_interest import (
    ALLOWED_HOURS, MIN_INTERVAL_SEC, STATE_FILE, build_share_text,
    load_state, save_state, should_run,
)
from anima.core.persistence import PersistenceProvider


def _p(tmp_path):
    return PersistenceProvider(str(tmp_path))


# ── 1. 时段闸门 ───────────────────────────────────────────────────────


def test_blocked_outside_allowed_hours(tmp_path):
    """凌晨不打扰。"""
    ok, why = should_run(_p(tmp_path), now=time.time(), hour=3)
    assert ok is False
    assert "时段" in why


def test_allowed_inside_window(tmp_path):
    ok, why = should_run(_p(tmp_path), now=time.time(), hour=10)
    assert ok is True, why


def test_allowed_hours_sane():
    assert ALLOWED_HOURS[0] >= 6, "不该在太早的时段打扰"
    assert ALLOWED_HOURS[1] <= 23


# ── 2. 22 小时间隔 ────────────────────────────────────────────────────


def test_blocked_within_interval(tmp_path):
    p = _p(tmp_path)
    save_state(p, {"last_run_at": time.time() - 3600})   # 1 小时前刷过
    ok, why = should_run(p, now=time.time(), hour=10)
    assert ok is False
    assert "不足" in why


def test_allowed_after_interval(tmp_path):
    p = _p(tmp_path)
    save_state(p, {"last_run_at": time.time() - MIN_INTERVAL_SEC - 60})
    ok, why = should_run(p, now=time.time(), hour=10)
    assert ok is True, why


def test_interval_is_22h_not_24h():
    """22h 而非 24h：避免「昨天 11:43、今天开机晚」导致隔天永久跳过。"""
    assert 20 * 3600 <= MIN_INTERVAL_SEC <= 23 * 3600


# ── 3. 状态独立存储 ───────────────────────────────────────────────────


def test_state_file_is_separate(tmp_path):
    p = _p(tmp_path)
    save_state(p, {"last_run_at": 123})
    assert (tmp_path / STATE_FILE).exists()
    assert STATE_FILE != "auto_trigger.json", "不得污染 auto_trigger.json"


def test_state_roundtrip(tmp_path):
    p = _p(tmp_path)
    save_state(p, {"last_run_at": 456.0, "last_topic": "x"})
    st = load_state(p)
    assert st["last_run_at"] == 456.0
    assert st["last_topic"] == "x"


def test_corrupt_state_fails_open_to_can_run(tmp_path):
    """状态坏了 ⇒ 按「没刷过」处理（宁可多刷一次，不要永远不刷）。"""
    (tmp_path / STATE_FILE).write_text("{ broken", encoding="utf-8")
    ok, _ = should_run(_p(tmp_path), now=time.time(), hour=10)
    assert ok is True


# ── 4. 🔴 诚实文案 ────────────────────────────────────────────────────


def _row(**kw):
    base = {"bvid": "BV1xx", "title": "某标题", "up": "某UP",
            "play": 12345, "duration_sec": "8:23"}
    base.update(kw)
    return base


def test_share_text_is_honest():
    """🔴 必须明说「只看了标题和简介」，不许假装看过内容。"""
    out = build_share_text([_row()], "猫娘")
    assert "只看了标题和简介" in out, f"必须诚实标注：{out}"
    assert "没看内容" in out


def test_share_text_has_link_and_meta():
    out = build_share_text([_row(bvid="BV1abc", up="某人", play=12345)], "x")
    assert "BV1abc" in out
    assert "https://www.bilibili.com/video/BV1abc" in out
    assert "某人" in out
    assert "1.2万" in out


def test_share_text_empty_rows():
    assert build_share_text([], "x") == ""


def test_share_text_never_claims_watching():
    """反向：文案里**不得**出现「我看了/看完了」这类虚假陈述。"""
    out = build_share_text([_row()], "x")
    for bad in ("我看了这个视频", "我看完了", "视频内容讲的是"):
        assert bad not in out, f"不得出现虚假陈述：{bad}"


# ── 5. 默认关闭 ───────────────────────────────────────────────────────


def test_default_off():
    from anima.core.config import AnimaConfig
    assert AnimaConfig().autonomy.bilibili_interest_enabled is False, (
        "自主打扰类能力必须默认关闭，需 owner 显式开启"
    )
