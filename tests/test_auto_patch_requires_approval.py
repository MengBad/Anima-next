"""档3自主生成补丁的**审批边界**契约（2026-10-21，owner 拍板方案 A）。

## 背景

`ControlledIteration.maybe_auto_repair` 是完整自主闭环：扫质量退化 → 开故障单
→ LLM 生成白名单补丁 → 跑测试 → 改生产代码。

改造前它在 `controlled_iteration.py` 里写着：

    # 自动路径：跳过人工审批，但保留审计
    core.tasks.set_status(task.id, "approved_pending_apply", ...)
    msg = self.apply_core_patch(...)

即 **它自己改自己的生产代码、不经 owner 点头**。这与项目铁律
「Self improvement ≠ Self authorization」正面冲突；`CORE_PATCH_WHITELIST`
里恰是 onebot11 / handler / autonomy / react / tools —— 生产主链路承重文件。

改造后：**自主生成照旧**（发现→写补丁→落库），但落地交回 owner：
状态停在 `awaiting_approval` + 私聊推送审批提示；
owner 用既有的 `!anima iterate approve <id>` / `apply <id>` 两步通道落地。

## 本测试锁死的边界

1. 生成补丁后状态**必须是 `awaiting_approval`**，绝不能是
   `approved_pending_apply`（后者是"已批准待落地"）；
2. 生成补丁后**绝不能调用 `apply_core_patch`**（目标文件不得被改）；
3. 私聊推送审批提示，且**只发私聊**（含 task_id 便于 owner 批准）；
4. `owners` 为空 → 不发送、不伪造目标，补丁本身仍落库；
5. 通知失败不影响「补丁已落库」这一事实（fail-soft）。
"""
import types

import pytest

from anima.cognitive.controlled_iteration import ControlledIteration
from anima.core.persistence import PersistenceProvider
from anima.core.tasks import TaskStore

OWNER = 42


class _Sent(list):
    """记录 gateway.send 的消息。"""


def _core(tmp_path, owners=(OWNER,), sent=None, send_fails=False):
    persistence = PersistenceProvider(str(tmp_path))
    tasks = TaskStore(persistence)

    async def _send(msg):
        if send_fails:
            raise RuntimeError("QQ 挂了")
        if sent is not None:
            sent.append(msg)

    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            permissions=types.SimpleNamespace(owners=list(owners)),
            iteration=types.SimpleNamespace(auto_repair=True),
        ),
        tasks=tasks,
        reply_quality=None,
        audit=None,
        gateway=types.SimpleNamespace(send=_send),
    )


def _seed_core_patch(it, core, tmp_path, *, body="NEW = 2\n", monkeypatch=None):
    """造一个 core_patch 任务，并把 LLM 生成补丁那一步打桩掉。

    🔴 为什么必须打桩：`maybe_auto_repair` **必然**调
    `_generate_whitelist_patch`（内部走真 LLM，120s 超时）。测试不能真调模型
    —— 既慢又不确定。打桩后测试仍有意义：本测试锁的是「补丁生成**之后**
    如何处置」，而不是「模型能不能写出补丁」。
    """
    task = core.tasks.create("测试故障单", OWNER, ["hint"], kind="core_patch")
    core.tasks.set_proposal(
        task.id,
        {
            "unified_diffs": [],
            "file_writes": [{"path": "anima/tools/__init__.py", "content": body}],
            "target_files": ["anima/tools/__init__.py"],
            "signal": "empty_should_reply",
            "hint": "测试用例",
        },
        evidence="测试夹具：直接落一个可应用补丁",
    )
    if monkeypatch is not None:
        async def _fake_patch(core_arg, task_arg):
            return [{"path": "anima/tools/__init__.py",
                     "diff": "<<<FULL_FILE>>>\n" + body}]
        monkeypatch.setattr(it, "_generate_whitelist_patch", _fake_patch)
    return core.tasks.get(task.id)


# ── 1. 状态必须停在 awaiting_approval ──────────────────────────────────


async def test_auto_patch_stops_at_awaiting_approval(tmp_path, monkeypatch):
    sent = _Sent()
    core = _core(tmp_path, sent=sent)
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    task = _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

    await it.maybe_auto_repair(core)

    got = core.tasks.get(task.id)
    assert got.status == "awaiting_approval", (
        f"补丁生成后必须停在待审，实得 {got.status!r}"
        "（若为 approved_pending_apply 即「自批自落地」，违反铁律）"
    )


async def test_auto_patch_never_self_approves(tmp_path, monkeypatch):
    """🔴 核心：绝不能出现 approved_pending_apply（那是自批）。"""
    core = _core(tmp_path, sent=_Sent())
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    task = _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

    await it.maybe_auto_repair(core)

    got = core.tasks.get(task.id)
    assert got.status != "approved_pending_apply", "出现自批状态即违反 Self improvement≠Self authorization"


# ── 2. 绝不自动落地（目标文件不得被改）────────────────────────────────


async def test_auto_patch_does_not_touch_target_file(tmp_path, monkeypatch):
    target = tmp_path / "anima" / "tools"
    target.mkdir(parents=True)
    target_file = target / "__init__.py"
    target_file.write_text("OLD = 1\n", encoding="utf-8")

    import anima.cognitive.controlled_iteration as ci
    orig = ci._project_root
    ci._project_root = lambda: tmp_path
    try:
        core = _core(tmp_path, sent=_Sent())
        it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
        _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

        await it.maybe_auto_repair(core)

        assert target_file.read_text(encoding="utf-8") == "OLD = 1\n", (
            "自主路径不得改动生产文件 —— 落地必须由 owner 显式 apply"
        )
    finally:
        ci._project_root = orig


async def test_auto_patch_creates_no_backup_dir(tmp_path, monkeypatch):
    """没落地就不该有 core_patch_backups（那是 apply 才产生的）。"""
    core = _core(tmp_path, sent=_Sent())
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    task = _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

    await it.maybe_auto_repair(core)

    assert not (tmp_path / "core_patch_backups" / task.id).exists(), "未落地却产生了备份目录"


# ── 3. 私聊推送审批提示 ────────────────────────────────────────────────


async def test_notifies_owner_privately_with_task_id(tmp_path, monkeypatch):
    sent = _Sent()
    core = _core(tmp_path, sent=sent)
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    task = _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

    await it.maybe_auto_repair(core)

    assert len(sent) == 1, f"应推送 1 条审批提示，实得 {len(sent)}"
    msg = sent[0]
    assert msg.detail_type == "private", "审批提示必须走私聊，不得发群"
    assert task.id in msg.message, "必须带 task_id，否则 owner 无法批准"
    assert "approve" in msg.message, "必须给出批准方式"


async def test_notification_never_goes_to_group(tmp_path, monkeypatch):
    sent = _Sent()
    core = _core(tmp_path, sent=sent)
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

    await it.maybe_auto_repair(core)

    assert all(m.detail_type == "private" for m in sent), "出现群出站即违反纪律"


# ── 4. owners 为空 fail-closed ────────────────────────────────────────


async def test_no_owners_means_no_send_but_patch_persisted(tmp_path, monkeypatch):
    sent = _Sent()
    core = _core(tmp_path, owners=(), sent=sent)
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    task = _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

    await it.maybe_auto_repair(core)

    assert sent == [], "owners 为空不得发送（不伪造目标）"
    assert core.tasks.get(task.id) is not None, "补丁本身仍应落库"


# ── 5. 通知失败 fail-soft ─────────────────────────────────────────────


async def test_send_failure_does_not_raise_and_patch_survives(tmp_path, monkeypatch):
    core = _core(tmp_path, sent=_Sent(), send_fails=True)
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    task = _seed_core_patch(it, core, tmp_path, monkeypatch=monkeypatch)

    await it.maybe_auto_repair(core)   # 不应抛出

    assert core.tasks.get(task.id).status == "awaiting_approval", "通知失败不得影响补丁落库状态"


# ── 6. 未生成补丁时不得走审批状态 ──────────────────────────────────────


async def test_no_patch_means_no_awaiting_status(tmp_path, monkeypatch):
    """LLM 给不出补丁 ⇒ 任务判 failed，而不是停在待审骗 owner。"""
    core = _core(tmp_path, sent=_Sent())
    it = ControlledIteration(str(tmp_path), ticket_cooldown_sec=0)
    task = _seed_core_patch(it, core, tmp_path)   # 不打桩：下面自己打「返回空」

    async def _no_patch(core_arg, task_arg):
        return []

    monkeypatch.setattr(it, "_generate_whitelist_patch", _no_patch)

    await it.maybe_auto_repair(core)

    got = core.tasks.get(task.id)
    assert got.status == "failed", (
        f"给不出补丁应判 failed（实得 {got.status!r}）——"
        "绝不能停在 awaiting_approval 让 owner 白审一个空补丁"
    )
