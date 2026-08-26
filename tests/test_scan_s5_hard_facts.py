# -*- coding: utf-8
"""S5 真的纠错：管理员 gate / 硬事实层读写 / 窄提取契约 / 身份护栏 / 接线。"""
import io
import os

from anima.cognitive.inner_state.hard_facts import (
    HardFactStore,
    detect_correction,
    extract_replacement,
    maybe_record_correction,
)


def test_extraction_contract():
    assert extract_replacement("别管我叫哈基米，叫我饼饼") == ("哈基米", "饼饼")
    assert extract_replacement("我不是喵，我是饼饼") == ("喵", "饼饼")
    assert extract_replacement("我叫饼饼，不叫喵") == ("喵", "饼饼")
    assert extract_replacement("不要叫喵了，叫饼饼") == ("喵", "饼饼")
    assert extract_replacement("以后叫我饼饼") == ("", "饼饼")
    # 窄提取：无明确旧→新一律 None（宁漏不瞎记）
    assert extract_replacement("你叫错了") is None
    assert extract_replacement("记住我爱吃鱼干") is None
    assert extract_replacement("你叫我过去开会") is None


def test_identity_override_blocked():
    # T1+T4：身份改写不因绕开 learning 闸门而生效
    assert extract_replacement("我不是哈基米，我是AI") is None
    assert extract_replacement("我不是哈基米，我是人工智能") is None
    # 改回正名允许
    assert extract_replacement("别再叫沙琪玛了，叫哈基米") == ("沙琪玛", "哈基米")


def test_admin_gate_and_persistence(tmp_path):
    # 普通用户不触发写入（验收1）
    assert maybe_record_correction(str(tmp_path), "别管我叫哈基米，叫我饼饼",
                                   admin=False) is None
    assert not os.path.exists(os.path.join(str(tmp_path), "hard_facts.json"))
    # 管理员触发写入（验收2前半）
    rec = maybe_record_correction(str(tmp_path), "别管我叫哈基米，叫我饼饼",
                                  admin=True)
    assert rec and rec["kind"] == "always_on" and rec["old"] == "哈基米"
    assert os.path.exists(os.path.join(str(tmp_path), "hard_facts.json"))


def test_store_roundtrip_dedupe_always_on(tmp_path):
    s1 = HardFactStore(str(tmp_path))
    s1.add_correction("哈基米", "饼饼")
    s1.add_correction("喵", "饼饼")           # 同 new → 旧记录失活
    s2 = HardFactStore(str(tmp_path))          # 重新加载持久化
    active = s2.active_facts("always_on")
    assert len(active) == 1 and active[0]["old"] == "喵"
    assert "饼饼" in s2.always_on_text()


def test_contextual_match_and_aging(tmp_path):
    s = HardFactStore(str(tmp_path))
    f = {"kind": "contextual", "text": "称呼相关：喵→饼饼",
         "old": "喵", "new": "饼饼", "created_at": 1.0,
         "last_used": 1.0, "use_count": 0, "active": True}
    s.facts.append(f)
    s.save()
    s2 = HardFactStore(str(tmp_path))
    hits = s2.contextual_facts("你们家的喵在吗")   # 同实例检索
    assert len(hits) == 1
    s2.touch(hits)                                  # 老化C：use_count+1 落盘
    assert HardFactStore(str(tmp_path)).facts[-1]["use_count"] == 1


def test_cognition_wiring_contract():
    src = io.open("anima/pipeline/stages/cognition.py", encoding="utf-8").read()
    assert "maybe_record_correction" in src
    assert 'add_system_fragment("hard_facts", _hf_always, 50, protected=True)' in src
    assert "_perm_lv >= 3" in src                  # 复用 permission 阶段的档位
    assert "test_system_order" not in src          # 身份锁锚点不动（由既有测试锁）
