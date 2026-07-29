"""B2 每日自动生成秘密测试。

覆盖 detect_daily_secrets / maybe_generate_daily_secrets：
- jealousy：某用户被夸次数 >> 本喵被夸次数
- resentment：bot_relations rivalry >= 0.3
- attachment：relation 熟悉度 >= 0.7（mock relation 模块）
- fear：scar 严重度 > 0.6（mock scar 模块）
- 去重落盘幂等（同 influence+target 不重复生成）
"""
import json
from pathlib import Path

import pytest

from anima.cognitive.secret_auto import (
    detect_daily_secrets,
    maybe_generate_daily_secrets,
)
from anima.memory.secrets import SecretsStore


def _write_chat_memory(tmp_path: Path, praise_other: int, self_praised: int = 2):
    """写 chat_memory.json：亦萱被夸 praise_other 次（uid=999），本喵被夸 self_praised 次。"""
    rounds_other = [{"role": "user", "name": "亦萱", "content": "我是亦萱"}]
    for _ in range(praise_other):
        rounds_other.append({"role": "user", "name": "a", "content": "亦萱好厉害啊"})
    rounds_self = []
    for _ in range(self_praised):
        rounds_self.append({"role": "user", "name": "b", "content": "哈基米好可爱"})
    cm = {
        "group_111_999": {"round_1": rounds_other},
        "group_111_2": {"round_1": rounds_self},
    }
    (tmp_path / "chat_memory.json").write_text(
        json.dumps(cm, ensure_ascii=False), encoding="utf-8"
    )


def test_detect_jealousy(tmp_path):
    _write_chat_memory(tmp_path, praise_other=10, self_praised=2)
    secrets = detect_daily_secrets(str(tmp_path))
    jeal = [s for s in secrets if s.influence == "jealousy"]
    assert len(jeal) == 1
    assert jeal[0].target_uid == "999"
    assert jeal[0].intensity > 0


def test_no_jealousy_when_self_praised_more(tmp_path):
    # 本喵被夸更多 → 不嫉妒
    _write_chat_memory(tmp_path, praise_other=3, self_praised=20)
    secrets = detect_daily_secrets(str(tmp_path))
    assert [s for s in secrets if s.influence == "jealousy"] == []


def test_detect_resentment(tmp_path):
    (tmp_path / "bot_relations.json").write_text(
        json.dumps({"relations": {"555": {"bot_name": "小机器人", "rivalry": 0.5}}}),
        encoding="utf-8",
    )
    secrets = detect_daily_secrets(str(tmp_path))
    res = [s for s in secrets if s.influence == "resentment"]
    assert len(res) == 1
    assert res[0].target_uid == "555"


def test_detect_attachment(tmp_path, monkeypatch):
    class _Info:
        familiarity = 0.9
        name = "小美"

    class _RelSys:
        def all(self):
            return {"777": _Info()}

    monkeypatch.setattr("anima.cognitive.relation.get_relation", lambda: _RelSys())
    secrets = detect_daily_secrets(str(tmp_path))
    att = [s for s in secrets if s.influence == "attachment"]
    assert len(att) == 1
    assert att[0].target_uid == "777"


def test_detect_fear(tmp_path, monkeypatch):
    class _Scar:
        severity = 0.8
        target_uid = "888"

    class _ScarSys:
        def all(self):
            return [_Scar()]

    monkeypatch.setattr("anima.cognitive.scar.get_scar_system", lambda dd: _ScarSys())
    secrets = detect_daily_secrets(str(tmp_path))
    fear = [s for s in secrets if s.influence == "fear"]
    assert len(fear) == 1
    assert fear[0].target_uid == "888"


def test_maybe_generate_daily_secrets_idempotent(tmp_path):
    _write_chat_memory(tmp_path, praise_other=10, self_praised=2)
    added1 = maybe_generate_daily_secrets(str(tmp_path))
    assert len(added1) == 1
    # 第二次运行：相同 (influence, target) 应去重，不再新增
    added2 = maybe_generate_daily_secrets(str(tmp_path))
    assert added2 == []
    store = SecretsStore(str(tmp_path))
    assert len(store.all()) == 1
