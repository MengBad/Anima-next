"""P3 回归：自身群状态每小时刷新，头衔/名片变更写 self_notes。

- compute_self_status_notes：纯函数比较前后状态，生成笔记文本；
- _refresh_self_status：真实方法遍历已知群，调 channel.get_group_member_info，
  变更时写 self_notes；首跑只种基线、不刷屏。
"""
import time

import pytest

from anima.core.autonomy import compute_self_status_notes, Heartbeat


def test_compute_self_status_notes_change():
    notes = compute_self_status_notes("旧头衔", "新头衔", "", "名片", 727631526)
    assert "群 727631526 当前头衔：【新头衔】" in notes
    assert "群 727631526 当前群名片：【名片】" in notes


def test_compute_self_status_notes_no_change():
    assert compute_self_status_notes("同", "同", "卡", "卡", 1) == []


def test_compute_self_status_notes_clear():
    notes = compute_self_status_notes("旧", "", "旧卡", "", 5)
    assert "群 5 头衔被清空了" in notes
    assert "群 5 群名片被清空了" in notes


def _make_gmi(info):
    async def _gmi(gid, uid):
        return dict(info)
    return _gmi


def _fake_heartbeat(member_info, tmp_path):
    hb = Heartbeat.__new__(Heartbeat)
    hb._group_last_msg = {727631526: time.time()}
    hb._self_status_cache = {}
    hb.core = type(
        "C",
        (),
        {
            "config": type(
                "CF", (), {"persistence": type("P", (), {"data_dir": str(tmp_path)})()}
            )(),
            "gateway": type(
                "GW",
                (),
                {
                    "channel": type(
                        "CH",
                        (),
                        {
                            "self_id": 123,
                            "get_group_member_info": staticmethod(_make_gmi(dict(member_info))),
                        },
                    )()
                },
            )(),
        },
    )()
    return hb


@pytest.mark.asyncio
async def test_refresh_self_status_writes_note_on_change(monkeypatch, tmp_path):
    captured = []

    class _FakeLearning:
        def add_note(self, content, delta=0.05, category=None):
            captured.append(content)
            return True

    monkeypatch.setattr(
        "anima.cognitive.learning.get_learning", lambda *_a, **_k: _FakeLearning()
    )

    hb = _fake_heartbeat({"user_id": 123, "title": "新头衔", "card": "新名片"}, tmp_path)
    hb._self_status_cache = {727631526: {"title": "旧头衔", "card": ""}}

    await hb._refresh_self_status()
    assert any("当前头衔：【新头衔】" in c for c in captured)
    assert any("当前群名片：【新名片】" in c for c in captured)


@pytest.mark.asyncio
async def test_refresh_self_status_seeds_on_first_run(monkeypatch, tmp_path):
    captured = []

    class _FakeLearning:
        def add_note(self, content, delta=0.05, category=None):
            captured.append(content)
            return True

    monkeypatch.setattr(
        "anima.cognitive.learning.get_learning", lambda *_a, **_k: _FakeLearning()
    )

    hb = _fake_heartbeat({"user_id": 123, "title": "头衔", "card": "名片"}, tmp_path)
    hb._self_status_cache = {}  # 首跑

    await hb._refresh_self_status()
    # 首跑只种基线，不写笔记
    assert captured == []
    assert hb._self_status_cache[727631526]["title"] == "头衔"
