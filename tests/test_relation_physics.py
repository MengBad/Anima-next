"""关系物理深度测试：bond_tension / relational_depth 的演化与持久化。"""
from anima.cognitive.relation import RelationEngine, get_relation


def test_negative_raises_tension(tmp_path):
    eng = RelationEngine(str(tmp_path))
    # 关系张力是 EMA 平滑后的累积量：单条辱骂 valence 仅到 ~-0.15（未破 -0.2 闸门），
    # 需持续负面互动才推过阈值拉起张力——这正是「不轻易记仇、但反复冒犯会积怨」的设计。
    for _ in range(3):
        eng.touch(1001, "你个傻逼滚远点")  # 持续强负面
    rel = eng.get(1001)
    assert rel.bond_tension > 0.0
    assert rel.valence < 0


def test_positive_grows_depth(tmp_path):
    eng = RelationEngine(str(tmp_path))
    # 多次温和正向互动 → 深度累积
    for _ in range(15):
        eng.touch(1002, "谢谢啦你真厉害哈哈")
    rel = eng.get(1002)
    assert rel.relational_depth > 0.05
    assert rel.bond_tension == 0.0  # 无负面 → 张力为 0


def test_physics_persist_roundtrip(tmp_path):
    eng = RelationEngine(str(tmp_path))
    for _ in range(3):
        eng.touch(1003, "去死吧废物")  # 持续拉高张力
    eng.persist()
    # 重新加载，值应保留
    eng2 = RelationEngine(str(tmp_path))
    eng2.restore()
    rel = eng2.get(1003)
    assert rel.bond_tension > 0.0
    phys = eng2.get_physics(1003)
    assert "bond_tension" in phys and "relational_depth" in phys
    assert 0.0 <= phys["bond_tension"] <= 1.0


def test_get_physics_returns_scalars(tmp_path):
    eng = RelationEngine(str(tmp_path))
    eng.touch(1004, "今天聊得好开心")
    phys = eng.get_physics(1004)
    assert set(["user_id", "stage", "familiarity", "trust", "valence",
                "bond_tension", "relational_depth", "interaction_count"]) <= set(phys.keys())


def test_singleton_isolation(tmp_path):
    # 不同 data_dir 不应串数据
    a = get_relation(str(tmp_path / "a"))
    b = get_relation(str(tmp_path / "b"))
    a.touch(1, "你好呀")
    assert b.get(1).interaction_count == 0
