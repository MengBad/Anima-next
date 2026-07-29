"""B3 秘密影响行为：对嫉妒/依恋/记仇对象，隐式调整生成参数。"""
from __future__ import annotations

from anima.cognitive.secret_injector import SecretInjector
from anima.memory.secrets import Secret


def _secret(influence, target_uid, intensity=0.6):
    return Secret(
        id="s", content="x", created_at=1.0, source_event="e",
        influence=influence, target_uid=target_uid, intensity=intensity,
    )


def test_jealousy_lowers_temperature_and_caps_tokens():
    sec = _secret("jealousy", "123456")
    ctx = {"temperature": 0.8, "max_tokens": 512, "system_bias": ""}
    out = SecretInjector.apply(ctx, [sec], "123456")
    assert out["temperature"] < 0.8          # 降温（冷淡）
    assert out["max_tokens"] <= 100          # 限长
    assert "疏远" in out["system_bias"]


def test_attachment_raises_temperature_and_sticker():
    sec = _secret("attachment", "777")
    sb = {}
    ctx = {"temperature": 0.8, "max_tokens": 512, "system_bias": ""}
    out = SecretInjector.apply(ctx, [sec], "777", sticker_boost_out=sb)
    assert out["temperature"] > 0.8          # 升温（热情）
    assert sb["sticker_boost"] >= 0.6


def test_resentment_short_and_cold():
    sec = _secret("resentment", "838916454")
    ctx = {"temperature": 0.8, "max_tokens": 512, "system_bias": ""}
    out = SecretInjector.apply(ctx, [sec], "838916454")
    assert out["temperature"] < 0.8
    assert out["max_tokens"] <= 25
    assert "简短" in out["system_bias"]


def test_other_user_secrets_do_not_affect():
    sec = _secret("jealousy", "999")   # 针对别人，不是当前用户
    ctx = {"temperature": 0.8, "max_tokens": 512, "system_bias": ""}
    out = SecretInjector.apply(ctx, [sec], "123456")
    assert out["temperature"] == 0.8
    assert out["max_tokens"] == 512
    assert out["system_bias"] == ""


def test_temperature_clamped():
    sec = _secret("resentment", "1")  # -0.2 多次叠加不应低于下限
    ctx = {"temperature": 0.31, "max_tokens": 512, "system_bias": ""}
    out = SecretInjector.apply(ctx, [sec], "1")
    assert out["temperature"] >= 0.3
