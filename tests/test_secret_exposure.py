"""B5 意外泄露测试。

覆盖 SecretInjector.maybe_expose：
- tired / excited 且低概率（<1%）→ 返回一条未泄露的秘密
- 概率未中 / 情绪非 tired-excited（如 calm）/ 已全部泄露 → 返回 None
并验证 mark_exposed 后该秘密不再出现在 active_secrets（避免重复泄露）。
"""
from pathlib import Path

from anima.cognitive.secret_injector import SecretInjector, EXPOSURE_PROB
from anima.memory.secrets import Secret, SecretsStore


class _FakeRng:
    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value

    def choice(self, seq):
        # 单元素场景：确定性返回首个；多元素亦返回首个（测试仅断言非空）
        return seq[0]


def _make_secret(secret_id="s1", influence="jealousy", exposed=False):
    return Secret(
        id=secret_id,
        content="测试秘密内容",
        created_at=1.0,
        source_event="",
        influence=influence,
        target_uid="1",
        intensity=0.5,
        exposed=exposed,
    )


def test_expose_when_tired_and_low_roll():
    s = SecretInjector.maybe_expose([_make_secret()], "tired", rng=_FakeRng(0.005))
    assert s is not None
    assert s.id == "s1"


def test_no_expose_when_prob_miss():
    s = SecretInjector.maybe_expose([_make_secret()], "tired", rng=_FakeRng(0.5))
    assert s is None


def test_no_expose_when_calm():
    # calm 不在 (tired, excited) 之内，即使极低概率也不泄露
    s = SecretInjector.maybe_expose([_make_secret()], "calm", rng=_FakeRng(0.001))
    assert s is None


def test_expose_when_excited():
    s = SecretInjector.maybe_expose([_make_secret()], "excited", rng=_FakeRng(0.005))
    assert s is not None


def test_no_expose_when_all_exposed():
    sec = _make_secret(exposed=True)
    s = SecretInjector.maybe_expose([sec], "tired", rng=_FakeRng(0.001))
    assert s is None


def test_mark_exposed_removes_from_active(tmp_path):
    store = SecretsStore(str(tmp_path))
    store.add(_make_secret(secret_id="x1"))
    assert len(store.active_secrets()) == 1
    assert store.mark_exposed("x1", True) is True
    assert len(store.active_secrets()) == 0
    assert store.get("x1").exposed is True


def test_exposure_prob_constant_is_low():
    # 显式确认意外泄露概率确实是 1%，符合「极低概率」
    assert EXPOSURE_PROB == 0.01
