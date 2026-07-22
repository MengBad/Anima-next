# Changelog

本项目采用近似语义化版本（MAJOR.MINOR.PATCH）。记录每个版本相对上一版的行为与接口变化。

## [0.3.0-rc2] — 2026-07-16

- WebUI 收敛为 Vue 3 + TypeScript + Vite 单一生命控制台，移除遗留的多套 HTML 页面覆盖。
- 新增六区导航：生命总览、对话与记忆、认知状态、能力与自动化、审批与工程、设置与权限。
- 新增三栏式设置/LLM 控制中心：提供商测试、模块模型路由、Token 用量、配置差异、风险和回滚入口。
- 表情资产管理迁移为可读交互：审核池/图库、搜索筛选、排序、批量操作、上传 AI 审核与策略维护。
- 统一响应式、触控目标、键盘焦点、减少动态效果和 CSP；Vite 资产使用哈希文件名并长期缓存。

## [0.3.0-rc1] — 2026-07-16

### RC 发布治理

- 将当前生产源码收敛为可复现发布基线，部署默认只接收 Git 已跟踪文件并拒绝脏工作树。
- 许可证统一为 `AGPL-3.0-only`，与生产使用的 `sylanne-core==2.5.0` 路径一致。
- 每次部署生成不含生产配置、密钥与运行数据的对应源码归档，并在 WebUI 登录页提供公开 Source 入口。
- 版本号统一为 `0.3.0rc1`；发布清单记录 commit、分支、文件哈希与部署时间。
- 当前 RC 基线包含独立 QQ/NapCat、认知内核、Sylann 2.5、Skills、订阅、表情、SelfCode 审批、配置中心与 LLM 用量账本。

## [0.2.1] — 2026-07-12（unreleased）

> 代码级升级计划书 `reports/2026-07-12_upgrade_plan.md` 执行批次：P0 缺陷清零 + WIP 合入（M0）。

### 阻断修复（P0）
- **心跳群号解析**（P0-1，`anima/core/autonomy.py`）：`_pick_group` 对 `group_{gid}_{uid}` 误用 `int(key.split("_",1)[1])`，Python 数字分隔符会把 uid 段放大成错误群号；改为按段取 `parts[1]`，主动发言不再发错群。
- **主动发言人格**（P0-2）：`_speak` system 由「你是 Anima，一个 QQ 群里的 AI」改为严格哈基米身份（含身份锁定 + 角色守卫短版）。
- **system 片段顺序**（P0-3，`anima/pipeline/stages.py`）：身份锁定 / 角色守卫移到跨群 / 工具心得 / 能力片段【之后】，确保守卫位于 system 绝对末尾，不被后段 priming 稀释。
- **记忆落盘硬化**（P0-5，`anima/memory/chat_memory.py`）：`add()` 由 ~10% 概率 save 改为阈值策略（累计 10 条或距上次落盘 60s 必落盘），崩溃丢对话窗口有上界；遗忘改动 store 后也落盘。

### 功能 / WIP 合入
- **联网搜索 web_search**（Bing HTML 免 key）：注册为 `Tool`，ReAct 可调用；失败返回可读中文错误，正文抓取走线程池不阻塞事件循环。
- **反刍 JSON 容错**（`anima/cognitive/rumination.py`）：新增 `_extract_json`，兼容 LLM 带 ```json 围栏 / 前后废话的非纯 JSON 返回。
- **护栏跑偏频控**（`guardrail_stage`）：同一用户 3 分钟内反复跑偏 ≥3 次本次静默不回复，避免刷屏。

### 体验 / 正确性（P1）
- **向量 threshold**（P1-1）：rerank 后 / 无 reranker 的向量路径按 `threshold` 过滤低相关记忆；BM25 降级路径保持原行为（分数尺度不同，不套用同一阈值）。
- **表情上下文 bug**（P1-2）：`_observe_stickers` 改用 `get_chat_memory().get_group_context` 并按 `content` 拼接（原引用不存在的 `config.memory.memory_dir` 且错用 `text` 字段）。
- **部署时间戳校验**（P1-3，`_deploy.py`）：重启后断言 `systemctl show anima -p ActiveEnterTimestamp` 已刷新，否则 `sys.exit(1)`。
- **旧桶备份**（P1-5）：`_restore` 丢弃旧版群级共享桶前写入 `chat_memory.legacy.bak.json` 并告警，便于追溯而非静默丢失。
- **出站 trace_id**（P1-8）：`handle` 入口生成 `secrets.token_hex(4)`，出站日志携带 `trace=`，便于全链路追踪。

### 测试
- 新增 13 个用例（心跳群号、主动发言人格、system 顺序、记忆落盘、向量 threshold、表情上下文、出站 trace、web_search、rumination JSON 等）；全量 **207 passed**（基线 194）。

## [0.2.0] — 2026-07-09

> 审计修复批次：针对 `ANIMA_AUDIT.md` 列出的 P1/P2 问题一次性修复。

### 安全 / 稳定性（P1）
- **沙箱命令执行加固**（`anima/tools/sandbox.py`）
  - `run_shell` 不再经由 `/bin/bash -c` 执行，改为 `shlex.split` 拆词后直接 `exec` 白名单命令。
  - 新增 shell 元字符阻断（`; | & $ \` < > ( ) { }` 等），`ls && rm -rf /` 这类复合绕过命令直接被拒绝。
  - 新增 `SAFE_SHELL_CMDS` 白名单集合与 `_validate_shell_command()` 校验函数。
- **沙箱 Python 解释器**（`anima/tools/sandbox.py`）
  - `run_python` 改用 `sys.executable`（即当前 venv 的 Python），而非系统 `python3`，保证运行时环境一致。
  - 异常改为 `logger.exception` 并向上返回通用提示，避免堆栈泄露给 LLM。
- **部署与可移植性**
  - `_deploy.py`：`PROJECT_ROOT` 改为 `Path(__file__).resolve().parent`，去除硬编码 Windows 路径；新增 `_render_remote_unit()` 动态渲染可移植 systemd 单元。
  - `requirements.txt`：删除硬编码 `-e g:\anima-next`。
  - `anima.service`：移除与可编辑安装冲突的 `PYTHONPATH=/opt/anima`。
- **向量记忆健康检查**（`anima/memory/vector_memory.py`）
  - 新增 `VectorMemory.health()`：验证 sqlite-vec 能否创建 vec0 虚拟表，返回 `status / sqlite_vec / embedding / semantic`。
  - 新增 `VectorMemory.close()` 关闭 Embedder 的 HTTP 会话。

### 功能 / 体验（P2）
- **真实天气工具**（`anima/tools/__init__.py`）：`_tool_weather` 从桩改为通过 `wttr.in` 查询（无需 API key），失败优雅降级。
- **`doctor` 连通性自检**（`anima/cli.py`）
  - 新增 `--config` 选项；QQ 依赖（websockets/aiohttp）改为必需检查，缺失即报错。
  - 新增 NapCat WS、LLM `/models`、向量库 health 三段连通性检查（`_check_ws` / `_check_llm` / `_check_vector_health`）。
- **`init --agent` 部署补齐**（`anima/cli.py`）
  - 新增 `--deploy-systemd/--no-deploy-systemd`；创建 `data/data/output/plugins` 目录；Linux 下渲染 systemd 单元；打印 NapCat 安装提示；配置路径透传 `doctor`。
- **AstrBot 兼容层运行时**（`anima/compat/astrbot/__init__.py`）：`Context.llm_generate` / `llm_chat` 从占位改为转发到 `llm_client.chat(...)`。
- **人格模板外置**（`config/persona_core.yaml` + `anima/pipeline/stages.py`）：`SYSTEM_PROMPT` 抽为可配置模板，新增 `load_persona_template()`。
- **自主心跳 `act` intent**（`anima/core/autonomy.py`）：新增 `think/speak/act` 三态判定，补齐只读工具调用的 `act` 分支与 `think` 内省分支。
- **消息去重与 @自己检测**（`anima/channels/onebot11.py`）
  - `parse_onebot_event` 新增 `mentions_self` / `reply_to_self` 检测。
  - `OneBot11Adapter` 新增 `message_id` 滑动窗口去重（cap 2000，超容保留最近一半）。
- **`write_file` 路径安全**（`anima/tools/__init__.py`）：仅取文件名并归一化二次校验，禁止 `../` 目录穿越逃逸 `output` 目录。

### 工程（P2/P3）
- **集成测试**（`tests/test_integration.py`）：新增端到端测试 —— OneBot 事件 → Pipeline → mock LLM → 回复，覆盖去重、@自己、沙箱白名单绕过拦截、路径穿越拦截。
- **`Makefile`**：新增 `install / test / run / doctor / clean` 任务。
- **版本号**：`pyproject.toml` 升至 `0.2.0`。

## [0.1.0] — 初始基线
- OneBot11 + NapCat 接入、5 阶段 Pipeline、ReAct 工具循环、向量记忆、认知内核、自主心跳、安全沙箱、AstrBot 兼容层（Tier 1）等核心能力落地。
- pytest 43/43 通过；AstrBot 兼容层 13/13 插件加载成功。
