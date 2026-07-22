"""认知模块持久化测试。"""

import pytest

from anima.cognitive.relation import get_relation, Stage
from anima.cognitive.desire import get_desire_system
from anima.cognitive.time_sense import get_time_sense


# 每个测试用独立 tmp_path，避免单例污染
def _reset_singletons():
    import anima.cognitive.relation as rel_mod
    import anima.cognitive.desire as des_mod
    import anima.cognitive.time_sense as ts_mod
    rel_mod._relation_engine = None
    des_mod._desire_system = None
    ts_mod._time_sense = None


@pytest.fixture(autouse=True)
def reset_singletons():
    _reset_singletons()
    yield
    _reset_singletons()


def test_relation_persist_roundtrip(tmp_path):
    data_dir = str(tmp_path)
    engine = get_relation(data_dir)
    engine.touch(111, "你好")
    engine.record_event(111, "被夸", delta_trust=0.1, delta_valence=0.2)

    engine.persist()

    # 重新初始化，模拟重启
    _reset_singletons()
    engine2 = get_relation(data_dir)
    rel = engine2.get(111)
    assert rel.interaction_count == 1
    assert rel.trust > 0.5
    assert rel.valence > 0
    assert rel.stage == Stage.STRANGER


def test_desire_persist_roundtrip(tmp_path):
    data_dir = str(tmp_path)
    ds = get_desire_system(data_dir)
    ds.add("想聊动漫", intensity=0.8)
    ds.persist()

    _reset_singletons()
    ds2 = get_desire_system(data_dir)
    active = ds2.get_active(0.5)
    assert len(active) == 1
    assert active[0].content == "想聊动漫"


def test_time_sense_persist_roundtrip(tmp_path):
    data_dir = str(tmp_path)
    ts = get_time_sense(data_dir)
    ts.touch(222)
    ts.persist()

    _reset_singletons()
    ts2 = get_time_sense(data_dir)
    assert 222 in ts2.last_interaction
    assert ts2.interaction_count[222] == 1
