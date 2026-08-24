"""handler 接线集成：owner“别理那两人”→“真实持久化”→闸门命中。"""

from anima.llm.handler import LLMHandler
from anima.llm.ignore_list import IgnoreStore
from anima.llm.ignore_list import should_skip_ignored


def _mk(data_dir):
    h = LLMHandler.__new__(LLMHandler)

    class Per:
        owners = [1562290139]
    class P:
        pass
    P.data_dir = data_dir
    class Cfg:
        persistence = P()
        permissions = Per()
        autonomy = type("A", (), {})()
        crossgroup = type("CG", (), {"enabled": True, "max_facts_per_user": 50, "min_fact_text_len": 6})()
    class Core:
        config = Cfg()
    h.core = Core()
    h._recent_spk = {}
    return h


def test_track_and_directive_persist_two_people(tmp_path):
    dd = str(tmp_path)
    h = _mk(dd)
    h._ignore_store_obj = IgnoreStore(dd)
    h._track_group_speaker(727, 100)
    h._track_group_speaker(727, 200)
    h._maybe_apply_ignore_directive("以后别再理那两个人了", 727, 1562290139)
    s = IgnoreStore(dd)
    assert s.is_ignored("727", 100)
    assert s.is_ignored("727", 200)


def test_directive_single_recent_only(tmp_path):
    dd = str(tmp_path)
    h = _mk(dd)
    h._ignore_store_obj = IgnoreStore(dd)
    h._track_group_speaker(727, 300)
    h._track_group_speaker(727, 100)   # 更晚发言
    h._maybe_apply_ignore_directive("别理他", 727, 1562290139)  # 无复数词 → 忽略最近1人=100
    s = IgnoreStore(dd)
    assert s.is_ignored("727", 100)
    assert not s.is_ignored("727", 300)


def test_non_owner_or_non_matching_does_not_persist(tmp_path):
    dd = str(tmp_path)
    h = _mk(dd)
    h._ignore_store_obj = IgnoreStore(dd)
    # 普通闲聊文本
    h._track_group_speaker(727, 100)
    h._maybe_apply_ignore_directive("今晚吃什么", 727, 1562290139)
    assert not IgnoreStore(dd).is_ignored("727", 100)
    # owner 但无复数提示
    h._track_group_speaker(727, 200)
    h._maybe_apply_ignore_directive("别理", 727, 1562290139)  # 仅有触发词无目标提示 → 不解析
    assert not IgnoreStore(dd).is_ignored("727", 200)


def test_lazy_store_and_gate_skip(tmp_path):
    dd = str(tmp_path)
    h = _mk(dd)
    st = h._ignore_store()
    st.add("727", [555])
    # 闸门判定直接命中忽略名单
    assert should_skip_ignored(h._ignore_store(), "group", "727", 555)
    assert not should_skip_ignored(h._ignore_store(), "group", "727", 666)
    # 持久化跨实例
    assert IgnoreStore(dd).is_ignored("727", 555)
