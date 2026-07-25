# AGENTS.md

This file provides guidance to Lingma (lingma.aliyun.com) when working with code in this repository.

---

> 本仓库是 **Anima 的生产源码**：OpenClaw 风格独立进程，部署于 VPS `/opt/anima`，经 `_deploy.py` 发布。用户每天在 QQ 中交互的 bot 即此。
> 实验分支 `G:/Anima`（AstrBot 插件形态）已于 2026-07-12 冻结，请勿在本仓库之外开发新功能。

## 项目概述

Anima-next 是一个脱离 AstrBot 的**独立全智能 QQ 机器人**：自有运行时（Gateway）、QQ 渠道（OneBot11 / NapCat）、认知内核、消息流水线、工具 / 沙箱 / Skills、SelfCode 双 Agent 工程扩展、心跳 / 模拟生活，以及按 **QQ 号 + 群号** 分级的权限模型。

## 架构（真实状态，2026-07-18）

| 路径 | 作用 |
|---|---|
| `anima/core/` | Gateway、Pipeline、EventBus、Config、Security、Persistence、`autonomy.py`（心跳 / 生活模拟 / 意图消费）、`intents.py`、`workflows.py`、`tasks.py` |
| `anima/channels/onebot11.py` | QQ：WS 收事件 + HTTP 发送；`get_image` / `get_msg`；并发信号量 + 背压；私聊/@/主人软容量 |
| `anima/cognitive/` | 欲望 / 世界观 / 时间感 / 遗忘 / 反刍 / 伤痕 / 关系 / 表情 / 视觉 / **SelfCode** / danger 自升级提案 |
| `anima/pipeline/stages.py` | 权限 → 记忆 → 认知 → 生成；情节记忆与约定跟进；多模态视觉注入 |
| `anima/crossgroup/` | 跨群记忆 + 群氛围（已集成） |
| `anima/tools/` | 内置工具 + 沙箱；含 `self_code`（Owner 高危，只生成待审提案） |
| `anima/skills/` | Skill 清单 / 草案审批 / 内置 compose_notice（**已实现**，非 Phase 4 空壳） |
| `anima/llm/` | LLM 客户端（重试 / 熔断 / **fallback_models** / 413 再编码后剥离）+ ReAct + 用量账本 |
| `anima/media.py` | 大图下载 + WebP 缩放/质量阶梯 + GIF 关键帧；预算按 data URI 线缆体积估算 |
| `anima/webui/` | aiohttp API + Vue SPA（`webui/` 源码，`npm run build` 产出 static） |
| `config/` | `default.yaml` 模板 / `persona_core.yaml` 人格 |
| `cli.py` / `_deploy.py` | 启动自检 / VPS 发布（默认不覆盖生产 `config.yaml`） |

## 开发命令

```bash
pip install -e ".[qq,vector,dev]"          # 推荐项目 .venv
make install                                # 等价命令
make test                                   # pytest tests/ -q
make test-verbose                           # pytest tests/ -v
make run                                    # python -m anima.cli start
make doctor                                 # 环境自检（QQ/LLM/向量库）
pytest tests/ -q                            # 以全量绿为准（2026-07-18 基线约 460+）
pytest tests/test_xxx.py::test_func_name    # 运行单个测试
cd webui && npm run build                   # 产出 anima/webui/static
python -m anima init --agent --spec spec.json
python -m anima doctor
```

## 环境与配置

- Python `3.12+`；关键环境变量：`ANIMA_LLM_API_KEY`、`ANIMA_LLM_ENDPOINT`、`NAPCAT_TOKEN`
- 配置文件：`/opt/anima/config.yaml`（生产）或 `./config.yaml`（本地）；模板在 `config/default.yaml`
- WebUI 默认 `0.0.0.0:8088`，需配 `webui.token`（≥32 位随机字符串）
- QQ 接入：`napcat_ws_url: ws://127.0.0.1:3001`
- systemd 服务名 `anima`，部署目录 `/opt/anima`
- 依赖：`pyproject.toml` 声明 + `requirements.txt` 精确锁定（生产用）；可选分组 qq/vector/dev/sylann/docs
- 许可证：AGPL-3.0-only；RC 发布需干净工作树；部署生成源码归档，WebUI `/source` 公开可查

## 关键注意事项

- **部署**：`python _deploy.py` + 确认 `systemctl show --property=ActiveEnterTimestamp anima` 刷新。脏工作区默认拒绝部署。
- **会话隔离**：`chat_memory` 按 `(群,用户)`；跨群/氛围只进唯一 system。
- **人格护栏**：`pipeline/stages.py` 兜底哈基米；`persona_core.yaml` anti_derailment；身份泄漏拦截。
- **图片**：入站 `image/bface/sface`；`resolve_image` 三级兜底。**NapCat 取图必须 `get_image`**。大图先本地转码再进 LLM，413 时先再压缩后才剥图。
- **SelfCode**：只写 `data/selfcode/` 运行时工具，**不改核心源码**；安装/删除/禁用需 Owner；`auto_install` 恒不自动装。
- **自主自升级**：`danger.self_improvement*` 只生成 WebUI 待审工程任务，不自动安装。
- **降级**：阶段 try/except；可选模块静默失败。
- **语言**：标识符英文；注释/用户串中文。

## 数据路径

VPS `/opt/anima/data/`（`chat_memory.json` / `crossgroup/` / `selfcode/` / `selfcode_backup_*` / `owner_tasks.json` / `selfcode_audit.json` 等）。仓库 `data/` 为本地快照，**禁止提交生产用户数据**。

## 能力现状速查（给下游 Agent）

### Skills（已落地）

- 目录：`anima/skills/`（registry / drafts / builtin）
- WebUI：能力页 Skill 草案预览 / 批准 / 驳回
- 勿再写「Skills 尚未实现 Phase 4」——那是 07-12 旧表述

### SelfCode 双 Agent（已落地）

1. Coder 写隔离 workspace 模块（`coder_backend`: auto|claude|native）
2. Critic APPROVE/REVISE + **静态契约**回灌
3. 沙箱 import / `TOOL_META` / `async run` 自测
4. `create_engineering_task` → `awaiting_approval`
5. Owner 批准 → 备份 → 热注册；运维 API：列表 / 禁用 / 删除 / 备份恢复

WebUI：`/engineering` 工程候选 + **已装 SelfCode 工具**面板  
API：`/api/selfcode/tools`、`.../enable|disable`、`DELETE .../tools/{name}`、`/api/selfcode/backups/{id}/restore`

### 0.4 质量加固（已大量合入）

- 运行质量：`/api/reply-quality`、`/api/ops-quality`（背压/延迟/熔断）
- 媒体：base64 预算 + 413 reencode + 更紧默认尺寸/质量
- 背压：私聊/@/主人软容量
- 意图：`dispatched` / 过期联动 workflow failed
- 情节约定：群聊可生成跟进议程
- LLM：`fallback_models` 同网关容灾（生产需在 `config.yaml` 实配）

## 当前路线图

- **执行源历史**：`reports/2026-07-12_upgrade_plan.md`（M0 多数已完成，勿当空计划重做）
- **主体架构**：`reports/2026-07-13_autonomous_subject_architecture.md`
- **优先后续**：人格漂移执法（7 日窗/相似/价态，已落地代码）→ SelfCode 效果评估与建议卸载、大图哈希缓存、danger 收紧、fallback 压测  
- **明确不做**：无审批自动安装；默认不做语音模块（用户否决过）

## 测试与发布纪律

1. 改完先 `pytest tests/ -q`；WebUI 改 `webui/` 后 `npm run build`
2. 提交完整、工作区干净再 `_deploy.py`
3. 部署后核对 ActiveEnterTimestamp、关键 API 200、日志无 warning
4. 生产 `config.yaml` 默认不被覆盖；改运行参数应 SSH 改配置或 WebUI 变更集，再确认重启需求
