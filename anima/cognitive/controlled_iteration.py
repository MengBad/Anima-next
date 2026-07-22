"""受控自迭代：故障工单（B）+ 白名单 core_patch（C）+ 档 3 自动修复。

档 3（auto_repair）流水线：
  扫 L0 → 开 fault_ticket → escalate → LLM 生成白名单 FULL_FILE 补丁
  → apply（先备份）→ pytest 子集 → 失败回滚 / 成功可选 systemctl restart

硬边界：
- 只改 CORE_PATCH_WHITELIST
- 绝不写 config.yaml / 权限 / 密钥 / _deploy 凭据
- 日配额 + 冷却；Owner 可用 iterate auto off 紧急关闭
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.iterate")

_BJ = timezone(timedelta(hours=8))
_STATE_FILE = "controlled_iteration.json"

CORE_PATCH_WHITELIST: frozenset[str] = frozenset({
    "anima/channels/onebot11.py",
    "anima/tools/__init__.py",
    "anima/llm/handler.py",
    "anima/agent/react.py",
    "anima/cognitive/reply_quality.py",
    "anima/pipeline/stages.py",
    "anima/cognitive/controlled_iteration.py",
    "anima/cognitive/danger.py",
    "anima/core/autonomy.py",
})

_BLOCKED_PATH_SUBSTR = (
    "config.yaml", "config.yml", ".env", "secret", "credential",
    "permissions_override", "api_key", "password",
)

_FAULT_RULES: list[dict[str, Any]] = [
    {
        "key": "empty_should_reply",
        "threshold": 1,
        "title": "应回却空：进入回复路径但无可用正文",
        "files": ["anima/llm/handler.py", "anima/agent/react.py"],
        "tests": ["tests/test_react.py", "tests/test_group_reply_gate_b.py"],
        "hint": "检查 ReAct finalize、空 content 重试、出站前 empty 遥测。",
    },
    {
        "key": "tool_spins",
        "ticket_group": "react_stability",
        "group_title": "ReAct 收敛异常：工具重复尝试或推理轮次过多",
        "threshold": 2,
        "title": "工具空转：同工具同参重复调用",
        "files": ["anima/agent/react.py", "anima/pipeline/stages.py"],
        "tests": ["tests/test_react.py"],
        "hint": "加强反死循环与工具结果可行动性提示。",
    },
    {
        "key": "space_link_fail",
        "threshold": 1,
        "title": "B 站主页链接处理失败",
        "files": ["anima/tools/__init__.py"],
        "tests": ["tests/test_bilibili_space_read.py"],
        "hint": "polymer/arc 回退与反爬兼容。",
    },
    {
        "key": "react_max_rounds",
        "ticket_group": "react_stability",
        "group_title": "ReAct 收敛异常：工具重复尝试或推理轮次过多",
        "threshold": 3,
        "title": "ReAct 频繁触顶",
        "files": ["anima/agent/react.py", "anima/pipeline/stages.py"],
        "tests": ["tests/test_react.py"],
        "hint": "减少无效 web_search，尽早 finalize。",
    },
    {
        "key": "outbound_failures",
        "threshold": 1,
        "title": "QQ 出站失败",
        "files": ["anima/channels/onebot11.py", "anima/llm/handler.py"],
        "tests": ["tests/test_onebot_backpressure.py", "tests/test_image_sticker_parse.py"],
        "hint": "检查消息段与发送失败处理。",
    },
    {
        "key": "identity_leaks",
        "threshold": 1,
        "title": "身份泄漏拦截",
        "files": ["anima/pipeline/stages.py"],
        "tests": ["tests/test_guardrail_analysis.py"],
        "hint": "人格纠偏与泄漏检测。",
    },
]

_SIGNAL_TESTS = {r["key"]: list(r.get("tests") or []) for r in _FAULT_RULES}


def _day() -> str:
    return datetime.now(_BJ).date().isoformat()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _norm_rel(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def is_whitelisted_path(path: str) -> bool:
    p = _norm_rel(path)
    if any(b in p.lower() for b in _BLOCKED_PATH_SUBSTR):
        return False
    return p in CORE_PATCH_WHITELIST


def _iter_cfg(core: Any) -> Any:
    return getattr(getattr(core, "config", None), "iteration", None)


class ControlledIteration:
    def __init__(self, data_dir: str, *, ticket_cooldown_sec: int = 6 * 3600):
        self.persistence = PersistenceProvider(data_dir)
        self.ticket_cooldown_sec = max(300, int(ticket_cooldown_sec))
        raw = self.persistence.read_json(_STATE_FILE, {}) or {}
        self.state: dict[str, Any] = raw if isinstance(raw, dict) else {}
        self.state.setdefault("last_scan_at", 0.0)
        self.state.setdefault("last_auto_repair_at", 0.0)
        self.state.setdefault("auto_repair_day", "")
        self.state.setdefault("auto_repair_count", 0)
        self.state.setdefault("tickets", {})
        self.state.setdefault("auto_repair_enabled_override", None)  # None|bool runtime kill switch
        self.state.setdefault("version", 2)
        self._auto_running = False

    def _save(self) -> None:
        self.persistence.write_json(_STATE_FILE, self.state)

    def _fingerprint(self, key: str, day: str | None = None) -> str:
        raw = f"{key}:{day or _day()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def is_auto_repair_enabled(self, core: Any) -> bool:
        override = self.state.get("auto_repair_enabled_override")
        if override is not None:
            return bool(override)
        cfg = _iter_cfg(core)
        return bool(cfg and getattr(cfg, "auto_repair", False))

    def set_auto_repair_override(self, enabled: bool | None) -> None:
        self.state["auto_repair_enabled_override"] = enabled
        self._save()

    def has_open_fault_tickets(self, core: Any) -> bool:
        store = getattr(core, "tasks", None)
        if store is None:
            return False
        open_status = {"planned", "running", "awaiting_approval", "approved_pending_apply"}
        for item in store.list():
            if getattr(item, "kind", "") in {"fault_ticket", "core_patch"} and item.status in open_status:
                return True
        return False

    def _quota_ok(self, core: Any) -> bool:
        cfg = _iter_cfg(core)
        quota = int(getattr(cfg, "daily_quota", 1) or 1) if cfg else 1
        day = _day()
        if self.state.get("auto_repair_day") != day:
            self.state["auto_repair_day"] = day
            self.state["auto_repair_count"] = 0
        return int(self.state.get("auto_repair_count") or 0) < max(0, quota)

    def _bump_quota(self) -> None:
        day = _day()
        if self.state.get("auto_repair_day") != day:
            self.state["auto_repair_day"] = day
            self.state["auto_repair_count"] = 0
        self.state["auto_repair_count"] = int(self.state.get("auto_repair_count") or 0) + 1
        self.state["last_auto_repair_at"] = time.time()
        self._save()

    def scan_quality_and_open_tickets(self, core: Any, *, force: bool = False) -> list[Any]:
        now = time.time()
        min_interval = 120 if force else 600
        if not force and now - float(self.state.get("last_scan_at") or 0) < min_interval:
            return []
        self.state["last_scan_at"] = now

        quality = getattr(core, "reply_quality", None)
        if quality is None or not hasattr(quality, "report"):
            self._save()
            return []
        report = quality.report() or {}
        owners = list(getattr(getattr(core.config, "permissions", None), "owners", []) or [])
        creator = int(owners[0]) if owners else 0
        if creator <= 0 or getattr(core, "tasks", None) is None:
            self._save()
            return []

        opened: list[Any] = []
        tickets = self.state.setdefault("tickets", {})
        grouped_rules: dict[str, list[dict[str, Any]]] = {}
        for rule in _FAULT_RULES:
            key = str(rule["key"])
            if int(report.get(key, 0) or 0) >= int(rule["threshold"]):
                grouped_rules.setdefault(str(rule.get("ticket_group") or key), []).append(rule)

        for group, rules in grouped_rules.items():
            keys = [str(rule["key"]) for rule in rules]
            signal_counts = {key: int(report.get(key, 0) or 0) for key in keys}
            fp = self._fingerprint(group)
            prev = tickets.get(fp) if isinstance(tickets.get(fp), dict) else None
            if prev and now - float(prev.get("opened_at") or 0) < self.ticket_cooldown_sec:
                tid = str(prev.get("task_id") or "")
                existing = core.tasks.get(tid) if tid else None
                if existing and existing.status not in {"completed", "failed", "cancelled", "rejected"}:
                    continue
            dup = False
            for item in core.tasks.list():
                proposal = getattr(item, "proposal", {}) or {}
                existing_signals = set(proposal.get("signals") or [proposal.get("signal")])
                if (
                    getattr(item, "kind", "") in {"fault_ticket", "core_patch"}
                    and item.status in {"planned", "running", "awaiting_approval", "approved_pending_apply"}
                    and (
                        proposal.get("ticket_group") == group
                        or bool(existing_signals.intersection(keys))
                        or any(key in (item.request or "") for key in keys)
                    )
                ):
                    dup = True
                    break
            if dup:
                continue

            files = sorted({
                f for rule in rules for f in (rule.get("files") or [])
                if is_whitelisted_path(f)
            })
            tests = list(dict.fromkeys(
                test for rule in rules for test in (rule.get("tests") or [])
            ))
            hints = [str(rule.get("hint") or "")[:500] for rule in rules if rule.get("hint")]
            title = str(rules[0].get("group_title") or rules[0]["title"])
            plan = [
                "核对质量遥测证据",
                "白名单内最小修复",
                "pytest 子集门禁",
                "apply / 自动修复或 Owner 审批",
            ]
            request = (
                f"[故障工单/{','.join(keys)}] {title}（今日信号："
                f"{'、'.join(f'{key}={count}' for key, count in signal_counts.items())}）\n"
                f"建议文件：{', '.join(files) or '（未映射）'}\n"
                f"提示：{'；'.join(hints)}"
            )
            task = core.tasks.create(request, creator, plan, kind="fault_ticket")
            proposal = {
                "kind": "fault_ticket",
                "ticket_group": group,
                "signal": keys[0],  # 兼容旧版 core_patch 升级路径。
                "signals": keys,
                "signal_counts": signal_counts,
                "count": sum(signal_counts.values()),
                "date": report.get("date") or _day(),
                "suggested_files": files,
                "tests": tests,
                "hint": "；".join(hints)[:1000],
                "quality_snapshot": {
                    k: int(report.get(k, 0) or 0)
                    for k in (
                        "total_replies", "empty_should_reply", "tool_spins",
                        "space_link_ok", "space_link_fail", "react_max_rounds",
                        "outbound_failures", "tool_failures", "identity_leaks",
                    )
                },
                "notes": ["只读聚合指标，无聊天正文。"],
            }
            evidence = " ".join(f"{key}={count}" for key, count in signal_counts.items())
            core.tasks.set_proposal(task.id, proposal, evidence=f"scan {_day()} {evidence}")
            tickets[fp] = {"task_id": task.id, "opened_at": now, "key": group, "count": sum(signal_counts.values())}
            opened.append(task)
            try:
                if getattr(core, "audit", None):
                    core.audit.emit("fault_ticket_opened", task_id=task.id, signal=group, count=sum(signal_counts.values()))
            except Exception:
                pass
            logger.info("故障工单已开 task=%s group=%s signals=%s", task.id, group, ",".join(keys))

        self._save()
        return opened

    def escalate_to_core_patch(self, core: Any, task_id: str, *, owner_id: int) -> Any:
        task = core.tasks.get(task_id)
        if task is None:
            raise ValueError("未找到任务")
        if task.kind not in {"fault_ticket", "core_patch", "diagnostic"}:
            raise ValueError(f"任务 kind={task.kind} 不能升级为 core_patch")
        prop = dict(task.proposal or {})
        files = [f for f in (prop.get("suggested_files") or []) if is_whitelisted_path(f)]
        files = sorted(set(files))
        if not files:
            raise ValueError("无白名单建议文件，拒绝生成 core_patch")
        patch_prop = {
            "kind": "core_patch",
            "from_task": task.id,
            "signal": prop.get("signal") or "",
            "target_files": files,
            "tests": prop.get("tests") or _SIGNAL_TESTS.get(str(prop.get("signal") or ""), []),
            "whitelist": sorted(CORE_PATCH_WHITELIST),
            "unified_diffs": prop.get("unified_diffs") or [],
            "file_writes": prop.get("file_writes") or [],
            "auto_apply": False,
            "hint": prop.get("hint") or "",
            "quality_snapshot": prop.get("quality_snapshot") or {},
            "effect": "白名单核心补丁提案；审批与 apply 可分离，档3可自动。",
            "risk": "错误补丁可能影响回复链路；有备份与 pytest 门禁。",
            "rollback": "data/core_patch_backups/<task_id>/",
        }
        task.kind = "core_patch"
        task.updated_at = time.time()
        core.tasks.set_proposal(
            task.id, patch_prop,
            evidence=f"escalate by {owner_id} → core_patch files={','.join(files)}",
        )
        return core.tasks.get(task.id)

    def attach_diffs(self, core: Any, task_id: str, diffs: list[dict[str, str]]) -> Any:
        task = core.tasks.get(task_id)
        if task is None:
            raise ValueError("未找到任务")
        if task.kind != "core_patch":
            raise ValueError("仅 core_patch 可附加 diff")
        cleaned: list[dict[str, str]] = []
        for item in diffs or []:
            if not isinstance(item, dict):
                continue
            path = _norm_rel(str(item.get("path") or ""))
            diff = str(item.get("diff") or "")
            if not path or not diff:
                continue
            if not is_whitelisted_path(path):
                raise ValueError(f"路径不在白名单：{path}")
            if len(diff) > 200_000:
                raise ValueError(f"diff 过大：{path}")
            cleaned.append({"path": path, "diff": diff})
        if not cleaned:
            raise ValueError("没有有效 diff")
        prop = dict(task.proposal or {})
        prop["unified_diffs"] = cleaned
        prop["kind"] = "core_patch"
        core.tasks.set_proposal(task.id, prop, evidence=f"附加 {len(cleaned)} 个 diff")
        return core.tasks.get(task.id)

    async def prepare_core_patch(self, core: Any, task_id: str, *, owner_id: int) -> Any:
        """Owner 明确触发补丁生成；只写任务 proposal，不改源码、不重启。"""
        task = core.tasks.get(task_id)
        if task is None:
            raise ValueError("未找到任务")
        if task.kind == "fault_ticket":
            task = self.escalate_to_core_patch(core, task_id, owner_id=owner_id)
        if task.kind != "core_patch":
            raise ValueError("只有故障工单或 core_patch 可以生成补丁")
        if task.status != "awaiting_approval":
            raise ValueError(f"当前状态 {task.status} 不能生成补丁")
        diffs = await self._generate_whitelist_patch(core, task)
        if not diffs:
            raise ValueError("工程代理没有生成可验证的白名单补丁")
        prepared = self.attach_diffs(core, task.id, diffs)
        core.tasks.set_status(
            task.id, "awaiting_approval",
            f"Owner {owner_id} 请求生成修复方案；已附加 {len(diffs)} 个候选文件",
        )
        return prepared

    def approve_core_patch(self, core: Any, task_id: str, reviewer: int) -> Any:
        task = core.tasks.get(task_id)
        if task is None:
            raise ValueError("未找到任务")
        if task.kind != "core_patch":
            raise ValueError("非 core_patch 任务")
        if task.status != "awaiting_approval":
            raise ValueError(f"当前状态 {task.status} 不能审批")
        proposal = task.proposal or {}
        if not (proposal.get("unified_diffs") or proposal.get("file_writes")):
            raise ValueError("尚未生成补丁内容，不能批准；请先生成修复方案")
        core.tasks.approve(task_id, reviewer)
        return core.tasks.set_status(
            task_id, "approved_pending_apply",
            f"Owner {reviewer} 已批准；apply 后才会改磁盘",
        )

    def apply_core_patch(
        self, core: Any, task_id: str, reviewer: int, *,
        skip_status_check: bool = False,
        run_tests: bool = True,
        restart: bool = False,
    ) -> str:
        task = core.tasks.get(task_id)
        if task is None:
            raise ValueError("未找到任务")
        if task.kind != "core_patch":
            raise ValueError("非 core_patch 任务")
        if not skip_status_check and task.status != "approved_pending_apply":
            raise ValueError(f"当前状态 {task.status}，请先审批")

        prop = dict(task.proposal or {})
        diffs = prop.get("unified_diffs") or []
        writes = prop.get("file_writes") or []
        if not diffs and not writes:
            raise ValueError("提案没有可机器应用的 diff/file_writes")

        root = _project_root()
        data_dir = Path(str(core.config.persistence.data_dir))
        backup_root = data_dir / "core_patch_backups" / task_id
        backup_root.mkdir(parents=True, exist_ok=True)
        applied: list[str] = []
        touched: list[Path] = []

        try:
            for item in writes:
                if not isinstance(item, dict):
                    continue
                path = _norm_rel(str(item.get("path") or ""))
                content = item.get("content")
                if not is_whitelisted_path(path):
                    raise ValueError(f"拒绝非白名单写入：{path}")
                if not isinstance(content, str) or len(content) > 500_000:
                    raise ValueError(f"非法 content：{path}")
                target = root / path
                if not str(target.resolve()).startswith(str(root.resolve())):
                    raise ValueError(f"路径穿越拒绝：{path}")
                if target.exists():
                    bak = backup_root / path
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, bak)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                applied.append(f"write {path}")
                touched.append(target)

            for item in diffs:
                if not isinstance(item, dict):
                    continue
                path = _norm_rel(str(item.get("path") or ""))
                diff = str(item.get("diff") or "")
                if not is_whitelisted_path(path):
                    raise ValueError(f"拒绝非白名单 diff：{path}")
                target = root / path
                if not str(target.resolve()).startswith(str(root.resolve())):
                    raise ValueError(f"路径穿越拒绝：{path}")
                if target.exists():
                    bak = backup_root / path
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, bak)
                new_content = _apply_unified_diff_or_raise(
                    target.read_text(encoding="utf-8") if target.exists() else "",
                    diff,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_content, encoding="utf-8")
                applied.append(f"patch {path}")
                touched.append(target)

            if run_tests:
                tests = list(prop.get("tests") or []) or ["tests/test_controlled_iteration.py"]
                ok, out = _run_pytest(root, tests, timeout=int(
                    getattr(_iter_cfg(core), "pytest_timeout_sec", 120) or 120
                ))
                if not ok:
                    _restore_backup(backup_root, root)
                    core.tasks.set_status(
                        task_id, "failed",
                        f"pytest 失败已回滚：{out[:1500]}",
                    )
                    raise ValueError(f"pytest 失败已回滚：{out[:400]}")

            # 防止 root 写盘后 anima 用户读不了 data/（会直接起不来、群聊全哑）
            _ensure_anima_ownership([data_dir, root / "anima", *touched])

            restart_note = ""
            if restart:
                restart_note = _try_restart_anima()

            core.tasks.set_status(
                task_id, "completed",
                f"reviewer={reviewer} applied={','.join(applied)}; backup={backup_root}; {restart_note}",
            )
            try:
                if getattr(core, "audit", None):
                    core.audit.emit(
                        "core_patch_applied", task_id=task_id, reviewer=reviewer,
                        files=",".join(applied)[:300], auto=bool(skip_status_check),
                    )
            except Exception:
                pass
            return (
                f"已应用 core_patch {task_id}：{', '.join(applied)}\n"
                f"备份：{backup_root}\n{restart_note}"
            )
        except Exception:
            # 尽力回滚
            try:
                if backup_root.exists():
                    _restore_backup(backup_root, root)
            except Exception:
                logger.exception("回滚失败 task=%s", task_id)
            raise

    async def maybe_auto_repair(self, core: Any) -> str | None:
        """档 3：若开启且有配额，自动处理一条故障单。"""
        if self._auto_running:
            return None
        if not self.is_auto_repair_enabled(core):
            return None
        cfg = _iter_cfg(core)
        cooldown = int(getattr(cfg, "auto_repair_cooldown_sec", 3600) or 3600) if cfg else 3600
        if time.time() - float(self.state.get("last_auto_repair_at") or 0) < cooldown:
            return None
        if not self._quota_ok(core):
            logger.info("档3自动修复：今日配额已用尽")
            return None

        self._auto_running = True
        try:
            self.scan_quality_and_open_tickets(core, force=False)
            task = self._pick_auto_candidate(core)
            if task is None:
                return None
            owners = list(getattr(getattr(core.config, "permissions", None), "owners", []) or [])
            owner_id = int(owners[0]) if owners else 0
            if owner_id <= 0:
                return None

            # escalate if needed
            if task.kind == "fault_ticket":
                task = self.escalate_to_core_patch(core, task.id, owner_id=owner_id)

            diffs = await self._generate_whitelist_patch(core, task)
            if not diffs:
                core.tasks.set_status(task.id, "failed", "自动修复：LLM 未生成可用白名单补丁")
                self.state["last_auto_repair_at"] = time.time()
                self._save()
                return f"自动修复失败 task={task.id}：无可用补丁"

            self.attach_diffs(core, task.id, diffs)
            # 自动路径：跳过人工审批，但保留审计
            core.tasks.set_status(task.id, "approved_pending_apply", "档3自动修复：跳过人工审批（已审计）")
            msg = self.apply_core_patch(
                core, task.id, owner_id,
                skip_status_check=False,
                run_tests=True,
                restart=bool(getattr(cfg, "auto_restart", True)) if cfg else True,
            )
            # apply_core_patch requires approved_pending_apply - we set it above
            self._bump_quota()
            logger.warning("档3自动修复成功 task=%s %s", task.id, msg[:200])
            return msg
        except Exception as exc:
            logger.exception("档3自动修复失败")
            self.state["last_auto_repair_at"] = time.time()
            self._save()
            return f"自动修复异常：{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            self._auto_running = False

    def _pick_auto_candidate(self, core: Any) -> Any | None:
        open_status = {"awaiting_approval", "planned", "approved_pending_apply"}
        candidates = [
            t for t in core.tasks.list()
            if t.kind in {"fault_ticket", "core_patch"} and t.status in open_status
        ]
        if not candidates:
            return None
        # 优先故障单、更早创建的
        candidates.sort(key=lambda t: (0 if t.kind == "fault_ticket" else 1, t.created_at))
        return candidates[0]

    async def _generate_whitelist_patch(self, core: Any, task: Any) -> list[dict[str, str]]:
        llm = getattr(core, "llm", None)
        if llm is None:
            return []
        prop = dict(task.proposal or {})
        files = [f for f in (prop.get("target_files") or prop.get("suggested_files") or []) if is_whitelisted_path(f)]
        cfg = _iter_cfg(core)
        max_chars = int(getattr(cfg, "max_file_chars", 120_000) or 120_000) if cfg else 120_000
        # 自动修复每次只改 1 个文件，避免输出截断导致解析失败
        files = files[:1]
        root = _project_root()
        blobs: list[str] = []
        for rel in files:
            path = root / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > max_chars:
                text = text[:max_chars] + "\n# ... truncated ...\n"
            blobs.append(f"### FILE: {rel}\n```python\n{text}\n```")
        if not blobs:
            return []

        prompt = (
            "你是 Anima 生产机器人的自动修复代理。根据故障说明，对下列白名单源码做【最小必要】修复。\n"
            "硬性输出格式（不要 JSON，不要 markdown 代码围栏）：\n"
            "===FILE anima/xxx.py===\n"
            "<该文件修复后的完整正文>\n"
            "===END===\n"
            "path 必须在允许列表内。禁止改密钥/权限/配置；禁止大删无关代码；Python 3.12+。\n\n"
            f"故障：{(task.request or '')[:800]}\n"
            f"信号：{prop.get('signal')}\n"
            f"提示：{prop.get('hint')}\n"
            f"允许文件：{', '.join(files)}\n\n"
            + "\n\n".join(blobs)
        )
        try:
            from anima.llm.usage import llm_scope
            with llm_scope("iterate", "auto_repair"):
                raw = await asyncio.wait_for(
                    llm.chat(
                        [{"role": "user", "content": prompt}],
                        temperature=0.1, max_tokens=12000,
                    ),
                    timeout=120.0,
                )
        except Exception:
            logger.exception("自动修复 LLM 调用失败")
            return []

        return _parse_llm_file_patches(str(raw or ""), allowed=set(files))

    def format_status(self, core: Any) -> str:
        open_faults, patches = [], []
        for item in (core.tasks.list() if getattr(core, "tasks", None) else []):
            if item.kind == "fault_ticket" and item.status not in {"completed", "cancelled", "rejected"}:
                open_faults.append(f"{item.id}[{item.status}]")
            if item.kind == "core_patch" and item.status not in {"completed", "cancelled", "rejected"}:
                patches.append(f"{item.id}[{item.status}]")
        auto = self.is_auto_repair_enabled(core)
        qday = self.state.get("auto_repair_day") or _day()
        qcnt = int(self.state.get("auto_repair_count") or 0)
        cfg = _iter_cfg(core)
        quota = int(getattr(cfg, "daily_quota", 1) or 1) if cfg else 1
        return (
            f"受控迭代：自动修复={'ON' if auto else 'OFF'}；"
            f"今日配额 {qcnt}/{quota}（{qday}）；"
            f"故障单 {len(open_faults)}（{', '.join(open_faults[:5]) or '无'}）；"
            f"core_patch {len(patches)}（{', '.join(patches[:5]) or '无'}）。"
        )


def _parse_llm_file_patches(raw: str, *, allowed: set[str]) -> list[dict[str, str]]:
    text = (raw or "").strip()
    out: list[dict[str, str]] = []

    def _accept(path: str, content: str) -> None:
        path = _norm_rel(path)
        if path not in allowed or not is_whitelisted_path(path):
            return
        if not isinstance(content, str) or len(content) < 8:
            return
        if len(content) > 500_000:
            return
        low = content.lower()
        if re.search(r"api_key\s*=\s*['\"][^'\"]+['\"]", content, re.I):
            return
        if "password" in low and re.search(r"password\s*=\s*['\"][^'\"]+['\"]", content, re.I):
            return
        out.append({"path": path, "diff": "<<<FULL_FILE>>>\n" + content})

    # 格式 A：===FILE path=== ... ===END===
    for m in re.finditer(
        r"===FILE\s+([^\s=]+)\s*===\s*\n([\s\S]*?)\n===END===",
        text,
    ):
        _accept(m.group(1).strip(), m.group(2))
    if out:
        return out

    # 格式 B：JSON 数组（模型常转义失败，作次选）
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text_json = fence.group(1).strip()
    else:
        text_json = text
    start, end = text_json.find("["), text_json.rfind("]")
    if start >= 0 and end > start:
        blob = text_json[start:end + 1]
        data = None
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            # 宽松：把未转义控制字符尽量压掉后再试
            try:
                data = json.loads(blob.encode("utf-8", "backslashreplace").decode("unicode_escape"))
            except Exception:
                data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    _accept(str(item.get("path") or ""), str(item.get("content") or ""))
    return out


def _run_pytest(root: Path, tests: list[str], *, timeout: int = 120) -> tuple[bool, str]:
    args = [sys.executable, "-m", "pytest", "-q", "--tb=line"]
    existing = [t for t in tests if (root / t).exists()]
    if not existing:
        existing = ["tests/test_controlled_iteration.py"] if (root / "tests/test_controlled_iteration.py").exists() else []
    if not existing:
        return True, "no tests found, skipped"
    args.extend(existing)
    try:
        proc = subprocess.run(
            args, cwd=str(root), capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return proc.returncode == 0, out[-4000:]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _restore_backup(backup_root: Path, project_root: Path) -> None:
    if not backup_root.exists():
        return
    for bak in backup_root.rglob("*"):
        if not bak.is_file():
            continue
        rel = bak.relative_to(backup_root)
        dest = project_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bak, dest)


def _ensure_anima_ownership(paths: list[Path]) -> None:
    """apply 后把关键路径改回 anima 用户，避免 PermissionError 起不来。"""
    for path in paths:
        try:
            if path is None:
                continue
            p = Path(path)
            if not p.exists():
                continue
            # 仅在看起来是 Linux 服务环境时尝试
            subprocess.run(
                ["chown", "-R", "anima:anima", str(p)],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            logger.debug("chown 跳过/失败 path=%s", path, exc_info=True)


def _try_restart_anima() -> str:
    try:
        # 重启前再保险一次 data 目录属主
        data = Path("/opt/anima/data")
        if data.is_dir():
            subprocess.run(
                ["chown", "-R", "anima:anima", str(data)],
                capture_output=True, text=True, timeout=30,
            )
        proc = subprocess.run(
            ["systemctl", "restart", "anima"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            return "systemctl restart anima: OK"
        return f"systemctl restart 失败 rc={proc.returncode}: {(proc.stderr or proc.stdout)[:200]}"
    except FileNotFoundError:
        return "无 systemctl，跳过重启（文件已写入，需手动重启）"
    except Exception as exc:
        return f"重启异常：{type(exc).__name__}: {exc}"


def _apply_unified_diff_or_raise(original: str, diff_text: str) -> str:
    text = diff_text.replace("\r\n", "\n")
    if text.startswith("<<<FULL_FILE>>>\n"):
        return text[len("<<<FULL_FILE>>>\n"):]
    if text.startswith("<<<FULL_FILE>>>"):
        return text.split("\n", 1)[1] if "\n" in text else ""
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "old.txt"
            src.write_text(original, encoding="utf-8")
            body = text
            if not body.lstrip().startswith("---"):
                body = "--- a/file\n+++ b/file\n" + body
            proc = subprocess.run(
                ["patch", "-p1", "--batch", "--reject-file=-", str(src)],
                input=body, text=True, capture_output=True, timeout=15, cwd=tmp,
            )
            if proc.returncode == 0:
                return src.read_text(encoding="utf-8")
            raise RuntimeError(f"patch 失败 rc={proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
    except FileNotFoundError as exc:
        raise RuntimeError("系统无 patch 命令；请使用 <<<FULL_FILE>>> 全文替换") from exc


_iterate: ControlledIteration | None = None
_iterate_dir: str | None = None


def get_controlled_iteration(data_dir: str = "data") -> ControlledIteration:
    global _iterate, _iterate_dir
    if _iterate is None or _iterate_dir != data_dir:
        _iterate = ControlledIteration(data_dir)
        _iterate_dir = data_dir
    return _iterate


def close_controlled_iteration() -> None:
    global _iterate, _iterate_dir
    _iterate = None
    _iterate_dir = None
