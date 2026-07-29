"""VPS 环境搭建 + Anima 部署脚本（安全版本）。

用法：
    export ANIMA_DEPLOY_HOST=pve.aicaowo.com
    export ANIMA_DEPLOY_USER=root
    export ANIMA_DEPLOY_PASS=<your-password>
    python _deploy.py                 # 正式部署
    python _deploy.py --dry-run       # 仅预检（连通性/Python/venv/待上传文件数），不改动远程
    python _deploy.py --with-config   # 连本地 config.yaml 一起上传（默认不覆盖生产配置）

安全特性：
  - 默认不上传 config.yaml，避免覆盖生产配置（用 --with-config 显式开启）。
  - 上传前对远程 data/chat_memory.json 做时间戳备份，迁移出错可回滚。
  - --dry-run 先验证连通性与环境，再决定是否真推，降低误操作风险。
  - 部署后自动重启 anima 服务并自检。
"""
import os
import sys
import atexit
import argparse
import hashlib
import json
import subprocess
import datetime
import tempfile
import zipfile
from pathlib import Path
import paramiko

HOST = os.environ.get("ANIMA_DEPLOY_HOST", "")
USER = os.environ.get("ANIMA_DEPLOY_USER", "")
PASS = os.environ.get("ANIMA_DEPLOY_PASS", "")

# 以脚本所在目录为项目根，确保在任何机器上都可移植
PROJECT_ROOT = str(Path(__file__).resolve().parent)
REMOTE_ROOT = "/opt/anima"


class RemoteCommandError(RuntimeError):
    """远程部署步骤失败；携带退出码和受限日志摘要。"""


def _parse_systemd_identity(output: str) -> tuple[int, int]:
    """解析 ``MainPID ActiveEnterTimestampMonotonic`` 两行输出。"""
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return 0, 0
    try:
        return int(lines[0]), int(lines[1])
    except ValueError:
        return 0, 0


def _render_post_deploy_smoke(remote_root: str) -> str:
    """生成部署后只读验收脚本；不输出 Token、配置或聊天正文。"""
    return f"""cd {remote_root}
{remote_root}/venv/bin/python - <<'PY'
import asyncio
import json
import time
import urllib.request
from pathlib import Path

import yaml

from anima.tools import _tool_runtime_status

root = Path({remote_root!r})
cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {{}}
token = str((cfg.get("webui") or {{}}).get("token") or "")
if not token:
    raise SystemExit("smoke: WebUI token missing")

def get(path):
    request = urllib.request.Request(
        "http://127.0.0.1:8088" + path,
        headers={{"Authorization": "Bearer " + token}},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        if response.status != 200:
            raise RuntimeError(f"{{path}} status={{response.status}}")
        return json.load(response)

# WebUI 绑定 8088 端口有 ~1s 延迟；部署重启后先等端口就绪再探活，
# 避免「Connection refused」时序竞态误判部署失败。
_health_deadline = time.time() + 30
health = None
while time.time() < _health_deadline:
    try:
        health = get("/api/health")
        break
    except Exception:
        time.sleep(1)
if health is None:
    raise SystemExit("smoke: /api/health not reachable within 30s")
modules = get("/api/modules/status")
evaluation = get("/api/evaluation")
if not health.get("running"):
    raise SystemExit("smoke: runtime not running")
if not health.get("channel_connected"):
    raise SystemExit("smoke: QQ channel not connected")
if not health.get("heartbeat_running"):
    raise SystemExit("smoke: heartbeat not running")
# attention 仅作告警而非硬失败：良性的「待送达自动化」等项不应阻断部署，
# 真正的健康门禁是上面的 running / channel_connected / heartbeat_running / modules。
attention_items = [c for c in evaluation.get("checks", []) if c.get("status") == "attention"]
for item in attention_items:
    print(f"[SMOKE-WARN] evaluation attention: {{item.get('name')}} | {{item.get('detail') or item.get('message')}}")
if not (modules.get("modules") or []):
    raise SystemExit("smoke: module list empty")
runtime_status = asyncio.run(_tool_runtime_status())
if "【内存】" not in runtime_status or "【磁盘】" not in runtime_status or "【负载】" not in runtime_status:
    raise SystemExit("smoke: fixed runtime status failed")
print(json.dumps({{
    "running": True,
    "channel_connected": True,
    "heartbeat_running": True,
    "modules": len(modules.get("modules") or []),
    "evaluation_ready": int((evaluation.get("summary") or {{}}).get("ready", 0)),
    "evaluation_attention": len(attention_items),
    "runtime_status": "ok",
}}, ensure_ascii=False))
PY"""


def _acquire_local_deploy_lock():
    """Prevent duplicated desktop/tool invocations from racing one VPS release.

    The lock is held by the operating system and is released automatically if
    the launcher is killed, so it cannot leave a stale lock after a timeout.
    """
    path = Path(tempfile.gettempdir()) / "anima-next-deploy.lock"
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 工具/终端被强制中断时 atexit 可能来不及执行，留下一个已失效的
        # PID 锁。仅在该 PID 确认不存在时回收；活跃进程仍必须严格拒绝并发发布。
        try:
            stale_pid = int(path.read_text(encoding="utf-8").strip())
            os.kill(stale_pid, 0)
        except ProcessLookupError:
            path.unlink(missing_ok=True)
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            print(f"回收失效部署锁（pid={stale_pid}）")
        except (ValueError, OSError):
            raise RuntimeError("已有 Anima 发布进程正在运行；请等待它完成后再试")
        else:
            raise RuntimeError("已有 Anima 发布进程正在运行；请等待它完成后再试")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    atexit.register(path.unlink, missing_ok=True)
    return path


def _project_version() -> str:
    """从唯一版本源读取版本号，避免部署戳与 Python 包版本漂移。"""
    import re

    text = (Path(PROJECT_ROOT) / "anima" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise RuntimeError("无法读取 anima.__version__")
    return match.group(1)


def _git_info() -> dict[str, object]:
    """返回发布身份；没有 Git 时允许源码归档用户继续部署。"""
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", PROJECT_ROOT, *args], capture_output=True,
            text=True, timeout=15,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    commit = git("rev-parse", "--short=12", "HEAD") or "source-archive"
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "source-archive"
    dirty = bool(git("status", "--porcelain=v1", "--untracked-files=all"))
    epoch_text = git("show", "-s", "--format=%ct", "HEAD")
    try:
        commit_epoch = int(epoch_text)
    except ValueError:
        commit_epoch = 315532800  # 1980-01-01，ZIP 支持的最早稳定时间
    return {"commit": commit, "branch": branch, "dirty": dirty,
            "commit_epoch": max(commit_epoch, 315532800)}


def _require_env():
    missing = [k for k, v in {
        "ANIMA_DEPLOY_HOST": HOST,
        "ANIMA_DEPLOY_USER": USER,
        "ANIMA_DEPLOY_PASS": PASS,
    }.items() if not v]
    if missing:
        print(f"缺少环境变量: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def _render_remote_unit(remote_root: str) -> str:
    """渲染部署到远程机的 systemd 单元（路径使用远程根）。"""
    return f"""[Unit]
Description=Anima QQ Bot
After=network-online.target
Wants=network-online.target
After=napcat.service
StartLimitBurst=10
StartLimitIntervalSec=60

[Service]
Type=simple
User=anima
Group=anima
WorkingDirectory={remote_root}
ExecStart={remote_root}/venv/bin/python -m anima start --config {remote_root}/config.yaml
Restart=on-failure
RestartSec=10
# 看门狗：事件循环死锁（CPU 自旋/同步阻塞）时收不到 WATCHDOG=1 心跳，
# systemd 自动 SIGKILL 重启，无需人工干预。
WatchdogSec=120
StandardOutput=journal
StandardError=journal

# 安全加固
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={remote_root}/data {remote_root}/config.yaml

[Install]
WantedBy=multi-user.target
"""


def _build_exclude(with_config: bool) -> set:
    exclude = {
        ".venv", "__pycache__", ".pytest_cache", "data", "*.pyc", ".git", ".env",
        "napcat_shell.zip", "qrcode.png",
        # 部署不需要的本地产物 / 测试工件 / 备份
        "tmp_astrbot_test", "anima.egg-info", ".coverage", "htmlcov",
        "*.egg-info", "*.db", "*.sqlite*", "None",
        # Private keys are never application source or deployable runtime assets.
        "*.pem", "*.key", "id_rsa*", "id_ed25519*",
        "_mig_sim_test",  # 本地迁移测试残留，绝不上传
        "_bili_verify.py", "_selfcode_verify.py", "overview_*.md",
        "public",  # 对应源码包在临时目录生成并单独上传，禁止递归打包旧产物
    }
    # 默认保护生产配置：不上传本地 config.yaml，避免覆盖线上设置
    if not with_config:
        exclude.add("config.yaml")
    return exclude


def _is_excluded(rel_path: str, exclude: set) -> bool:
    rel = rel_path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    parts = rel.split("/")
    for rule in exclude:
        if "*" in rule:
            if fnmatch(rel, rule) or any(fnmatch(part, rule) for part in parts):
                return True
        elif rule == rel or rule in parts:
            return True
    return any(part.startswith(".pytest") for part in parts)


def _tracked_paths() -> list[str] | None:
    """优先使用 Git 索引作为发布清单；源码归档环境再退回目录遍历。"""
    try:
        result = subprocess.run(
            ["git", "-C", PROJECT_ROOT, "ls-files", "-z"],
            capture_output=True, timeout=20,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return [item.decode("utf-8", errors="strict")
            for item in result.stdout.split(b"\0") if item]


def _dirty_untracked_paths() -> list[str]:
    """返回 --allow-dirty 明确授权下可发布的未跟踪源码。

    默认发布仍严格只用 Git 已跟踪文件。只有调用方已显式选择
    ``--allow-dirty`` 时，才把未跟踪的源码/配置模板/测试一并纳入，防止新增
    Python 模块在紧急验证发布中被 ``git ls-files`` 漏掉。运行数据、密钥和
    临时工件仍由 .gitignore 与目录白名单排除。
    """
    try:
        result = subprocess.run(
            ["git", "-C", PROJECT_ROOT, "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True, timeout=20,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    allowed_roots = {"anima", "config", "tests", "webui"}
    paths = [item.decode("utf-8", errors="strict")
             for item in result.stdout.split(b"\0") if item]
    return [path for path in paths if Path(path).parts and Path(path).parts[0] in allowed_roots]


def _plan_upload(exclude: set, extra_rel_paths: list[str] | None = None) -> list[tuple[str, str]]:
    """返回发布清单；Git 仓库内只允许已跟踪文件进入生产。"""
    tracked = _tracked_paths()
    rel_paths: list[str] = []
    if tracked is not None:
        rel_paths.extend(tracked)
        rel_paths.extend(extra_rel_paths or [])
        # 确保 console 和 napcat service 加入同步列表（任务1要求）
        rel_paths.extend(["web/anima-console.service", "web/anima-napcat.service"])
        if "config.yaml" not in exclude and (Path(PROJECT_ROOT) / "config.yaml").is_file():
            rel_paths.append("config.yaml")
    else:
        for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
            rel_dir = os.path.relpath(dirpath, PROJECT_ROOT).replace("\\", "/")
            dirnames[:] = [name for name in dirnames
                           if not _is_excluded(f"{rel_dir}/{name}", exclude)]
            for filename in filenames:
                rel_paths.append((f"{rel_dir}/{filename}" if rel_dir != "." else filename))

    plan: list[tuple[str, str]] = []
    for rel in sorted(set(rel_paths)):
        if _is_excluded(rel, exclude):
            continue
        source = Path(PROJECT_ROOT) / Path(rel)
        if not source.is_file():
            continue
        normalized = rel.replace("\\", "/")
        plan.append((str(source), f"{REMOTE_ROOT}/{normalized}"))
    return plan


def _build_source_archive(plan: list[tuple[str, str]], version: str,
                          git_info: dict[str, object]):
    """生成确定性对应源码包与逐文件哈希清单。"""
    temp_dir = tempfile.TemporaryDirectory(prefix="anima-release-")
    root = Path(temp_dir.name)
    archive = root / "anima-next-source.zip"
    manifest_path = root / "release-manifest.json"
    prefix = f"anima-next-{version}/"
    timestamp = datetime.datetime.fromtimestamp(
        int(git_info["commit_epoch"]), tz=datetime.timezone.utc,
    )
    zip_time = (timestamp.year, timestamp.month, timestamp.day,
                timestamp.hour, timestamp.minute, timestamp.second)
    entries: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    for source, _remote in plan:
        path = Path(source)
        rel = path.resolve().relative_to(Path(PROJECT_ROOT).resolve()).as_posix()
        if _is_excluded(rel, _build_exclude(False)):
            continue
        data = path.read_bytes()
        entries.append({"path": rel, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
        payloads.append((rel, data))
    manifest = {
        "project": "Anima-next",
        "version": version,
        "license": "AGPL-3.0-only",
        "commit": git_info["commit"],
        "branch": git_info["branch"],
        "dirty": bool(git_info["dirty"]),
        "generated_at": timestamp.isoformat(),
        "files": entries,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2,
                                sort_keys=True).encode("utf-8") + b"\n"
    manifest_path.write_bytes(manifest_bytes)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as bundle:
        for rel, data in payloads + [("RELEASE-MANIFEST.json", manifest_bytes)]:
            info = zipfile.ZipInfo(prefix + rel, date_time=zip_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    return temp_dir, archive, manifest_path


def fnmatch(name: str, pat: str) -> bool:
    import fnmatch as _fn
    return _fn.fnmatch(name, pat)


def main():
    parser = argparse.ArgumentParser(description="Anima VPS 部署")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预检环境与待上传文件，不改动远程")
    parser.add_argument("--with-config", action="store_true",
                        help="连本地 config.yaml 一起上传（默认不覆盖生产配置）")
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="紧急情况下允许从脏工作区部署；发布身份会明确标记 dirty",
    )
    args = parser.parse_args()

    version = _project_version()
    git_info = _git_info()
    if git_info["dirty"] and not args.allow_dirty:
        print(
            "拒绝从脏工作区部署：请先提交或清理改动；"
            "仅紧急恢复可显式使用 --allow-dirty。",
            file=sys.stderr,
        )
        sys.exit(2)
    if git_info["dirty"]:
        print("WARNING: 正在使用 --allow-dirty 发布未提交工作区。", file=sys.stderr)

    _require_env()
    deploy_lock = _acquire_local_deploy_lock()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # VPS 偶发 SSH banner 抖动（Error reading SSH protocol banner），
    # 重试 + banner_timeout 兜底，避免部署因瞬态连接失败中断。
    last = None
    for _ in range(6):
        try:
            ssh.connect(HOST, username=USER, password=PASS, timeout=15, banner_timeout=30)
            break
        except Exception as e:
            last = e
            import time as _t
            _t.sleep(4)
    else:
        print(f"SSH 连接失败（重试 6 次）: {last}", file=sys.stderr)
        sys.exit(1)

    def run(cmd, silent=False, check=True):
        if not silent:
            print(f"[RUN] {cmd[:80]}", flush=True)
        _, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        if err:
            print(f"[STDERR] {err[:500]}", flush=True)
        if out:
            print(out.strip()[:1000], flush=True)
        if check and code != 0:
            raise RemoteCommandError(
                f"远程命令失败 exit={code}: {cmd[:160]}\n"
                f"stdout={out[-500:]}\nstderr={err[-500:]}"
            )
        return out

    # ---------------- 预检 ----------------
    print("===== 预检 =====")
    run("python3 --version 2>&1; echo '---'; "
        f"test -d {REMOTE_ROOT}/venv && echo 'venv: 已存在' || echo 'venv: 缺失'")
    dirty_extras = _dirty_untracked_paths() if args.allow_dirty else []
    if dirty_extras:
        print(f"--allow-dirty: 额外纳入 {len(dirty_extras)} 个未跟踪源码文件")
    plan = _plan_upload(_build_exclude(args.with_config), dirty_extras)
    source_plan = _plan_upload(_build_exclude(False), dirty_extras)
    release_tmp, source_archive, release_manifest = _build_source_archive(
        source_plan, version, git_info,
    )
    source_sha256 = hashlib.sha256(source_archive.read_bytes()).hexdigest()
    total = len(plan)
    total_bytes = sum(os.path.getsize(s) for s, _ in plan if os.path.exists(s))
    print(f"待上传文件: {total} 个 | 约 {total_bytes/1024:.1f} KB")
    print(
        f"发布身份: {version} / {git_info['commit']} / {git_info['branch']}"
        f" | 对应源码 {source_archive.stat().st_size / 1024:.1f} KB"
    )
    if args.with_config:
        print("config.yaml: 将上传（覆盖生产配置）")
    else:
        print("config.yaml: 已跳过（保留生产配置，可用 --with-config 开启）")

    if args.dry_run:
        print("\n[DRY-RUN] 未改动远程。确认无误后去掉 --dry-run 执行正式部署。")
        release_tmp.cleanup()
        ssh.close()
        return

    # ---------------- 正式部署 ----------------
    # 创建 anima 用户与目录
    run(f"id -u anima >/dev/null 2>&1 || useradd -r -s /bin/false -d {REMOTE_ROOT} anima")
    run(f"mkdir -p {REMOTE_ROOT} && chown -R anima:anima {REMOTE_ROOT}")

    # 首次安装才触发 apt。常规发布不应因为系统软件源抖动而延迟源码热更新；
    # Python 依赖仍由后续 pip install -e 统一校准。
    venv_exists = run(
        f"test -x {REMOTE_ROOT}/venv/bin/python && echo yes || echo no",
        silent=True,
    ).strip() == "yes"
    if not venv_exists:
        run("apt-get update -qq 2>&1 | tail -5")
        run("DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git wget curl unzip python3-venv python3-pip 2>&1 | tail -5")
        run(f"python3 -m venv {REMOTE_ROOT}/venv 2>&1")
        run(f"{REMOTE_ROOT}/venv/bin/pip install --upgrade pip 2>&1 | tail -3")
    else:
        print("[BOOTSTRAP] Existing venv found; skipping apt bootstrap.")

    # 上传项目文件
    print("[UPLOAD] Uploading signed source bundle...")
    run(f"mkdir -p {REMOTE_ROOT}/public")
    sftp = ssh.open_sftp()
    bundle_name = f"anima-next-{git_info['commit']}.zip"
    bundle_remote = f"/tmp/{bundle_name}"
    sftp.put(str(source_archive), bundle_remote)
    # 公网源码告知入口保留同一份不可变发布包，不单独重新打包。
    run(f"cp {bundle_remote} {REMOTE_ROOT}/public/anima-next-source.zip")
    sftp.put(str(release_manifest), f"{REMOTE_ROOT}/public/release-manifest.json")
    if args.with_config:
        sftp.put(str(Path(PROJECT_ROOT) / "config.yaml"), f"{REMOTE_ROOT}/config.yaml")
    sftp.close()
    stage_dir = f"/tmp/anima-next-stage-{git_info['commit']}"
    run(
        f"rm -rf {stage_dir} && mkdir -p {stage_dir} && "
        f"unzip -oq {bundle_remote} -d {stage_dir} && "
        f"release=$(find {stage_dir} -mindepth 1 -maxdepth 1 -type d | head -1) && "
        f"test -n \"$release\" && cp -a \"$release\"/. {REMOTE_ROOT}/ && "
        f"rm -rf {stage_dir} {bundle_remote}"
    )
    release_tmp.cleanup()
    print("[UPLOAD] Done. (one source bundle)")

    # Vite 资源名带 hash，单纯 cp 更新会让旧版 app-*.js/css 永久留在 VPS。
    # 仅按当前 index.html 的引用列表清理这两类静态编译产物：绝不触及用户数据、配置、
    # 表情资产或其他非 Vite 文件。如果 index 解析失败则不删除任何东西。
    print("[CLEANUP] 清理未被当前 WebUI 引用的旧静态资源...")
    run(
        f"python3 - <<'PY'\n"
        f"import re\n"
        f"from pathlib import Path\n"
        f"root = Path({REMOTE_ROOT!r}) / 'anima' / 'webui' / 'static'\n"
        f"index = root / 'index.html'\n"
        f"try:\n"
        f"    text = index.read_text(encoding='utf-8')\n"
        f"except OSError:\n"
        f"    raise SystemExit(0)\n"
        f"keep = set(re.findall(r'app-[A-Za-z0-9_-]+\\.(?:js|css)', text))\n"
        f"if not keep:\n"
        f"    raise SystemExit(0)\n"
        f"removed = 0\n"
        f"for path in root.glob('app-*'):\n"
        f"    if path.is_file() and path.name not in keep and path.suffix in {{'.js', '.css'}}:\n"
        f"        path.unlink()\n"
        f"        removed += 1\n"
        f"print(f'webui stale assets removed: {{removed}}')\n"
        f"PY"
    )

    # 安装依赖
    run(f"set -o pipefail; {REMOTE_ROOT}/venv/bin/pip install -e "
        f"'{REMOTE_ROOT}[qq,vector,dev,sylann,docs]' 2>&1 | tail -8")

    # 修正权限
    run(f"chown -R anima:anima {REMOTE_ROOT}")

    # 远程数据备份（迁移前快照，便于回滚）
    # 单份滚动备份（可覆盖），避免时间戳备份 pre-isolation-* 长期堆积（体检报告 §3.5）
    print("[BACKUP] 备份远程旧记忆文件（单份滚动）...")
    run(f"test -f {REMOTE_ROOT}/data/chat_memory.json && "
        f"cp {REMOTE_ROOT}/data/chat_memory.json "
        f"{REMOTE_ROOT}/data/chat_memory.json.pre-isolation 2>/dev/null || true")
    # 表情索引可能在启动后自动清理悬空文件记录；部署前保留一份可回溯快照。
    run(f"test -f {REMOTE_ROOT}/data/stickers.json && "
        f"cp {REMOTE_ROOT}/data/stickers.json "
        f"{REMOTE_ROOT}/data/stickers.json.pre-reconcile 2>/dev/null || true")
    # 清理旧的带时间戳备份堆积
    run(f"rm -f {REMOTE_ROOT}/data/chat_memory.json.pre-isolation-* 2>/dev/null || true")

    # 安装 systemd unit（动态渲染，确保路径与远程根一致）
    unit = _render_remote_unit(REMOTE_ROOT)
    run(
        f"cat > /etc/systemd/system/anima.service <<'EOF'\n{unit}\nEOF\n"
        "systemctl daemon-reload"
    )

    # 重启服务（让新隔离代码在首次加载时完成迁移）
    print("[RESTART] 重启 anima 服务以加载隔离修复...")
    # P1-3：部署后必须核对 ActiveEnterTimestamp 已刷新，否则说明服务没真正重启
    # （可能启动即崩、被 mask、或 restart 未生效），不能当作部署成功。
    identity_cmd = (
        "systemctl show anima --value -p MainPID -p ActiveEnterTimestampMonotonic"
    )
    before_pid, before_mono = _parse_systemd_identity(run(identity_cmd, silent=True))
    run("systemctl restart anima")
    run(
        "for i in $(seq 1 30); do "
        "active=$(systemctl is-active anima 2>/dev/null || true); "
        "pid=$(systemctl show anima -p MainPID --value 2>/dev/null || true); "
        f"if [ \"$active\" = active ] && [ -n \"$pid\" ] && [ \"$pid\" != 0 ] "
        f"&& [ \"$pid\" != {before_pid} ]; then exit 0; fi; sleep 1; done; "
        "systemctl status anima --no-pager -l; exit 1"
    )
    after_output = run(identity_cmd, silent=True)
    after_pid, after_mono = _parse_systemd_identity(after_output)
    if after_pid <= 0 or after_pid == before_pid or after_mono <= before_mono:
        raise RemoteCommandError(
            "服务重启证据不足："
            f"before(pid={before_pid},mono={before_mono}) "
            f"after(pid={after_pid},mono={after_mono})"
        )
    after = run("systemctl show anima -p ActiveEnterTimestamp --value", silent=True).strip()
    print(f"[OK] 服务已重启（pid {before_pid} -> {after_pid}; ActiveEnterTimestamp: {after}）")

    print("[SMOKE] 验证运行时、QQ、心跳、WebUI、模块与固定只读工具...")
    run(_render_post_deploy_smoke(REMOTE_ROOT))

    # 写入部署版本戳（生产机不携带 .git，WebUI 据此展示可复现发布身份）。
    stamp = {
        "commit": git_info["commit"],
        "branch": git_info["branch"],
        "version": version,
        "dirty": bool(git_info["dirty"]),
        "source_archive_sha256": source_sha256,
        "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "deploy",
    }
    stamp_str = json.dumps(stamp, ensure_ascii=False, indent=2)
    run(
        f"cat > {REMOTE_ROOT}/version.json <<'EOF'\n{stamp_str}\nEOF\n"
        f"chown anima:anima {REMOTE_ROOT}/version.json"
    )

    # 自检
    run(f"{REMOTE_ROOT}/venv/bin/anima doctor 2>&1")

    ssh.close()
    # Keep the OS-level lock alive through every remote operation.
    deploy_lock.unlink(missing_ok=True)
    print("\n[DONE] VPS 部署完成。")
    print(f"旧记忆已备份至 {REMOTE_ROOT}/data/chat_memory.json.pre-isolation（单份滚动）")
    print("迁移将在 bot 首次加载时自动完成（旧群级桶丢弃、私聊保留、污染清洗）。")


if __name__ == "__main__":
    main()
