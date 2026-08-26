# -*- coding: utf-8 -*-
"""P2-D：头衔变更写入从信道 on_event 迁入认知层 event.notice 消费者（social_reactions）。
信道不再直写 learning/bot_social；竞态与归属不变。"""
import asyncio
from types import SimpleNamespace

import anima.cognitive.social_reactions as sr


def test_title_note_builder():
    assert sr._build_title_change_note(
        {"notice_type": "group_title_update", "user_id": 5, "title": "喵"}, 5) == "头衔被有人改为【喵】"
    assert sr._build_title_change_note(
        {"notice_type": "group_title_update", "user_id": 6, "title": "喵"}, 5) is None, "非目标不应记笔记"


def test_handle_notice_writes_self_notes_and_rivalry(tmp_path, monkeypatch):
    learned, rivalry = [], []
    def _fake_get(dd):
        return SimpleNamespace(add_note=lambda note, delta=0, category="": learned.append((note, delta, category)))
    monkeypatch.setattr("anima.cognitive.learning.get_learning", _fake_get)
    monkeypatch.setattr("anima.cognitive.bot_social.is_bot_uid", lambda op, known: op in known)
    monkeypatch.setattr(
        "anima.cognitive.bot_social.BotRelationStore",
        lambda dd: SimpleNamespace(record_interaction=lambda *a, **k: rivalry.append(a)))
    core = SimpleNamespace(
        config=SimpleNamespace(persistence=SimpleNamespace(data_dir=str(tmp_path)), social=None),
        gateway=SimpleNamespace(channel=SimpleNamespace(_known_bot_ids=lambda: {888})),
    )
    obj = sr.SocialReactions(core)
    ev = {"post_type": "notice", "notice_type": "group_title_update", "self_id": 100,
          "user_id": 100, "target_id": 100, "operator_id": 888, "title": "喵", "group_id": 9}
    asyncio.run(obj.handle_notice(ev))
    assert learned and learned[0][2] == "self_correction", "应写入 self_correction 笔记"
    assert rivalry and rivalry[0][2] == "rivalry", "其他 bot 改头衔应记竞争事件"


def test_onebot_no_longer_owns_title_note():
    import anima.channels.onebot11 as ob
    assert not hasattr(ob, "build_title_change_note"), "信道不应再拥有头衔笔记能力"
