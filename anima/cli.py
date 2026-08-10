"""CLI —— anima init / start / doctor。"""
from __future__ import annotations

import sys
import platform
import asyncio
import json
from pathlib import Path
import logging

from pydantic import SecretStr

import click


PKG_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_SRC = PKG_DIR.parent / "config" / "default.yaml"


@click.group()
def cli():
    """Anima — 独立全智能 QQ 机器人"""


@cli.command()
@click.option("--agent", is_flag=True,
              help="非交互模式（给其他 agent 部署用）：从 --spec 叠加默认值，输出机器可读自检 JSON")
@click.option("--path", "path", default="config.yaml", help="生成的配置文件路径")
@click.option("--spec", "spec", default=None,
              help="最小覆盖 spec (JSON/YAML)，仅含要覆盖的字段（支持嵌套深合并）")
@click.option("--deploy-systemd/--no-deploy-systemd", default=True,
              help="是否生成 systemd 单元文件（仅 Linux 有效）")
def init(agent, path, spec, deploy_systemd):
    """生成配置并自检 + 准备部署目录。

    --agent 模式：以 config/default.yaml 为默认模板，将 --spec 深合并后
    经 AnimaConfig(pydantic) 校验补全，写出 config.yaml，并输出机器可读 JSON
    {ok, config_path, checks, next}；不打印控制台提示，便于其他 agent 调用。
    """
    import yaml as _yaml
    from anima.core.config import AnimaConfig

    dst = Path(path)

    # 1) 默认模板
    base: dict = {}
    if DEFAULT_CONFIG_SRC.exists():
        base = _yaml.safe_load(DEFAULT_CONFIG_SRC.read_text(encoding="utf-8")) or {}
    else:
        click.echo("默认配置模板缺失 (config/default.yaml)", err=True)
        sys.exit(1)

    # 2) 合并 spec（深合并，覆盖默认值）
    if spec:
        try:
            spec_data = _load_spec(spec)
        except Exception as e:
            if agent:
                click.echo(json.dumps({"ok": False, "error": f"spec 加载失败: {e}"},
                                      ensure_ascii=False, indent=2))
            else:
                click.echo(f"X spec 加载失败: {e}", err=True)
            sys.exit(1)
        _deep_merge(base, spec_data)

    # 3) pydantic 校验 + 默认值补全
    try:
        cfg = AnimaConfig.model_validate(base)
    except Exception as e:
        if agent:
            click.echo(json.dumps({"ok": False, "error": f"配置校验失败: {e}"},
                                  ensure_ascii=False, indent=2))
        else:
            click.echo(f"X 配置校验失败: {e}", err=True)
        sys.exit(1)

    # 注意：SecretStr 经 model_dump 会被掩码为 '**********'，
    # 必须在写出前用 .get_secret_value() 解开，否则部署出的配置密钥无效。
    plain = _unwrap_secrets(cfg.model_dump())
    dst.write_text(_yaml.safe_dump(plain, allow_unicode=True, sort_keys=False),
                  encoding="utf-8")
    if agent:
        click.echo(f"已生成配置（agent 模式）: {dst}")
    else:
        click.echo(f"已生成默认配置：{dst}")

    # 4) 运行时目录
    for d in ("data", "data/output", "plugins"):
        Path(d).mkdir(parents=True, exist_ok=True)
    if not agent:
        click.echo("OK 已创建运行时目录: data/ data/output/ plugins/")

    # 5) systemd 单元（可移植，基于当前项目根）
    if deploy_systemd and platform.system() == "Linux":
        unit_path = Path("anima.service")
        unit_path.write_text(_render_systemd_unit(), encoding="utf-8")
        if not agent:
            click.echo(f"OK 已生成 systemd 单元: {unit_path}（部署时 cp 到 /etc/systemd/system/）")
            click.echo("  部署命令: cp anima.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now anima")

    # 6) agent 模式：机器可读自检，跳过控制台提示
    if agent:
        checks = _agent_selfcheck(cfg)
        ok = all(c["ok"] for c in checks)
        result = {
            "ok": ok,
            "config_path": str(dst),
            "checks": checks,
            "next": [
                "确保 NapCat 已启动并暴露 WS/HTTP 于配置中的 qq.napcat_ws_url",
                f"anima start --config {dst}",
                "生产环境: cp anima.service /etc/systemd/system/ && systemctl enable --now anima",
            ],
        }
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if ok else 1)

    # 7) 交互模式：NapCat 提示 + doctor
    click.echo("--- NapCat 安装提示（同机 same-host 拓扑）---")
    click.echo("  npm i -g napcat-cli")
    click.echo("  napcat start --ws 3001 --token <your-token>")
    click.echo("  并将 config.yaml 中 qq.napcat_ws_url 指向 ws://127.0.0.1:3001")

    ctx = click.Context(doctor)
    ctx.params["config_path"] = str(dst)
    doctor.invoke(ctx)


def _render_systemd_unit() -> str:
    """基于当前项目根生成可移植的 systemd 单元文件。"""
    import getpass
    root = Path(__file__).resolve().parent.parent
    user = getpass.getuser() or "anima"
    return f"""[Unit]
Description=Anima QQ Bot
After=network-online.target
Wants=network-online.target
After=napcat.service

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={root}
ExecStart={root}/venv/bin/python -m anima start --config {root}/config.yaml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# 安全加固
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={root}/data

[Install]
WantedBy=multi-user.target
"""


# ---------------------------------------------------------------------------
# agent 非交互部署辅助
# ---------------------------------------------------------------------------

def _load_spec(spec_path: str) -> dict:
    """加载 --spec 文件（JSON 或 YAML）。"""
    p = Path(spec_path)
    if not p.exists():
        raise FileNotFoundError(f"spec 文件不存在: {spec_path}")
    text = p.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import yaml as _yaml
        return _yaml.safe_load(text) or {}


def _deep_merge(base: dict, override: dict) -> None:
    """原地深合并 override 到 base（仅 dict 递归，其余直接覆盖）。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _unwrap_secrets(obj):
    """递归解开 pydantic.SecretStr（否则序列化会被掩码为 '**********'）。"""
    if isinstance(obj, SecretStr):
        return obj.get_secret_value()
    if isinstance(obj, dict):
        return {k: _unwrap_secrets(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_unwrap_secrets(v) for v in obj]
    return obj


def _agent_selfcheck(cfg) -> list:
    """返回结构化自检结果列表，供 agent 解析（不致命项失败也列入 checks）。"""
    checks = []

    # Python 版本
    checks.append({
        "name": "python>=3.12",
        "ok": sys.version_info >= (3, 12),
        "detail": f"{sys.version_info.major}.{sys.version_info.minor}",
    })

    # 核心依赖
    for mod in ("pydantic", "yaml", "click"):
        try:
            __import__(mod)
            checks.append({"name": f"dep:{mod}", "ok": True, "detail": ""})
        except ImportError:
            checks.append({"name": f"dep:{mod}", "ok": False, "detail": "缺失"})

    # LLM 配置
    _provider = cfg.llm.provider_config()
    _provider_key = _provider.get("api_key", cfg.llm.api_key)
    llm_key = bool(_provider_key.get_secret_value() if hasattr(_provider_key, "get_secret_value") else _provider_key)
    checks.append({
        "name": "llm.api_key",
        "ok": llm_key,
        "detail": str(_provider.get("endpoint") or cfg.llm.endpoint) if llm_key else "未配置（将进入 echo 模式）",
    })

    # QQ 渠道依赖
    qq_ok = True
    for mod in ("websockets", "aiohttp"):
        try:
            __import__(mod)
        except ImportError:
            qq_ok = False
    checks.append({"name": "qq.deps", "ok": qq_ok, "detail": ""})

    # 连通性（best-effort，失败不致命）
    if qq_ok and cfg.qq.napcat_ws_url:
        ws_ok = _check_ws(cfg.qq.napcat_ws_url, cfg.qq.access_token)
        checks.append({
            "name": "napcat.ws",
            "ok": ws_ok,
            "detail": cfg.qq.napcat_ws_url,
        })
    if llm_key:
        llm_reach = _check_llm(cfg.llm)
        checks.append({
            "name": "llm.reach",
            "ok": llm_reach,
            "detail": cfg.llm.endpoint,
        })

    # owner 权限
    checks.append({
        "name": "permissions.owners",
        "ok": bool(cfg.permissions.owners),
        "detail": f"{len(cfg.permissions.owners)} owner(s)",
    })
    return checks


@cli.command()
@click.option("--config", "config_path", default="config.yaml")
@click.option("--once", is_flag=True, help="启动后立即退出（冒烟测试）")
def start(config_path, once):
    """启动 Anima。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # 配置变更重启的第一段启动保护必须发生在 Pydantic 加载之前：
    # 若上一次候选配置在初始化阶段崩溃，本次进程先恢复上一份可用 YAML。
    from anima.core.config_recovery import prepare_start
    # ``--once`` is an import/config smoke test and never reaches the runtime
    # health point, so it must not consume a candidate-start attempt.
    recovery_status = "none" if once else prepare_start(config_path)
    if recovery_status == "rolled_back":
        click.echo("检测到候选配置未通过启动健康点，已自动回滚上一版本")

    from anima.core.config import load_config
    from anima.core.anima_core import AnimaCore
    from anima.channels.onebot11 import OneBot11Adapter

    cfg = load_config(config_path)

    core = AnimaCore(cfg)
    core.config_path = str(config_path)
    if recovery_status == "rolled_back":
        marker_change = "自动回滚（启动健康检查失败）"
        core.audit.emit("config_auto_rolled_back", reason=marker_change)
    # staging 不接 NapCat：仍可验证 LLM、SelfCode、WebUI 与审计，但不会抢答线上 QQ。
    if cfg.qq.enabled:
        channel = OneBot11Adapter(
            ws_url=cfg.qq.napcat_ws_url,
            http_url=cfg.qq.napcat_http_url,
            self_id=cfg.qq.self_id,
            access_token=cfg.qq.access_token.get_secret_value() if cfg.qq.access_token else None,
            event_concurrency=cfg.qq.event_concurrency,
            max_pending_events=cfg.qq.max_pending_events,
        )
        core.gateway.bind(channel)
        core.register(channel)
    else:
        click.echo("QQ 渠道已禁用：以隔离 staging 模式运行")

    # Runtime recovery wiring (F4.7b): register a Module whose restore() runs the
    # runtime recovery bootstrap during core.start()'s restore loop. The store /
    # workflow / planning handles are runtime dependencies, constructed here and
    # injected; core stays the host (no core -> runtime dependency).
    try:
        from anima.core.persistence import PersistenceProvider
        from anima.constitution.planning_interface import PlanningInterface
        from anima.constitution.workflow import WorkflowInterface
        from anima.runtime.persistence import WorkflowRunStore
        from anima.runtime.recovery_module import RecoveryModule

        core.register(RecoveryModule(
            store=WorkflowRunStore(PersistenceProvider(cfg.persistence.data_dir)),
            workflow=WorkflowInterface(),
            planning=PlanningInterface(),
        ))
    except Exception:
        # Recovery wiring must never block startup (degradation, AGENTS.md):
        # a failure to construct the runtime recovery module is logged and skipped.
        logging.getLogger("anima.cli").exception("runtime recovery wiring failed, continuing")

    # LLM 客户端
    provider_cfg = cfg.llm.provider_config()
    provider_key_value = provider_cfg.get("api_key", cfg.llm.api_key)
    provider_key_text = provider_key_value.get_secret_value() if hasattr(provider_key_value, "get_secret_value") else str(provider_key_value or "")
    if provider_key_text:
        from anima.llm import LLMClient
        from anima.llm.usage import UsageLedger
        api_key = provider_cfg.get("api_key", cfg.llm.api_key)
        provider_api_key = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else str(api_key or "")
        llm = LLMClient(
            endpoint=str(provider_cfg.get("endpoint") or cfg.llm.endpoint),
            api_key=provider_api_key,
            model=str(provider_cfg.get("model") or cfg.llm.model),
            timeout=float(provider_cfg.get("timeout", cfg.llm.timeout)),
            retries=int(provider_cfg.get("retries", cfg.llm.retries)),
            retry_backoff_sec=float(provider_cfg.get("retry_backoff_sec", cfg.llm.retry_backoff_sec)),
            fallback_models=list(provider_cfg.get("fallback_models") or cfg.llm.fallback_models),
            circuit_failure_threshold=int(provider_cfg.get("circuit_failure_threshold", cfg.llm.circuit_failure_threshold)),
            circuit_cooldown_sec=float(provider_cfg.get("circuit_cooldown_sec", cfg.llm.circuit_cooldown_sec)),
            provider=cfg.llm.active_provider,
            module_models=cfg.llm.module_models,
            usage_ledger=UsageLedger(cfg.persistence.data_dir),
        )
        core.llm = llm
        core.usage_ledger = llm.usage_ledger
        click.echo(f"LLM 已接入: {cfg.llm.model} @ {cfg.llm.endpoint}")

        # 工具注册表
        from anima.tools import create_default_registry
        core.tools = create_default_registry(core)
        if cfg.skills.enabled:
            from anima.skills import SkillRegistry
            root = Path(cfg.persistence.data_dir) / cfg.skills.dir
            builtin_root = Path(__file__).resolve().parent / "skills" / "builtin"
            core.skills = SkillRegistry(root, core, max_loaded=cfg.skills.max_loaded,
                                        builtin_root=builtin_root)
            core.skills.load()
            click.echo(f"Skills: 已加载 {core.skills.install_into_tools()} 个")
            from anima.skills import SkillDraftManager
            core.skill_drafts = SkillDraftManager(cfg.persistence.data_dir, core)
        if cfg.self_code.enabled:
            from anima.cognitive.selfcode import get_selfcode_manager
            restored = get_selfcode_manager(core).load_installed()
            click.echo(f"SelfCode 运行时能力: 已恢复 {restored} 个（均为此前 Owner 已批准项）")
        click.echo(f"工具已注册: {len(core.tools.tools)} 个")

        # 初始化记忆（含持久化）
        from anima.memory.chat_memory import get_chat_memory
        persist_path = f"{cfg.persistence.data_dir}/chat_memory.json"
        mem = get_chat_memory(persist_path=persist_path)
        click.echo("对话记忆: 已加载" if mem._store else "对话记忆: 空")

        # 注册 LLM 对话处理器（含 Pipeline）
        _register_llm_handler(core)

        # 加载 AstrBot 兼容插件
        if cfg.compat.astrbot_enabled:
            from anima.compat import load_plugins
            # data_dir 是字符串，必须用 Path 包裹后才能做路径运算（修复 str/str TypeError）
            plugins_dir = Path(cfg.persistence.data_dir).parent / "plugins"  # 项目根/plugins
            # 更稳健：使用工作目录下的 plugins（CWD 即项目根）
            plugins_dir = Path("plugins")
            n = load_plugins(plugins_dir, core)
            click.echo(f"AstrBot 兼容插件: 已加载 {n} 个")

        # 启动自主心跳
        if cfg.autonomy.enabled:
            from anima.core.autonomy import Heartbeat
            core.heartbeat = Heartbeat(
                core,
                tick_sec=cfg.autonomy.heartbeat_tick_sec,
                cooldown_sec=cfg.autonomy.cooldown_sec,
                daily_max=cfg.autonomy.daily_max_per_group,
                ratio_max=cfg.autonomy.ratio_max,
                token_budget=cfg.autonomy.token_budget_per_day,
                active_reply_prob=cfg.autonomy.active_reply_prob,
                active_silence_min_sec=cfg.autonomy.active_silence_min_sec,
                active_global_cooldown_sec=cfg.autonomy.active_global_cooldown_sec,
                active_min_group_messages=cfg.autonomy.active_min_group_messages,
                active_daily_max_per_group=cfg.autonomy.active_daily_max_per_group,
                active_context_score_threshold=cfg.autonomy.active_context_score_threshold,
                active_feedback_window_sec=cfg.autonomy.active_feedback_window_sec,
                active_feedback_min_samples=cfg.autonomy.active_feedback_min_samples,
                active_feedback_min_acceptance=cfg.autonomy.active_feedback_min_acceptance,
                data_dir=cfg.persistence.data_dir,
            )
            core.heartbeat.active_intent_max_attempts = int(
                getattr(cfg.autonomy, "active_intent_max_attempts", 5) or 5
            )
            # 心跳群消息计数回调
            mem.set_group_callback(core.heartbeat.count_message)
            core._auto_start_heartbeat = True
            click.echo("自主心跳: 已配置 (30s tick, per-group cooldown)")
    else:
        click.echo("LLM 未配置，使用 echo 模式")
        _register_echo_handler(core)

    if once:
        click.echo("冒烟测试：配置加载 + 适配器绑定成功，立即退出。")
        return

    click.echo(f"Anima 启动中，WS={cfg.qq.napcat_ws_url}")
    core.run()


def _register_echo_handler(core):
    """注册一个简单的 echo handler：群聊中收到「ping」回复「pong」。
    这是 P0 验证用，LLM 不可用时回退。
    """
    from anima.core.gateway import OutboundMsg

    async def echo_handler(event):
        text = event.get("text", "").strip()
        user_id = event.get("user_id")
        group_id = event.get("group_id")
        detail_type = event.get("raw", {}).get("message_type", "private")
        target_id = group_id if detail_type == "group" else user_id

        if text.lower() == "ping":
            reply = f"[CQ:at,qq={user_id}] pong!"
            await core.gateway.send(OutboundMsg(detail_type, target_id, reply))

    core.bus.subscribe("event.message.group", echo_handler)
    core.bus.subscribe("event.message.private", echo_handler)


def _register_llm_handler(core):
    """注册 LLM 对话处理器：订阅所有消息事件，调用 LLM 生成回复。"""
    from anima.llm.handler import LLMHandler

    handler = LLMHandler(core)
    core.llm_handler = handler

    async def on_message(event):
        await handler.handle(event)

    core.bus.subscribe("event.message.group", on_message)
    core.bus.subscribe("event.message.private", on_message)


@cli.command("ops-cleanup")
@click.option("--data-dir", "data_dir", default="data", help="运行时数据目录")
@click.option("--keep-running", is_flag=True, help="不取消全部 selfcode running（仅超时）")
def ops_cleanup_cmd(data_dir, keep_running):
    """清理僵尸 SelfCode、粘滞笔记/世界观、玩具自升级欲望。"""
    from anima.cognitive.ops_cleanup import run_ops_cleanup

    report = run_ops_cleanup(
        data_dir=data_dir,
        cancel_all_running_selfcode=not keep_running,
    )
    click.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))


@cli.command()
@click.option("--config", "config_path", default="config.yaml", help="用于连通性检查的配置文件")
def doctor(config_path):
    """环境自检：Python、依赖、配置、连通性。"""
    ok = True

    # Python
    if sys.version_info < (3, 12):
        click.echo(f"X Python {sys.version_info.major}.{sys.version_info.minor} < 3.12", err=True)
        ok = False
    else:
        click.echo(f"OK Python {sys.version_info.major}.{sys.version_info.minor}")

    # 核心依赖
    for mod_name in ("pydantic", "yaml", "click"):
        try:
            __import__(mod_name)
            click.echo(f"OK 核心依赖 {mod_name}")
        except ImportError:
            click.echo(f"X 缺核心依赖 {mod_name}", err=True)
            ok = False

    # structlog 为可选（代码统一使用 stdlib logging）
    try:
        __import__("structlog")
    except ImportError:
        click.echo("~ 可选 structlog 未装（不影响运行，代码使用 stdlib logging）")

    # QQ 渠道依赖 —— 启动 QQ 渠道必需
    qq_deps_ok = True
    for mod_name, label in (("websockets", "WebSocket"), ("aiohttp", "HTTP")):
        try:
            __import__(mod_name)
            click.echo(f"OK QQ 依赖 {label}: {mod_name}")
        except ImportError:
            click.echo(f"X 缺 QQ 依赖 {mod_name}（启动 QQ 渠道必需，请 pip install -e '.[qq]'）", err=True)
            ok = False
            qq_deps_ok = False

    # 向量库依赖
    for mod_name, label in (("aiosqlite", "向量库"), ("sqlite_vec", "向量库")):
        try:
            __import__(mod_name)
            click.echo(f"OK {label}: {mod_name}")
        except ImportError:
            click.echo(f"~ 可选 {label}: {mod_name} 未装（向量记忆不可用，将降级）")

    click.echo(f"~ 平台: {platform.system()} (部署目标: Linux)")

    # 数据目录
    Path("data").mkdir(exist_ok=True)
    click.echo("OK 数据目录可写: data/")

    # 配置文件与连通性检查（可选，需要配置存在）
    cfg = _load_config_silent(config_path)
    if cfg is not None:
        click.echo("--- 连通性检查 ---")
        # NapCat WS
        if qq_deps_ok and cfg.qq.napcat_ws_url:
            if _check_ws(cfg.qq.napcat_ws_url, cfg.qq.access_token):
                click.echo(f"OK NapCat WS 可达: {cfg.qq.napcat_ws_url}")
            else:
                click.echo(f"~ NapCat WS 不可达: {cfg.qq.napcat_ws_url}（请确认 NapCat 已启动）")

        # LLM endpoint
        if cfg.llm.api_key.get_secret_value():
            if _check_llm(cfg.llm):
                click.echo(f"OK LLM endpoint 可达: {cfg.llm.endpoint} ({cfg.llm.model})")
            else:
                click.echo(f"~ LLM endpoint 不可达或未返回 models: {cfg.llm.endpoint}")
        else:
            click.echo("~ LLM api_key 未配置（echo 模式）")

        # 向量库健康
        if _check_vector_health(cfg):
            click.echo("OK 向量记忆后端正常")
        else:
            click.echo("~ 向量记忆后端不可用（将降级为 BM25/无语义召回）")
    else:
        click.echo("~ 未找到配置文件，跳过连通性检查（运行 anima init 生成）")

    if ok:
        click.echo("OK 自检通过")
    else:
        click.echo("X 自检未通过", err=True)
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# 连通性检查辅助函数
# ---------------------------------------------------------------------------

def _load_config_silent(config_path: str):
    """静默加载配置；不存在/解析失败返回 None。"""
    p = Path(config_path)
    if not p.exists():
        return None
    try:
        from anima.core.config import load_config
        return load_config(config_path)
    except Exception:
        return None


def _check_ws(ws_url: str, access_token) -> bool:
    """尝试连接 NapCat WS，验证可达性（5s 超时）。"""
    try:
        import websockets
    except ImportError:
        return False
    import asyncio

    headers = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    async def _probe():
        async with asyncio.timeout(5):
            async with websockets.connect(ws_url, additional_headers=headers, open_timeout=5):
                return True

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


def _check_llm(llm_cfg) -> bool:
    """调用 LLM /models 端点验证鉴权与可达性（8s 超时）。"""
    try:
        import aiohttp
    except ImportError:
        return False
    import asyncio

    url = llm_cfg.endpoint.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {llm_cfg.api_key.get_secret_value()}"}

    async def _probe():
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=headers) as resp:
                return resp.status == 200

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


def _check_vector_health(cfg) -> bool:
    """初始化向量后端，验证 sqlite-vec / embedding / reranker 是否可用。"""
    try:
        from anima.memory.vector_memory import create_vector_memory
        import asyncio

        async def _probe():
            vm = create_vector_memory(cfg)
            if vm is None:
                return False
            status = await vm.health()
            await vm.close()
            return status.get("status") == "ok"

        return asyncio.run(_probe())
    except Exception:
        return False


def main():
    cli()
