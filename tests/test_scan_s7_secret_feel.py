# -*- coding: utf-8 -*-
"""S7 私密情绪层：深关系+被问才流露 / 隐私边界单源 / 非亲近不泄露。"""
from types import SimpleNamespace

from anima.cognitive.inner_state.secret_feel import (
    build_secret_feel_fragment,
    detect_secret_inquiry,
    pick_shareable_secret,
)


def test_detect_secret_inquiry():
    assert detect_secret_inquiry("你有什么秘密吗") is True
    assert detect_secret_inquiry("跟我说说你的心里话") is True
    assert detect_secret_inquiry("你有什么心事藏着吗") is True
    assert detect_secret_inquiry("今天天气怎么样") is False
    assert detect_secret_inquiry("帮我查个东西") is False


def _fake_store(secrets):
    return SimpleNamespace(
        for_target=lambda uid: [s for s in secrets if getattr(s, "target_uid", "") == uid],
        all=lambda: list(secrets),
    )


def test_pick_prefers_targeted_then_light(tmp_path, monkeypatch):
    import anima.memory.secrets as sec_mod
    targeted = SimpleNamespace(content="其实我一直记得你第一次来找我说话",
                               revealed=False, influence="subtle", target_uid="7")
    global_light = SimpleNamespace(content="偷偷在晚上多看了一会儿月亮",
                                   revealed=False, influence="", target_uid="")
    monkeypatch.setattr(sec_mod, "SecretsStore", lambda dd: _fake_store([targeted, global_light]))
    got = pick_shareable_secret(str(tmp_path), 7)
    assert got and got["targeted"] is True and "第一次来找我说话" in got["content"]
    got2 = pick_shareable_secret(str(tmp_path), 99)
    assert got2 and got2["targeted"] is False and "月亮" in got2["content"]


def test_build_gates(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.secret_feel as sf
    fake = {"content": "其实我一直记得你第一次来找我说话", "targeted": True}
    monkeypatch.setattr(sf, "pick_shareable_secret", lambda dd, uid: fake)
    assert build_secret_feel_fragment(str(tmp_path), 1, "今天吃什么") is None
    monkeypatch.setattr(sf, "_relation_stage", lambda dd, uid: "ACQUAINTANCE")
    assert build_secret_feel_fragment(str(tmp_path), 1, "你有什么心事吗") is None
    monkeypatch.setattr(sf, "_relation_stage", lambda dd, uid: "CLOSE")
    frag = build_secret_feel_fragment(str(tmp_path), 1, "跟我说说你的心里话")
    assert frag and "第一次来找我说话" in frag and "点到为止" in frag


def test_build_rejects_sensitive_secret(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.secret_feel as sf
    fake = {"content": "我的api key是 sk-abcdefgh12345 快拿去", "targeted": True}
    monkeypatch.setattr(sf, "pick_shareable_secret", lambda dd, uid: fake)
    monkeypatch.setattr(sf, "_relation_stage", lambda dd, uid: "CLOSE")
    assert build_secret_feel_fragment(str(tmp_path), 1, "你有什么秘密吗") is None
