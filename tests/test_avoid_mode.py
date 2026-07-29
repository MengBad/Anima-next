"""伤痕回避联动单元测试（任务5）：scar_count>=2 → 24h 回避 → autonomy 不 care。"""
import time
import tempfile
import types

import anima.core.autonomy as autonomy
import anima.memory.chat_memory as cm_mod
import anima.cognitive.scar as scar_mod

from anima.cognitive.scar import get_scar_system
from anima.core.autonomy import is_user_avoided


def _tmp_data_dir():
    return tempfile.mkdtemp()


def test_scar_avoid_after_two():
    dd = _tmp_data_dir()
    scar = get_scar_system(dd)
    scar.add(123, "对方骂了难听的话", severity=0.4)
    assert not scar.is_avoiding(123)
    scar.add(123, "对方又骂了", severity=0.4)
    assert scar.is_avoiding(123)
    assert scar.avoid_until_for(123) > time.time()


def test_avoid_expires_after_24h():
    dd = _tmp_data_dir()
    scar = get_scar_system(dd)
    scar.add(123, "a", severity=0.4)
    scar.add(123, "b", severity=0.4)
    assert scar.is_avoiding(123, now=time.time() + 86400 + 1) is False


def test_autonomy_helper_reflects_scar():
    dd = _tmp_data_dir()
    scar = get_scar_system(dd)
    scar.add(123, "a", severity=0.4)
    scar.add(123, "b", severity=0.4)
    assert is_user_avoided(dd, 123) is True
    assert is_user_avoided(dd, 999) is False


def test_group_only_avoided_speakers():
    """群内近期发言者全部处于回避期时，autonomy 不产生 care 意图。"""
    class FakeCM:
        _store = {"group_1_123": None, "group_1_456": None}

        def get_context(self, chat_type, chat_id, user_id, recent_rounds=10):
            if int(user_id) == 123:
                return [{"role": "user", "content": "傻逼"}]
            return []

    fake_cm = FakeCM()
    fake_scar = types.SimpleNamespace(is_avoiding=lambda uid, now: int(uid) == 123)
    orig_cm = cm_mod.get_chat_memory
    orig_scar = scar_mod.get_scar_system
    cm_mod.get_chat_memory = lambda: fake_cm
    scar_mod.get_scar_system = lambda dd: fake_scar

    class FakeCore:
        config = types.SimpleNamespace(persistence=types.SimpleNamespace(data_dir="data"))

    hb = types.SimpleNamespace(core=FakeCore())
    try:
        result = autonomy.Heartbeat._group_only_avoided_speakers(hb, 1, time.time())
        assert result is True
    finally:
        cm_mod.get_chat_memory = orig_cm
        scar_mod.get_scar_system = orig_scar
