"""受控迭代：故障工单 + 白名单 core_patch 审批/应用分离。"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from anima.cognitive.controlled_iteration import (
    CORE_PATCH_WHITELIST,
    ControlledIteration,
    is_whitelisted_path,
)
from anima.cognitive.reply_quality import ReplyQuality
from anima.core.persistence import PersistenceProvider
from anima.core.tasks import TaskStore


def _core(tmp_path, quality: ReplyQuality | None = None):
    persistence = PersistenceProvider(str(tmp_path))
    tasks = TaskStore(persistence)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            permissions=types.SimpleNamespace(owners=[42]),
        ),
        tasks=tasks,
        reply_quality=quality,
        audit=None,
    )


def test_whitelist_paths():
    assert is_whitelisted_path("anima/tools/__init__.py")
    assert not is_whitelisted_path("anima/cli.py")
    assert "anima/channels/onebot11.py" in CORE_PATCH_WHITELIST


def test_scan_opens_fault_ticket_once(tmp_path):
    quality = ReplyQuality(str(tmp_path))
    quality.record_empty_should_reply()
    quality.record_tool_spin(3)
    core = _core(tmp_path, quality)
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=3600)
    opened = it.scan_quality_and_open_tickets(core, force=True)
    kinds = {t.kind for t in opened}
    assert "fault_ticket" in kinds
    assert any("empty_should_reply" in t.request for t in opened)
    # 冷却内再扫不应重复开同类
    again = it.scan_quality_and_open_tickets(core, force=True)
    assert again == []
    assert it.has_open_fault_tickets(core)


def test_scan_merges_related_react_faults_into_one_ticket(tmp_path):
    quality = ReplyQuality(str(tmp_path))
    quality.record_tool_spin(3)
    for _ in range(3):
        quality.record_react_max_rounds()
    core = _core(tmp_path, quality)

    opened = ControlledIteration(str(tmp_path)).scan_quality_and_open_tickets(core, force=True)

    assert len(opened) == 1
    proposal = opened[0].proposal
    assert proposal["ticket_group"] == "react_stability"
    assert set(proposal["signals"]) == {"tool_spins", "react_max_rounds"}
    assert "tests/test_react.py" in proposal["tests"]


def test_escalate_approve_apply_full_file(tmp_path, monkeypatch):
    # 使用临时“项目根”：把 controlled_iteration 的 root 指到 tmp
    quality = ReplyQuality(str(tmp_path))
    quality.record_space_link(ok=False)
    core = _core(tmp_path, quality)
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=10)
    opened = it.scan_quality_and_open_tickets(core, force=True)
    assert opened
    fault = next(t for t in opened if "space_link_fail" in t.request)

    # 构造白名单下的假文件位于 tmp 伪 root
    fake_root = tmp_path / "proj"
    target = fake_root / "anima" / "tools" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text("OLD = 1\n", encoding="utf-8")

    monkeypatch.setattr(
        "anima.cognitive.controlled_iteration._project_root",
        lambda: fake_root,
    )

    task = it.escalate_to_core_patch(core, fault.id, owner_id=42)
    assert task.kind == "core_patch"
    assert task.status == "awaiting_approval"
    assert "anima/tools/__init__.py" in (task.proposal or {}).get("target_files", [])

    # 附加 FULL_FILE diff
    it.attach_diffs(core, task.id, [{
        "path": "anima/tools/__init__.py",
        "diff": "<<<FULL_FILE>>>\nNEW = 2\n",
    }])
    approved = it.approve_core_patch(core, task.id, 42)
    assert approved.status == "approved_pending_apply"

    msg = it.apply_core_patch(core, task.id, 42)
    assert "已应用" in msg
    assert target.read_text(encoding="utf-8") == "NEW = 2\n"
    # 有备份
    bak = list((tmp_path / "core_patch_backups" / task.id).rglob("__init__.py"))
    assert bak and bak[0].read_text(encoding="utf-8") == "OLD = 1\n"
    done = core.tasks.get(task.id)
    assert done.status == "completed"


def test_apply_rejects_non_whitelist(tmp_path, monkeypatch):
    core = _core(tmp_path)
    it = ControlledIteration(str(tmp_path))
    task = core.tasks.create("x", 42, ["a"], kind="core_patch")
    core.tasks.set_proposal(task.id, {
        "kind": "core_patch",
        "target_files": ["anima/cli.py"],
        "unified_diffs": [],
    })
    with pytest.raises(ValueError):
        it.attach_diffs(core, task.id, [{
            "path": "anima/cli.py",
            "diff": "<<<FULL_FILE>>>\nhacked\n",
        }])


def test_parse_llm_file_patches_whitelist_only():
    import json
    from anima.cognitive.controlled_iteration import _parse_llm_file_patches
    raw = json.dumps([
        {"path": "anima/tools/__init__.py", "content": "x = 1\n" + ("# c\n" * 20)},
        {"path": "anima/cli.py", "content": "hacked = True\n" + ("# c\n" * 20)},
    ])
    patches = _parse_llm_file_patches(raw, allowed={"anima/tools/__init__.py", "anima/cli.py"})
    assert len(patches) == 1
    assert patches[0]["path"] == "anima/tools/__init__.py"
    assert patches[0]["diff"].startswith("<<<FULL_FILE>>>")


def test_parse_file_end_blocks():
    from anima.cognitive.controlled_iteration import _parse_llm_file_patches
    raw = (
        "说明文字应忽略\n"
        "===FILE anima/agent/react.py===\n"
        "print('ok')\n" + ("# line\n" * 5) +
        "===END===\n"
        "===FILE anima/cli.py===\n"
        "bad()\n"
        "===END===\n"
    )
    patches = _parse_llm_file_patches(
        raw, allowed={"anima/agent/react.py", "anima/cli.py"},
    )
    assert len(patches) == 1
    assert patches[0]["path"] == "anima/agent/react.py"
    assert "print('ok')" in patches[0]["diff"]


@pytest.mark.asyncio
async def test_prepare_core_patch_generates_reviewable_diff_without_writing(tmp_path, monkeypatch):
    core = _core(tmp_path)
    fake_root = tmp_path / "proj"
    target = fake_root / "anima" / "tools" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text("OLD = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "anima.cognitive.controlled_iteration._project_root",
        lambda: fake_root,
    )

    class LLM:
        async def chat(self, *args, **kwargs):
            return "===FILE anima/tools/__init__.py===\nNEW = 2\n# reviewed candidate\n===END==="

    core.llm = LLM()
    task = core.tasks.create("修复工具结果误报", 42, ["生成白名单补丁"], kind="fault_ticket")
    core.tasks.set_proposal(task.id, {
        "signal": "tool_spins",
        "suggested_files": ["anima/tools/__init__.py"],
        "tests": ["tests/test_react.py"],
    })
    core.tasks.set_status(task.id, "awaiting_approval")

    prepared = await ControlledIteration(str(tmp_path)).prepare_core_patch(
        core, task.id, owner_id=42,
    )

    assert prepared.kind == "core_patch"
    assert prepared.status == "awaiting_approval"
    assert prepared.proposal["unified_diffs"][0]["path"] == "anima/tools/__init__.py"
    assert "NEW = 2" in prepared.proposal["unified_diffs"][0]["diff"]
    assert target.read_text(encoding="utf-8") == "OLD = 1\n"


def test_core_patch_cannot_be_approved_before_diff_exists(tmp_path):
    core = _core(tmp_path)
    task = core.tasks.create("修复候选", 42, ["等待生成补丁"], kind="core_patch")
    core.tasks.set_proposal(task.id, {
        "kind": "core_patch",
        "target_files": ["anima/tools/__init__.py"],
        "unified_diffs": [],
    })
    core.tasks.set_status(task.id, "awaiting_approval")

    with pytest.raises(ValueError, match="尚未生成补丁内容"):
        ControlledIteration(str(tmp_path)).approve_core_patch(core, task.id, 42)

def test_auto_repair_override_and_quota(tmp_path):
    core = _core(tmp_path)
    core.config.iteration = types.SimpleNamespace(
        auto_repair=False, daily_quota=1, auto_repair_cooldown_sec=0,
        auto_restart=False, max_files_per_patch=2, max_file_chars=1000,
        pytest_timeout_sec=30,
    )
    it = ControlledIteration(str(tmp_path))
    assert it.is_auto_repair_enabled(core) is False
    it.set_auto_repair_override(True)
    assert it.is_auto_repair_enabled(core) is True
    assert it._quota_ok(core) is True
    it._bump_quota()
    assert it._quota_ok(core) is False
