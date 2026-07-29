"""B1 秘密存储：dataclass / store 落盘 / 按影响类型 / 按目标 / 泄露标记。"""
from __future__ import annotations

from pathlib import Path

from anima.memory.secrets import Secret, SecretsStore


def test_secret_dataclass_defaults():
    s = Secret(id="sec_1", content="x", created_at=1.0, source_event="e", influence="jealousy")
    assert s.exposed is False
    assert s.intensity == 0.5
    assert s.target_uid is None
    d = s.to_dict()
    assert d["id"] == "sec_1"


def test_store_add_and_persist(tmp_path):
    store = SecretsStore(str(tmp_path))
    assert store.count() == 0
    store.add_secret(
        content="其实有点嫉妒亦萱，她总是被夸",
        source_event="亦萱被群友夸了10次，本喵只被夸了2次",
        influence="jealousy",
        target_uid="123456",
        intensity=0.6,
    )
    assert store.count() == 1
    # 落盘
    assert (tmp_path / "secrets.json").exists()
    # 新实例从磁盘加载
    store2 = SecretsStore(str(tmp_path))
    assert store2.count() == 1
    sec = store2.all()[0]
    assert sec.influence == "jealousy"
    assert sec.target_uid == "123456"
    assert abs(sec.intensity - 0.6) < 1e-9


def test_filter_by_influence_and_target(tmp_path):
    store = SecretsStore(str(tmp_path))
    store.add_secret("嫉妒亦萱", "被夸", "jealousy", target_uid="123456", intensity=0.6)
    store.add_secret("记仇刻子", "改头衔", "resentment", target_uid="838916454", intensity=0.8)
    store.add_secret("喜欢和某人聊", "熟悉", "attachment", target_uid="123456", intensity=0.5)

    assert len(store.by_influence("jealousy")) == 1
    assert len(store.for_target("123456")) == 2
    assert len(store.for_target("838916454")) == 1


def test_active_secrets_excludes_exposed(tmp_path):
    store = SecretsStore(str(tmp_path))
    a = store.add_secret("secret A", "e", "fear", target_uid="999", intensity=0.7)
    store.add_secret("secret B", "e", "gratitude", target_uid="888", intensity=0.3)
    assert len(store.active_secrets()) == 2
    # 标记 A 泄露
    assert store.mark_exposed(a.id, True) is True
    active = store.active_secrets()
    assert len(active) == 1
    assert active[0].id != a.id
    # 已泄露再标记同值 → 无变化
    assert store.mark_exposed(a.id, True) is False


def test_mark_exposed_persists(tmp_path):
    store = SecretsStore(str(tmp_path))
    a = store.add_secret("secret A", "e", "fear", target_uid="999", intensity=0.7)
    store.mark_exposed(a.id, True)
    store2 = SecretsStore(str(tmp_path))
    assert store2.get(a.id).exposed is True


def test_invalid_influence_falls_back(tmp_path):
    store = SecretsStore(str(tmp_path))
    s = store.add_secret("x", "e", "not_a_real_type", target_uid="1", intensity=0.5)
    assert s.influence == "gratitude"  # 回退到合法值
