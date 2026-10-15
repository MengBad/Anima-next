"""X7 回归：bot UID 权威集扩充（群主 + 3852906976）。

2026-10-15 owner 坐实并授权：
  - `838916454`（群 727631526 的 **owner**，昵称「丰川刻子」）——
    owner 明确答复「**账号本身就是 bot**」。此前我据昵称启发式把它判为真人、
    建议排除，**被 owner 纠正**。教训：昵称永远不能反推身份，UID 必须 owner 坐实。
  - `3852906976`（昵称 `'1'`、群名片「网友莉顺卡」）—— owner 确认是 bot 账号。
  - 加上已有的 `486159181`（Aki），权威集共 **3** 个。

**不填**（owner 未点名，且名字像 bot 也不擅自作主）：
  - `3695244487`（管理员「Sasaki Yuto」，昵称误命中 hint「aki」）
  - `2854202507` / `3889024489`（可怜Bot / 可怜BOT）

生效后的行为变化：这三个 UID 未 @ Anima 的消息走 `bot_ignore` 被跳过
（防互@循环），同时 bot 关系档案开始记录。被 @ 时仍正常回复。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {486159181, 838916454, 3852906976}
NOT_BOT = {3695244487, 2854202507, 3889024489}


def _cfg_text():
    return (ROOT / "config" / "default.yaml").read_text(encoding="utf-8")


def test_default_yaml_has_three_uids():
    """config/default.yaml 的 known_bot_ids 必须含 3 个 UID。"""
    text = _cfg_text()
    m = re.search(r"known_bot_ids:\s*\[([^\]]*)\]", text)
    assert m, "default.yaml 缺 known_bot_ids"
    got = {int(x) for x in re.findall(r"\d+", m.group(1))}
    assert EXPECTED <= got, "缺 UID：%s（当前 %s）" % (sorted(EXPECTED - got), sorted(got))


def test_default_yaml_excludes_non_bots():
    """未被 owner 指名的 UID 不得混入（宁缺勿滥）。"""
    text = _cfg_text()
    m = re.search(r"known_bot_ids:\s*\[([^\]]*)\]", text)
    got = {int(x) for x in re.findall(r"\d+", m.group(1))}
    bad = NOT_BOT & got
    assert not bad, "未经 owner 指名的 UID 混入了权威集：%s" % sorted(bad)


def test_is_bot_uid_for_all_three():
    """三个 UID 都必须被 is_bot_uid 判为 bot。"""
    from anima.cognitive.bot_social import is_bot_uid

    for uid in EXPECTED:
        assert is_bot_uid(uid, EXPECTED), "UID %d 未被判为 bot" % uid


def test_real_humans_not_flagged():
    """管理员/疑似候选不得被误判（红线：误判 = 永久忽略真人）。"""
    from anima.cognitive.bot_social import is_bot_uid

    for uid in NOT_BOT:
        assert not is_bot_uid(uid, EXPECTED), "UID %d 被误判为 bot" % uid


def test_is_bot_sender_event_shape():
    """走真实事件结构：sender.user_id 命中 ⇒ True；未命中 ⇒ False。"""
    from anima.cognitive.bot_social import is_bot_sender

    for uid in EXPECTED:
        ev = {"user_id": uid, "sender": {"user_id": uid, "role": "member"}}
        assert is_bot_sender(ev, EXPECTED), "UID %d 的事件未被识别" % uid

    for uid in NOT_BOT:
        ev = {"user_id": uid, "sender": {"user_id": uid, "role": "member"}}
        assert not is_bot_sender(ev, EXPECTED), "UID %d 被误判" % uid


def test_bot_marked_event_still_detected():
    """平台若显式标 role=bot，即使不在权威集也应识别（不回归原行为）。"""
    from anima.cognitive.bot_social import is_bot_sender

    ev = {"user_id": 999999999, "sender": {"user_id": 999999999, "role": "bot"}}
    assert is_bot_sender(ev, EXPECTED) is True


def test_known_bots_json_shape(tmp_path):
    """known_bots.json 的 schema：UID 为键 + confirmed 字段，三例齐全。"""
    from anima.cognitive.bot_social import KnownBotsStore

    p = tmp_path / "known_bots.json"
    p.write_text(json.dumps({"bots": {
        "486159181": {"bot_name": "Aki", "confirmed": True},
        "838916454": {"bot_name": "丰川刻子", "confirmed": True},
        "3852906976": {"bot_name": "网友莉顺卡", "confirmed": True},
    }}, ensure_ascii=False), encoding="utf-8")

    store = KnownBotsStore(str(tmp_path))
    assert store.uids() == EXPECTED, "store.uids() = %s" % store.uids()


def test_unconfirmed_entry_not_authoritative(tmp_path):
    """confirmed=false 的条目不得进入权威集（防误填）。"""
    from anima.cognitive.bot_social import KnownBotsStore

    p = tmp_path / "known_bots.json"
    p.write_text(json.dumps({"bots": {
        "486159181": {"bot_name": "Aki", "confirmed": True},
        "123456789": {"bot_name": "未确认", "confirmed": False},
    }}, ensure_ascii=False), encoding="utf-8")

    store = KnownBotsStore(str(tmp_path))
    assert store.uids() == {486159181}, "未确认条目泄漏进权威集：%s" % store.uids()


def test_production_config_has_three():
    """生产 config.yaml 必须含 3 个 UID（部署后生效的判据）。

    仅在能读到生产文件时校验（本地无 /opt/anima 时跳过）。
    """
    prod = Path("/opt/anima/config.yaml")
    if not prod.exists():
        pytest.skip("本地无生产 config.yaml（由 accept_x7.py 在生产上验）")
    text = prod.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"known_bot_ids:\s*\[([^\]]*)\]", text)
    assert m, "生产 config.yaml 缺 known_bot_ids"
    got = {int(x) for x in re.findall(r"\d+", m.group(1))}
    assert EXPECTED <= got, "生产缺 UID：%s" % sorted(EXPECTED - got)
