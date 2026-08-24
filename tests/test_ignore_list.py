"""owner 忽略名单：持久化存储 + 「别理那两人」解析 + 闸门判定。"""

import json
from pathlib import Path

from anima.llm.ignore_list import (
    IgnoreStore,
    matches_ignore_directive,
    resolve_ignore_targets,
    should_skip_ignored,
)


### IgnoreStore ###

def test_ignore_store_add_list_is_ignored_remove(tmp_path):
    s = IgnoreStore(str(tmp_path))
    assert s.add("727631526", [111, 222]) == 2
    assert s.is_ignored("727631526", 111)
    assert s.is_ignored("727631526", "222")
    assert not s.is_ignored("727631526", 999)
    assert not s.is_ignored("888", 111)          # 异群隔离
    lst = s.list("727631526")
    assert sorted(lst) == ["111", "222"]
    # 幂等 add
    s.add("727631526", [111])
    assert len(s.list("727631526")) == 2
    assert s.remove("727631526", 111)
    assert not s.is_ignored("727631526", 111)
    assert not s.remove("727631526", 111)        # 已移除


def test_ignore_store_persists_reload(tmp_path):
    p = Path(tmp_path) / "ignore_list.json"
    assert not p.exists()
    s = IgnoreStore(str(tmp_path))
    s.add("global", [42])
    assert p.exists()
    s2 = IgnoreStore(str(tmp_path))              # 重新加载
    assert s2.is_ignored("global", 42)


def test_ignore_store_corrupt_file_is_tolerant(tmp_path):
    Path(tmp_path, "ignore_list.json").write_text("{not json", encoding="utf-8")
    s = IgnoreStore(str(tmp_path))
    assert not s.is_ignored("global", 1)


### 指令识别 ###

def test_matches_ignore_directive_positive():
    for t in ("以后别再理刚刚那两个人了", "别理他", "不要理这俩", "冷落那两个人"):
        assert matches_ignore_directive(t), t


def test_matches_ignore_directive_negative():
    for t in ("今晚吃什么", "别理", "在吗", "喵", ""):
        assert not matches_ignore_directive(t), t


### 目标解析 ###

def test_resolve_targets_two_people():
    speakers = [100, 200, 300]
    assert resolve_ignore_targets("别理刚刚那两个人了", speakers) == [100, 200]


def test_resolve_targets_single():
    speakers = [100, 200, 300]
    assert resolve_ignore_targets("别理他", speakers) == [100]


def test_resolve_targets_limited_by_available():
    assert resolve_ignore_targets("别理那两个人", []) == []
    assert resolve_ignore_targets("别理那两个人", [500]) == [500]


### 闸门判定 ###

def test_should_skip_ignored_group_only(tmp_path):
    s = IgnoreStore(str(tmp_path))
    s.add("727631526", [111])
    assert should_skip_ignored(s, "group", "727631526", 111)
    assert not should_skip_ignored(s, "group", "727631526", 222)
    assert not should_skip_ignored(s, "private", "727631526", 111)   # 私聊不拦
    assert not should_skip_ignored(None, "group", "727631526", 111)   # 无 store 不拦
    assert not should_skip_ignored(s, "group", None, 111)             # 无群不拦


### 数据文件形态（写盘内容） ###

def test_ignore_file_shape(tmp_path):
    s = IgnoreStore(str(tmp_path))
    s.add("727631526", [111, 222])
    raw = json.loads(Path(tmp_path, "ignore_list.json").read_text(encoding="utf-8"))
    assert raw == {"727631526": ["111", "222"]}
