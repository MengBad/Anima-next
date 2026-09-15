"""人格单源化回归（队列②，2026-09-12）：正本唯一、fail-closed、双出口同源。

- 被动回复（pipeline/stages/cognition）与主动发言（core/autonomy）必须共用
  anima/cognitive/persona_core 装配器，正本 = config/persona_core.yaml；
- 旧的内联回退副本与 autonomy 手写人格副本已删除，不得回潮。
"""
import types

import pytest

import anima.cognitive.persona_core as pc
from anima.cognitive.persona_core import (
    assemble_persona_system,
    load_persona_template,
)


def test_template_loads_with_all_placeholders():
    tmpl = load_persona_template()
    assert "哈基米" in tmpl
    for key in pc.SECTION_KEYS:
        assert "{" + key + "}" in tmpl


def test_missing_system_prompt_raises(tmp_path, monkeypatch):
    p = tmp_path / "persona_core.yaml"
    p.write_text("other: 1\n", encoding="utf-8")
    monkeypatch.setattr(pc, "persona_yaml_path", lambda: p)
    with pytest.raises(RuntimeError):
        load_persona_template()


def test_assemble_fills_given_and_blanks_missing_sections():
    out = assemble_persona_system(time_context="TIME_HERE", desire_context="DESIRE_HERE")
    assert "TIME_HERE" in out and "DESIRE_HERE" in out
    for key in ("self_notes", "world_context", "scar_context"):
        assert "{" + key + "}" not in out
    assert "{time_context}" not in out


def test_assemble_appends_extra_rules():
    out = assemble_persona_system(time_context="T", extra_rules="## 出口守则\n只说一句")
    assert "## 出口守则" in out and "只说一句" in out


def test_active_speech_system_uses_yaml_persona():
    """主动发言 system 必须出自正本：yaml 新版段落在场，旧手写副本独有措辞绝迹。"""
    from anima.core.autonomy import _build_active_speech_system

    system = _build_active_speech_system("2026-09-12 12:00 (下午)", "欲望片段", "发言守则文本")
    assert "身份与边界" in system  # yaml 正本段落（旧内联副本没有）
    assert "发言守则文本" in system and "欲望片段" in system
    assert "嘴有点欠" not in system  # 旧 autonomy 手写副本独有开场白
    assert "核心气质" not in system  # 旧内联副本独有段落
    assert "身份锁定" not in system  # 护栏不与 yaml 身份与边界重复（2026-09-12 去重）


def test_cognition_and_autonomy_share_assembler():
    """两个出口必须引用同一装配器（防止影子副本回潮）。"""
    import anima.pipeline.stages.cognition as cognition
    import inspect

    from anima.core import autonomy

    src_cog = inspect.getsource(cognition)
    src_auto = inspect.getsource(autonomy)
    assert "from anima.cognitive.persona_core import" in src_cog
    assert "assemble_persona_system" in src_cog
    assert "assemble_persona_system" in src_auto
    # 内联副本回潮检测： cognition 不再携带完整内联人格文本
    assert 'SYSTEM_PROMPT = """' not in src_cog
    assert "嘴有点欠的猫娘" not in src_auto


def test_heartbeat_persists_consolidation_date(tmp_path):
    """微修回归：last_consolidation_date 必须随 persist()/restore() 持久化。"""
    from anima.core.autonomy import Heartbeat

    hb = Heartbeat.__new__(Heartbeat)
    hb._path = tmp_path / "heartbeat.json"
    hb._persist = types.SimpleNamespace(
        write_json=lambda p, d: p.write_text(__import__("json").dumps(d), encoding="utf-8"),
        read_json=lambda p, d: __import__("json").loads(p.read_text(encoding="utf-8")) if p.exists() else d,
    )
    hb._group_last_speak = {}
    hb._group_speak_count = {}
    hb._group_total_msgs = {}
    hb._group_last_msg = {}
    hb._active_feedback_pending = {}
    hb._active_feedback_stats = {}
    hb._recent_active_replies = {}
    hb._tokens_today = 0
    hb._day_reset = 0.0
    hb._last_active_speak = 0.0
    hb._last_morning_dream_date = ""
    hb._last_secret_gen_date = ""
    hb._last_consolidation_date = "2026-09-12"
    hb.persist()
    hb._last_consolidation_date = ""
    hb.restore()
    assert hb._last_consolidation_date == "2026-09-12"
