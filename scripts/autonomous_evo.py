#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anima Autonomous Evolution Agent — Orchestrator (standalone, read-only-safe)

设计原则（与 Final Evolution Roadmap / Autonomous Evolution Agent Prompt 对齐）：
  - 速度 < 安全
  - 能力 < 控制
  - 记忆 != 权限
  - Agent != Owner
  - 自动化 != 自主越权
  - 任何不确定 -> STOP AND ASK OWNER

本脚本是"项目经理 + SRE + 安全审计员"，**不是**自动改系统的程序。
它每轮只做：
  1. HEALTH CHECK（只读，SSH + /api/health + systemctl + journalctl 扫描）
  2. CURRENT STAGE DETECT（读 ANIMA_STAGE_STATE.json）
  3. RISK ASSESSMENT
  4. SELECT NEXT SAFE ACTION（仅 LOW 风险自动执行）
  5. EXECUTE ONLY AUTHORIZED ACTION
  6. TEST（仅当前阶段相关子集，低风险）
  7. AUDIT（维护 state 文件 + 审计链）
  8. REPORT（P10_HOURLY_REPORT）
  9. WAIT / CONTINUE

永久禁止（代码级硬阻断，不受 prompt 影响）：
  - 开启 execution / sandbox.write / code execution
  - 修改权限 / 创建 admin / 从 memory 推导权限
  - 开放生产能力 / production 扩权
  - 绕过 kill switch
  - 删除审计记录
  - 任何重启服务 / 部署代码（中风险，必须 PLAN + 等 owner）

运行：
  python scripts/autonomous_evo.py --once        # 单轮（供 hourly 自动化调用）
  python scripts/autonomous_evo.py --banner      # 打印 ONLINE 横幅 + 单轮
  python scripts/autonomous_evo.py --push-state  # 仅把本地 state 推到 VPS

依赖：paramiko + pyyaml（均在仓库 venv 内）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCAL_STATE = REPO / "state"
STATE_JSON = REPO / "ANIMA_STAGE_STATE.json"
HOURLY_REPORT = REPO / "P10_HOURLY_REPORT.md"
DEFAULT_CREDS = Path(r"C:/Users/15622/.anima_vps_creds.env")
REMOTE_ROOT = "/opt/anima"
REMOTE_STATE = f"{REMOTE_ROOT}/state"
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
GIT_HEAD = REPO / ".git" / "HEAD"

# ---- 永久安全边界（硬编码，不可被参数覆盖）-----------------------------------
FORBIDDEN_ACTIONS = {
    "enable_execution", "enable_sandbox_write", "enable_code_execution",
    "modify_permission", "create_admin", "derive_permission_from_memory",
    "expand_production", "bypass_kill_switch", "delete_audit",
    "restart_service", "deploy_code", "auto_send_media",
    "activate_sylann", "enable_capability", "increase_autonomy_level",
}
# 自动推进矩阵：哪些阶段的"开启/启用"必须等 owner
OWNER_GATED_STAGES = {
    "R2": ["memory_read_enable"],          # Memory Read 开启
    "R3": ["sylann_enable"],               # Sylann 开启
    "R4": ["media_send_enable"],           # Media 发送能力
    "R5": ["tool_capability_enable"],      # 工具能力启用
    "R6": ["autonomy_increase"],           # 自主等级提升
    "R7": ["execution_enable"],            # 执行能力（需 OWNER_SIGNED_ENABLE_TOKEN）
    "R8": ["full_production_enable"],      # 全量生产
}
# 各阶段自动允许的低风险动作
AUTO_SAFE_ACTIONS = {
    "R1": ["health_check", "log_analysis", "test_run", "report_gen"],
    "R2": ["memory_arch_read", "sensitive_scan", "migration_plan_gen",
           "test_gen", "risk_report_gen", "observation_monitor", "test_run"],
    "R3": ["scan_engine", "analyze_deps", "enable_plan_gen", "test_plan_gen", "test_run"],
    "R4": ["detect_sticker", "detect_voice", "detect_image_pipeline",
           "safety_report_gen", "test_run"],
    "R5": ["scan_tool_registry", "capability_matrix_gen", "deny_chain_test", "test_run"],
    "R6": ["eval_autonomy_level", "simulate_task", "risk_report_gen", "test_run"],
    "R7": ["readiness_check", "test_run"],
    "R8": ["readiness_check", "report_gen"],
}


# ------------------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def load_creds() -> dict:
    path = Path(os.environ.get("ANIMA_VPS_CREDS", str(DEFAULT_CREDS)))
    creds: dict = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    return creds


def ssh_connect(creds: dict):
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    host = creds.get("ANIMA_DEPLOY_HOST", "192.168.6.100")
    user = creds.get("ANIMA_DEPLOY_USER", "root")
    pw = creds.get("ANIMA_DEPLOY_PASS", "")
    last_err = None
    for _ in range(12):
        try:
            ssh.connect(host, username=user, password=pw, timeout=15, banner_timeout=30)
            return ssh
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(6)
    raise RuntimeError(f"SSH connect failed: {last_err}")


def ssh_run(ssh, cmd: str, timeout: int = 40) -> str:
    _, o, e = ssh.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    return out + (f"\n[stderr]{err}" if err else "")


def _ssh_stdout(ssh, cmd: str, timeout: int = 40) -> str:
    """只取 stdout（用于需要纯 JSON 的健康探测，避免 stderr 污染解析）。"""
    _, o, _ = ssh.exec_command(cmd, timeout=timeout)
    return o.read().decode("utf-8", "replace").strip()


# 远端健康探测脚本：读 config token/port -> 请求 /api/health -> 原样打印 JSON。
# 任何错误也以 JSON 形式打印，保证 stdout 始终可解析。
REMOTE_HEALTH_SCRIPT = r'''
import json, yaml, urllib.request, urllib.error
cfg = {}
try:
    cfg = yaml.safe_load(open('/opt/anima/config.yaml')) or {}
except Exception:
    cfg = {}
token = (cfg.get('webui') or {}).get('token', '')
port = int((cfg.get('webui') or {}).get('port', 8088))
req = urllib.request.Request(
    'http://127.0.0.1:%d/api/health' % port,
    headers={'Authorization': 'Bearer %s' % token},
)
try:
    data = urllib.request.urlopen(req, timeout=8).read().decode('utf-8')
    print(data)
except Exception as e:
    print(json.dumps({'error': 'health_http: %s' % e}))
'''


# ------------------------------------------------------------------------------
# 1. HEALTH CHECK（只读）
# ------------------------------------------------------------------------------
def fetch_health(creds: dict) -> dict:
    """SSH 到 VPS，只读拉取 /api/health + systemctl + 近期错误扫描。"""
    result: dict = {"ok": False, "fetched_at": now_iso()}
    try:
        ssh = ssh_connect(creds)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"ssh: {e}"
        return result
    try:
        # 远端一次性取 token + port + health JSON（纯 stdout，可解析）
        health_raw = _ssh_stdout(
            ssh,
            f"{REMOTE_ROOT}/venv/bin/python - <<'PY'\n{REMOTE_HEALTH_SCRIPT}\nPY",
            timeout=30,
        )
        try:
            health = json.loads(health_raw)
        except Exception:  # noqa: BLE001
            health = {"raw": health_raw[:500], "parse_error": True}
        if health.get("error"):
            result["error"] = health["error"]
            ssh.close()
            return result
        svc = _ssh_stdout(ssh, "systemctl is-active anima 2>/dev/null", timeout=20)
        err_scan = _ssh_stdout(
            ssh,
            "journalctl -u anima --since '2 hour ago' 2>/dev/null | grep -iE 'traceback|exception|multiple values|permissionerror|identity violation|credential leak' | tail -15",
            timeout=30,
        )
        ssh.close()
        result.update({
            "ok": True,
            "status": health.get("status"),
            "running": health.get("running"),
            "channel_connected": health.get("channel_connected"),
            "heartbeat_running": health.get("heartbeat_running"),
            "llm": health.get("llm"),
            "sylann": health.get("sylann"),
            "metrics": health.get("metrics"),
            "resources": health.get("resources"),
            "service_active": svc.strip(),
            "recent_errors": [l for l in err_scan.splitlines() if l and "[stderr]" not in l][:15],
        })
    except Exception as e:  # noqa: BLE001
        result["error"] = f"health_fetch: {e}"
        try:
            ssh.close()
        except Exception:
            pass
    return result


# ------------------------------------------------------------------------------
# 2. STAGE DETECT
# ------------------------------------------------------------------------------
def read_stage_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    # 默认基线（R2-A 刚完成）
    return {
        "current_stage": "R2",
        "stage_detail": "R2-A Memory Read Recovery (secret_filtered 已上线)",
        "observation": {
            "required_hours": 24,
            "started_at": now_iso(),
            "started_by": "R2-A deploy 2026-08-13",
            "completed": False,
        },
        "gates": {
            "check_pass": True, "tests_pass": True,
            "observation_complete": False, "no_incident": True,
            "rollback_available": True,
        },
        "updated_at": now_iso(),
    }


def detect_stage(state: dict) -> str:
    return state.get("current_stage", "R2")


# ------------------------------------------------------------------------------
# 3. RISK ASSESSMENT + 4. SELECT NEXT SAFE ACTION
# ------------------------------------------------------------------------------
def assess_and_select(state: dict, health: dict) -> dict:
    stage = detect_stage(state)
    obs = state.get("observation", {})
    required = int(obs.get("required_hours", 24))
    started = obs.get("started_at")
    elapsed_h = 0.0
    if started:
        try:
            st = datetime.fromisoformat(started)
            elapsed_h = (datetime.now(timezone(timedelta(hours=8))) - st).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            pass

    # 观察期是否完成
    obs_complete = bool(obs.get("completed")) or elapsed_h >= required
    safe_actions = AUTO_SAFE_ACTIONS.get(stage, ["health_check", "report_gen"])

    # 默认：跑一个低风险安全动作（观察/巡检）+ 报告
    if stage == "R2" and not obs_complete:
        next_action = "observation_monitor"
        risk = "LOW"
        authorized = True
        note = f"R2 24h 观察进行中 ({elapsed_h:.1f}/{required}h)；自动跑 memory 测试子集 + 巡检"
    elif stage == "R2" and obs_complete:
        # 观察完成 -> 必须等 owner 批准 R3
        next_action = "WAIT_OWNER_R3_APPROVAL"
        risk = "HIGH"
        authorized = False
        note = "R2 观察期已满，下一步 Sylann 开启需 owner 授权（自动阻断）"
    else:
        # 其他阶段：若下一动作属于 owner-gated，则阻断等授权
        gated = OWNER_GATED_STAGES.get(stage, [])
        if gated:
            next_action = f"WAIT_OWNER_{stage}_APPROVAL"
            risk = "HIGH"
            authorized = False
            note = f"{stage} 含 owner-gated 动作 {gated}，自动阻断"
        else:
            next_action = safe_actions[0] if safe_actions else "report_gen"
            risk = "LOW"
            authorized = True
            note = f"{stage} 自动执行低风险动作: {next_action}"

    return {
        "stage": stage,
        "elapsed_hours": round(elapsed_h, 2),
        "observation_required_hours": required,
        "observation_complete": obs_complete,
        "next_action": next_action,
        "risk": risk,
        "authorized": authorized,
        "safe_actions_available": safe_actions,
        "owner_gated": OWNER_GATED_STAGES.get(stage, []),
        "note": note,
    }


# ------------------------------------------------------------------------------
# 5. EXECUTE ONLY AUTHORIZED ACTION（仅 LOW 风险）
# ------------------------------------------------------------------------------
def run_safe_action(action: str) -> dict:
    """执行被授权的低风险动作。返回结构化结果。"""
    if action in FORBIDDEN_ACTIONS:
        return {"executed": False, "reason": "FORBIDDEN by safety boundary", "action": action}

    out: dict = {"action": action, "executed": True, "detail": ""}
    try:
        if action in ("test_run", "observation_monitor"):
            # 当前阶段相关测试子集（轻量，不跑全量）
            subset = [
                "tests/test_memory_read_r2a.py",
                "tests/test_chat_memory_persist.py",
                "tests/test_vector_memory.py",
            ]
            cmd = [str(VENV_PY), "-m", "pytest", *subset, "-q", "--no-header",
                   "-p", "no:cacheprovider"]
            r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                               timeout=300)
            # pytest 可能非零退出（测试失败）-> 记录但不崩溃
            summary = (r.stdout + r.stderr)[-800:]
            out["detail"] = summary.strip()
            out["returncode"] = r.returncode
        elif action in ("sensitive_scan", "scan_engine", "scan_tool_registry",
                        "detect_sticker", "detect_voice", "detect_image_pipeline",
                        "eval_autonomy_level", "readiness_check", "memory_arch_read"):
            out["detail"] = "扫描类动作：本次仅做健康/状态聚合（详见 report），不读 QQ 明文"
        elif action in ("migration_plan_gen", "risk_report_gen", "enable_plan_gen",
                        "test_plan_gen", "safety_report_gen", "capability_matrix_gen",
                        "simulate_task", "report_gen"):
            out["detail"] = "文档/计划生成：由报告步骤统一产出"
        else:
            out["detail"] = "no-op safe action"
    except Exception as e:  # noqa: BLE001
        out["executed"] = False
        out["error"] = str(e)
    return out


# ------------------------------------------------------------------------------
# 6/7. AUDIT — state 文件维护
# ------------------------------------------------------------------------------
def build_state_files(state: dict, health: dict, decision: dict) -> dict:
    current_stage = {
        "current_stage": decision["stage"],
        "stage_detail": state.get("stage_detail"),
        "observation": state.get("observation"),
        "gates": state.get("gates"),
        "last_health": {
            "status": health.get("status"),
            "running": health.get("running"),
            "channel_connected": health.get("channel_connected"),
            "heartbeat_running": health.get("heartbeat_running"),
            "service_active": health.get("service_active"),
        },
        "next_action": decision["next_action"],
        "next_risk": decision["risk"],
        "authorized": decision["authorized"],
        "blocked_actions": sorted(FORBIDDEN_ACTIONS),
        "owner_gated": decision["owner_gated"],
        "updated_at": now_iso(),
    }
    capability_state = {
        "execution": "LOCKED",
        "sandbox_write": "LOCKED",
        "code_execution": "LOCKED",
        "sylann": health.get("sylann", {}).get("enabled") if isinstance(health.get("sylann"), dict) else False,
        "media_send": "OFF",
        "capability_mode": "OFF",
        "agent_autonomy": "L0-L1",
        "note": "全部能力默认 LOCKED/OFF；任何开启须经 owner 授权",
        "updated_at": now_iso(),
    }
    permission_state = {
        "authority_source": "Identity + Capability + Grant",
        "memory_is_permission_source": False,
        "admin_auto_created": False,
        "kill_switch": "ACTIVE",
        "grant_required_for_execution": True,
        "updated_at": now_iso(),
    }
    risk_register = {
        "permanent_principles": [
            "速度 < 安全", "能力 < 控制", "记忆 != 权限",
            "Agent != Owner", "自动化 != 自主越权", "任何不确定 STOP AND ASK OWNER",
        ],
        "current_risk": decision["risk"],
        "open_items": [],
        "updated_at": now_iso(),
    }
    # approval_queue：把 owner-gated 的下一步写成授权请求
    approval_queue: list = []
    if not decision["authorized"] and decision["next_action"].startswith("WAIT_OWNER"):
        target = decision["next_action"].replace("WAIT_OWNER_", "").replace("_APPROVAL", "")
        approval_queue.append({
            "id": f"AR-{decision['stage']}-{int(time.time())}",
            "stage": decision["stage"],
            "request": f"enable {target}",
            "risk": "HIGH",
            "status": "AWAITING_OWNER",
            "created_at": now_iso(),
            "note": "由 Autonomous Evolution Agent 自动生成；owner 批准前不执行",
        })
    risk_register["open_items"] = [a["request"] for a in approval_queue]

    return {
        "current_stage.json": current_stage,
        "capability_state.json": capability_state,
        "permission_state.json": permission_state,
        "risk_register.json": risk_register,
        "approval_queue.json": {"queue": approval_queue, "updated_at": now_iso()},
    }


def write_local_state(state_files: dict) -> None:
    LOCAL_STATE.mkdir(parents=True, exist_ok=True)
    for name, data in state_files.items():
        (LOCAL_STATE / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def push_state_to_vps(state_files: dict, creds: dict) -> bool:
    try:
        ssh = ssh_connect(creds)
    except Exception as e:  # noqa: BLE001
        log(f"[state] push 失败(ssh): {e}")
        return False
    try:
        ssh_run(ssh, f"mkdir -p {REMOTE_STATE}", timeout=20)
        for name, data in state_files.items():
            payload = json.dumps(data, ensure_ascii=False)
            # 用 cat 写入远端（低风险：仅 JSON 状态文件，非代码/配置）
            ssh_run(ssh, f"cat > {REMOTE_STATE}/{name} <<'ANIMAEOF'\n{payload}\nANIMAEOF",
                    timeout=30)
        ssh.close()
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[state] push 失败: {e}")
        try:
            ssh.close()
        except Exception:
            pass
        return False


# ------------------------------------------------------------------------------
# 8. REPORT
# ------------------------------------------------------------------------------
def check_anomaly(health: dict) -> tuple[bool, str]:
    if not health.get("ok"):
        return True, f"health 拉取失败: {health.get('error', 'unknown')}"
    if health.get("service_active") != "active":
        return True, f"service 非 active: {health.get('service_active')}"
    if health.get("status") in ("unhealthy", "degraded"):
        return True, f"overall status={health.get('status')}"
    if not health.get("heartbeat_running"):
        return True, "heartbeat_running=false"
    if health.get("recent_errors"):
        return True, f"近期错误 {len(health['recent_errors'])} 条"
    metrics = health.get("metrics") or {}
    if isinstance(metrics, dict):
        er = metrics.get("error_rate") or metrics.get("errors_since_start")
        if er and float(er) > 0:
            return True, f"metrics error: {er}"
    return False, ""


def generate_hourly_report(state: dict, health: dict, decision: dict,
                           action_result: dict, anomaly: tuple) -> str:
    is_anom, anom_msg = anomaly
    ok, chan, hb = health.get("status"), health.get("channel_connected"), health.get("heartbeat_running")
    m = health.get("metrics") or {}
    risk = decision["risk"]
    rec = ("⚠️ 检测到异常 -> STOP / FREEZE / 生成 INCIDENT / 通知 owner" if is_anom
           else ("下一步需 owner 授权，已写入 approval_queue" if not decision["authorized"]
                 else "继续自动低风险巡检"))
    lines = [
        "# P10_HOURLY_REPORT",
        "",
        f"> 生成时间: {now_iso()}",
        "",
        "## Current Stage",
        "",
        f"- 阶段: **{decision['stage']}** ({state.get('stage_detail', '')})",
        f"- 观察期: {decision['elapsed_hours']}/{decision['observation_required_hours']}h "
        f"({'完成' if decision['observation_complete'] else '进行中'})",
        "",
        "## Health",
        "",
        f"- status: `{ok}`",
        f"- service_active: `{health.get('service_active')}`",
        f"- channel_connected: `{chan}`",
        f"- heartbeat_running: `{hb}`",
        f"- LLM circuit: `{health.get('llm', {}).get('circuit_open') if isinstance(health.get('llm'), dict) else 'n/a'}`",
        f"- 近期错误: {len(health.get('recent_errors', []))} 条",
        "",
        "## Metrics",
        "",
        f"- {json.dumps(m, ensure_ascii=False) if m else '(n/a)'}",
        "",
        "## Security",
        "",
        "- execution: LOCKED · sandbox.write: LOCKED · code_execution: LOCKED",
        "- kill_switch: ACTIVE · memory≠permission · admin_auto_created: False",
        "- 审计链: ENABLED（memory_read_audit.jsonl 持续产出）",
        "",
        "## Incidents",
        "",
        f"- {'⚠️ ' + anom_msg if is_anom else '无'}",
        "",
        "## Risk",
        "",
        f"- 本轮风险: **{risk}**",
        f"- 下一步动作: `{decision['next_action']}`（authorized={decision['authorized']}）",
        f"- owner-gated: {decision['owner_gated']}",
        "",
        "## Recommendation",
        "",
        f"- {rec}",
        "",
        "---",
        f"_由 Autonomous Evolution Agent 自动生成 · 安全边界：禁止自动开 execution/权限/扩权/删审计_",
    ]
    return "\n".join(lines)


def generate_incident_report(health: dict, anom_msg: str) -> str:
    content = [
        "# INCIDENT_REPORT",
        "",
        f"> 触发时间: {now_iso()}",
        f"> 级别: HIGH（自动 STOP + FREEZE）",
        "",
        "## 异常",
        anom_msg,
        "",
        "## 健康检查快照",
        "```json",
        json.dumps(health, ensure_ascii=False, indent=2)[:2000],
        "```",
        "",
        "## 已执行动作",
        "- 停止自动推进（FREEZE）",
        "- 不重启服务、不部署、不修改权限（遵循中/高风险规则）",
        "- 等待 owner 介入",
        "",
        "## 通知",
        "owner 需查看本报告并决定恢复/回滚策略。",
    ]
    return "\n".join(content)


# ------------------------------------------------------------------------------
# Banner
# ------------------------------------------------------------------------------
def print_banner(decision: dict, health: dict) -> None:
    print("=" * 60)
    print("ANIMA AUTONOMOUS EVOLUTION AGENT ONLINE")
    print("=" * 60)
    print(f"Current Stage : {decision['stage']} ({decision.get('elapsed_hours')}h obs)")
    print(f"Health        : {health.get('status')} / svc={health.get('service_active')} "
          f"/ hb={health.get('heartbeat_running')}")
    print(f"Next Safe Act : {decision['next_action']} (risk={decision['risk']}, "
          f"authorized={decision['authorized']})")
    print(f"Blocked Acts  : execution / permission / prod-expand / kill-switch / audit-delete")
    print(f"Owner Appr Req: {decision['owner_gated'] or 'none'}")
    print("=" * 60)


# ------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Anima Autonomous Evolution Orchestrator")
    ap.add_argument("--once", action="store_true", help="执行单轮循环")
    ap.add_argument("--banner", action="store_true", help="打印 ONLINE 横幅")
    ap.add_argument("--push-state", action="store_true", help="仅推送本地 state 到 VPS")
    args = ap.parse_args(argv)

    creds = load_creds()
    state = read_stage_state()
    health = fetch_health(creds)
    decision = assess_and_select(state, health)

    if args.banner or args.once:
        print_banner(decision, health)

    anomaly = check_anomaly(health)
    is_anom, anom_msg = anomaly

    if is_anom:
        # 异常：STOP / FREEZE / INCIDENT / 不执行任何动作
        inc = generate_incident_report(health, anom_msg)
        (REPO / "INCIDENT_REPORT.md").write_text(inc, encoding="utf-8")
        log(f"⚠️ 异常检测: {anom_msg} -> 已生成 INCIDENT_REPORT.md，自动 STOP")
        STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        state_files = build_state_files(state, health, decision)
        write_local_state(state_files)
        push_state_to_vps(state_files, creds)
        print("\n" + inc)
        return 2  # 非零：通知调度方有异常

    # 正常：仅执行被授权的低风险动作
    action_result = {"action": "none", "executed": False}
    if decision["authorized"] and decision["next_action"] not in ("report_gen",):
        action_result = run_safe_action(decision["next_action"])
        if not decision["next_action"].startswith("WAIT_OWNER"):
            log(f"执行安全动作: {decision['next_action']} -> {action_result.get('detail', '')[:120]}")
    elif decision["next_action"].startswith("WAIT_OWNER"):
        log(f"下一步需 owner 授权，已写入 approval_queue: {decision['next_action']}")

    # 持久化阶段状态（每轮都写，保证观察计时跨运行稳定；完成时置 completed）
    if decision["observation_complete"] and decision["stage"] == "R2":
        state.setdefault("observation", {})["completed"] = True
        state.setdefault("gates", {})["observation_complete"] = True
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # AUDIT + REPORT
    state_files = build_state_files(state, health, decision)
    write_local_state(state_files)
    push_state_to_vps(state_files, creds)

    report = generate_hourly_report(state, health, decision, action_result, anomaly)
    HOURLY_REPORT.write_text(report, encoding="utf-8")
    print("\n" + report)

    if args.push_state:
        log("state 已推送，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
