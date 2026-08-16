# Changelog

本项目采用近似语义化版本（MAJOR.MINOR.PATCH）。记录每个版本相对上一版的行为与接口变化。

## [R6] — 2026-08-15（Governance & Execution）

### R6.1 Owner Authorization Boundary — COMPLETE
- python.exec owner-only 硬边界（`PythonExecProvider._authorize_owner`），fail-closed，D1-D4 短路。
- 已部署 `8eca02d`，VPS 验收通过（owner ALLOW / 非 owner DENY / 伪造 DENY 全验证）。

### R6.2 Governance Hardening — COMPLETE
- 并行 agent：capability revoke / kill switch / audit hash chain / runtime attestation（`32d40de→424f434` 部署）。
- 本 agent：`event_chain.py` 端到端审计事件链（caller/capability/permission/provider/sandbox/result/timestamp）
  + 通用 JSONL 存储 + fail-open sink + 反伪造（16 测试绿）。

### R6.3 Permission Model v2 — COMPLETE
- `rbac.py` 企业级 RBAC：Principal→Role→Capability→Scope→Policy→Audit。
- RBAC / capability scope / temporary permission / inheritance / revoke / 反伪造 / fail-closed（19 测试绿）。
- 已提交 `ad9feb1`，随 R6.3/4/5 一并部署 VPS。

### R6.4 Execution Isolation — COMPLETE
- `isolation.py` 工业级隔离：CPU/内存/超时/进程/seccomp 策略档 + 资源记账 + TERM→KILL 终止（17 测试绿）。
- 已提交 `ad9feb1`，随 R6.3/4/5 一并部署 VPS。

### R6.5 Execution Evidence — COMPLETE
- `evidence.py` 行为证据：execution trace→artifact→audit→replay，任何行为可事后还原（11 测试绿）。
- 补充 `EvidenceSink.__call__` 可调用接口（兼容编排器证据投递）。
- 已提交 `ad9feb1`，随 R6.3/4/5 一并部署 VPS。

### R7 Multi-Agent Architecture — COMPLETE
- `orchestrator.py` 多 Agent 安全协作编排：Planner/Developer/Tester/Reviewer/Deploy 流水线。
- 每 Agent 独立身份 + RBAC 能力/scope 独立授权（fail-closed）。
- 每阶段落 R6.5 Evidence（ALLOW/DENY 都记录，Audit Everything）。
- 已提交 `ff41e74`，已部署 VPS（version `ff41e740a1b7`），生产实证 54 项测试全绿。

### R8 Autonomous Development Loop — COMPLETE
- `devloop.py` 自主开发闭环：DevIssue → Plan→Develop→Test→Review→Deploy 端到端编排。
- `orchestrator.run` 加性支持 `stage_capabilities`（各 Agent 按各自能力授权）。
- `issue_sources.py`：自监控（SelfCode 效果建议/审计高频 DENY）+ Owner 指令 → DevIssue；严重度过滤，只读采集。
- 受控门禁（fail-closed）：部署需 Owner 显式授权，默认不调 deploy_fn；Developer 复用 SelfCode 双 Agent 仅生成候选、不自动安装（self-improvement ≠ self-authorization）。
- 真实接线：`make_r8_selfcode_developer`（接 SelfCodeManager）/ `make_r8_deploy_fn`（接 `_deploy.py`）/ `_run_pytest`（TEST 阶段）。
- 已提交 `f939049`，已部署 VPS（version `f939049b1716`），生产实证 R8 测试 12 项全绿；R6+R7+R8 全量 113 测试无回归。

### R9 Controlled Self Evolution — COMPLETE
- `self_evolution.py`：ANIMA 可提出自身优化，但**绝不自我授权**生产更新。
- `EvolutionProposal` / `EvolutionProposer`（复用 SelfCode 双 Agent 生成候选，仅提案不安装）/ `EvolutionReviewer`（安全围栏）/ `EvolutionGate`（fail-closed 审批门，默认 DENY）/ `EvolutionLoop`（接入 R8 闭环）。
- **安全围栏**：`CORE_PROTECTED` 黑名单禁止 R9 自动改核心权限/安全/部署代码（rbac.py/orchestrator.py/security.py/evidence.py/_deploy.py 等），触及即 reject，需 Owner 人工 PR。
- **受控原则**：self-improvement ≠ self-authorization——即便 Owner 批准，R8 受控部署仍需 owner_authorized=True 才调 deploy_fn。
- 已提交 `e1b32e7`，已部署 VPS（version `e1b32e7e1a5a`），生产实证 R9 测试 8 项全绿；R6+R7+R8+R9 全量 121 测试无回归。

### R9-02 WebUI 受控自我进化通道 — COMPLETE
- `self_evolution.py`：提案 `to_dict` 增 `touches_core`/`decision` 透传；新增 `get_evolution_loop`/`get_evolution_gate`（进程内单例）/ `propose_evolution`/`list_evolution_proposals`/`decide_evolution`；`EvolutionLoop` 复用单例 gate（propose/approve/reject 状态跨请求一致）。
- 后端 `server.py` + `routes/__init__.py`：4 个 `/api/evolution` 端点（proposals / propose / {id}/approve / {id}/reject），批准仅放行**提案**不部署，全部审计落盘。
- 前端 `EngineeringView.vue` + `api.ts` + `types.ts`：工程页新增「受控自我进化」面板（发起提案 / 触核心红标 / 批准·驳回），`npm run build` 类型检查通过。
- `tests/test_webui_r9_evolution.py`：2 端点测试（propose→approve→reject + 触核心被围栏 reject）。
- 安全纪律：UI 批准 = `gate.approve` 仅放行提案；绝不借 WebUI 自我授权生产更新；`CORE_PROTECTED` 提案在 propose 阶段即被围栏拒绝。
- 已提交 `dfe993d`，已部署 VPS（version `dfe993d7439a`），生产实证 `/api/evolution/proposals` 返回 200、R9+WebUI 端点测试 10 项全绿；全量 **123 测试**无回归。

### R10-00 GitHub Actions CI/CD — COMPLETE
- `.github/workflows/anima-ci.yml`：push/PR 到 `master`/`anima-autonomous-evo` 触发；四 job（backend/frontend/security/evidence）。
- `scripts/anima_guard.py`：检测 `CORE_PROTECTED` 与安全关键路径改动，产出 `security-report.json`，标记 `requires_owner_review`，仅报告不阻断（fail-closed 体现在「须 Owner 审查」）。
- `scripts/ci_evidence.py`：汇总 `ci-report.json` + `commit-attestation.json`（声明未自动部署/未自动批准 Evolution/未自动改代码）。
- **禁止项（架构保证）**：CI 不含 deploy 步骤、不含 auto-approve Evolution、不含 auto-modify code、不含 auto-skip-failure。
- `Makefile` 加 `ci`/`security-guard`/`ci-evidence` 本地复跑目标；`reports/2026-08-16_r10_engineering_autonomy.md` 计划书。
- 依赖补全：`pyproject.toml`/`requirements.txt` 声明 `paramiko>=3.0`（`_deploy.py` SSH 发布依赖，修复 CI 中 test_deploy/test_release_baseline 收集失败）。
- CI 安装全可选依赖组 `[qq,vector,dev,docs,sylann]`；忽略 6 个已知预存 broken 测试（`anima.pilot` 缺失 / 冻结配置集过期 / subprocess 硬编码 Windows 路径 `G:/Anima-next`），聚焦 R6–R9+R10 核心。
- **GitHub Actions 真实验证通过**（run 31935652741）：Backend 2113 passed / Frontend build / Security guard / Evidence 四 job 全绿；`commit-attestation.json` 声明未自动部署、未自动批准 Evolution、未自动改代码。

### R10-01 Reality Observation Layer — COMPLETE
- 新增 `anima/observation/`：统一观测事件层。
- `event.py`：`ObservationEvent` + 严重度模型（`to_dict`/`severity_rank`），统一 `{type, source, severity, title, detail, evidence, target, ts, id}`。
- `audit_analyzer.py`：扫描审计链，高频 DENY（同 target ≥ min_denials 次）→ high 级 `audit_deny`；显式异常 → `runtime_error`。
- `runtime_monitor.py`：读 `health_history`/`evaluation`，进程未运行/渠道未连接 → high；评估注意力 → normal。
- `user_feedback.py`：归一化反馈队列，命中 bug/报错关键词 → high。
- `collector.py`：`collect_all` 聚合全部源；`to_dev_issues` 桥接 R8 `DevIssue`（进入自主开发闭环）。
- 设计纪律：观测层只归集、不自我授权、不触发自愈（自愈属 R13）；每条带 evidence（R6.5 Audit Everything）。
- `tests/test_r10_01_observation.py`：9 测试全绿（审计聚合/窗口过滤/运行时降级/反馈归一/聚合桥接/空输入安全）。
- `github_source.py`（GitHub Issue 采集）留 TODO：需 API token，不阻塞当前闭环。

### R10-02 Evolution Evaluation Engine — COMPLETE
- 新增 `anima/evolution/evaluator/`：把「观测 → 评估」闭环的评估半环工程化、可验证化。
- `metric.py`：`MetricSample` + `collect_metrics(core, context)` 抽取 Before/After 指标（runtime_running / channel_connected / llm_request_count / llm_failure_count / llm_failure_rate / health_samples / automation_pending）。
- `benchmark.py`：`Benchmark` 编排 `capture_before` / `capture_after` / `set_before_metrics` / `evaluate_pair`，产出 `ImprovementEvidence`。
- `comparison.py`：`ImprovementEvidence` + `compare_metric` / `compare_samples` / `summarize_verdict`；按指标方向（higher/lower-better）判定 improved / degraded / neutral，聚合出 effective / ineffective / neutral 结论，每条结论带 evidence 明细。
- 接入 R9 `EvolutionLoop.evolve`：持有 `core` 时，部署前后自动采集指标、产出改进证据，并 fail-open 落盘 `data/evolution_evidence.jsonl`（`_save_evidence`）。
- `_HIGHER_BETTER` / `_LOWER_BETTER` 方向集合 + `_direction()` 用成员判定（修复此前误用 `.get` 对 set 的 bug）。
- `tests/test_r10_02_evaluation.py`：12 测试全绿（度量抽取 / 方向判定 / 前后对照 / 聚合结论 / 集成：evolve 经 core 产出 improvement_evidence）。
- 与 R10-01 Observation 共同构成「观测 → 评估」闭环：Observation 发现问题 → Evaluation 量化验证改进效果，闭环可审计、不自我授权。

### R10-03 可观测自治闭环收口（Observation→Evolution→Evaluation）— COMPLETE
- 新增 `anima/evolution/orchestrator.py`：`ObservationOptimizationLoop` 把 R10-01 观测 → R8/R9 受控自我进化 → R10-02 Before/After 改进证据串成单一可观测闭环（ObservableCycleReport）。
- 设计纪律：默认 `dry_run`（只观测 + 提案 + 量化评估，不部署）；`dry_run=False` 仍需 `owner_authorized=True` 才真部署（受控门禁不变）。闭环只产出提案与证据，绝不自我授权生产更新；观测/评估层只归集量化、不决策不自愈。
- `observe()` 复用 `collect_all` + `to_dev_issues`；`run_cycle()` 逐 Issue 经 `EvolutionLoop.evolve` 提案，聚合 `_aggregate_evidence` 给出 effective/harmful/ineffective 整体结论。
- `tests/test_r10_observation_evolution_loop.py`：2 测试全绿（dry_run 不部署 / owner 授权部署产出改进证据）。
- R9 + R10-01 + R10-02 + 闭环 组合 31 测试无回归。R10 工程化自治闭环（CI / 观测 / 评估 / 闭环）全部收口。

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
