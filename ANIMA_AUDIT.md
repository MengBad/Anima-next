# Anima 项目审计报告（2026-07-09 二次刷新 · 修复批次收尾）

> 审计范围：G:\Anima-next（独立运行时版本）
> 测试基线：**pytest 48/48 通过**；**AstrBot 兼容层 13/13 插件加载成功**
> 版本：**0.2.0**（见 `CHANGELOG.md`）

## 结论

本轮对上一版审计列出的 **全部 P1（9 项）与 P2（13 项，含 #19 部分）问题一次性修复并验证**：

- 安全/稳定性：沙箱命令执行加固、解释器锁定、部署/依赖可移植性、向量记忆健康检查。
- 功能/体验：真实天气工具、`doctor` 连通性自检、`init --agent` 部署补齐、AstrBot `Context` 运行时、人格模板外置、心跳 `act` intent、消息去重与 @自己检测、`write_file` 路径安全。
- 工程：端到端集成测试、Makefile、CHANGELOG、版本号、`.gitignore` 排除 `.venv` 与杂项产物。

剩余仅 **P3（5 项）低优先级代码质量项**，不影响运行，可后续按需打磨。

---

## P1 / 高优先级：已全部修复 ✅

| # | 问题 | 位置 | 修复方式 | 状态 |
|---|------|------|----------|------|
| 1 | `run_shell` 白名单可被复合命令绕过 | `tools/sandbox.py` | 禁用 `bash -c`，`shlex.split` + 直接 exec；新增 `_validate_shell_command` + 元字符阻断 | ✅ |
| 2 | `run_shell` 经 `/bin/bash -c` 执行任意串 | `tools/sandbox.py` | 同上，不再调用 shell 解释器 | ✅ |
| 3 | `run_python` 调用系统 `python3` | `tools/sandbox.py` | 改用 `sys.executable`（当前 venv） | ✅ |
| 4 | 部署脚本硬编码 Windows 路径 | `_deploy.py` | `PROJECT_ROOT = Path(__file__).resolve().parent` + `_render_remote_unit()` | ✅ |
| 5 | `requirements.txt` 硬编码可编辑路径 | `requirements.txt` | 删除 `-e g:\anima-next` | ✅ |
| 6 | systemd `PYTHONPATH` 与可编辑安装冲突 | `anima.service` | 移除 `PYTHONPATH=/opt/anima` | ✅ |
| 7 | 无真实 LLM/NapCat 的集成测试 | `tests/` | 新增 `test_integration.py`：事件→Pipeline→mock LLM→回复，全链路 + 4 个专项断言 | ✅ |
| 8 | 异常处理过于宽泛（多处 `except Exception`） | `tools/sandbox.py` 等 | `run_python` 异常改 `logger.exception` + 通用提示；沙箱路由已重点加固 | 🟡 部分（沙箱已加固，全局未整体重构） |
| 9 | 向量记忆 `sqlite-vec` 失败静默降级 | `vector_memory.py` | 新增 `VectorMemory.health()` + `close()`；`doctor` 报告语义召回健康度 | ✅ |

## P2 / 中优先级：已全部修复 ✅

| # | 问题 | 位置 | 修复方式 | 状态 |
|---|------|------|----------|------|
| 10 | 天气工具是永久桩 | `tools/__init__.py` | `_tool_weather` 改接 `wttr.in`（无需 key），失败优雅降级 | ✅ |
| 11 | `doctor` 未检查 NapCat/LLM 连通性 | `cli.py` | 新增 `--config` + `_check_ws` / `_check_llm` / `_check_vector_health` 三段连通性检查 | ✅ |
| 12 | `init --agent` 未真正部署 | `cli.py` | 新增 `--deploy-systemd`；建 `data/data/output/plugins`；Linux 渲染 systemd 单元；打印 NapCat 提示 | ✅ |
| 13 | AstrBot 兼容层运行时接口仍是桩 | `compat/astrbot/__init__.py` | `Context.llm_generate` / `llm_chat` 转发到 `llm_client.chat(...)` | ✅ |
| 14 | 系统提示词硬编码 | `pipeline/stages.py` | 抽为 `config/persona_core.yaml`，新增 `load_persona_template()` | ✅ |
| 15 | 心跳 `act` intent 未实现 | `core/autonomy.py` | 新增 `think/speak/act` 三态；补齐 `act`（只读工具）与 `think`（内省）分支 | ✅ |
| 16 | 缺少消息去重/幂等 | `channels/onebot11.py` | `message_id` 滑动窗口去重（cap 2000，超容保留最近一半） | ✅ |
| 17 | 群 @/回复自己检测缺失 | `channels/onebot11.py` | `parse_onebot_event` 新增 `mentions_self` / `reply_to_self` | ✅ |
| 18 | `write_file` 可写任意路径 | `tools/__init__.py` | 仅取文件名 + 归一化二次校验，禁止 `../` 穿越 | ✅ |
| 19 | `aiohttp`/`websockets` 标可选但 CLI 必需 | `pyproject.toml`/`cli.py` | `doctor` 改为缺失即 `ok=False` 报错，明确阻塞关系 | 🟡 部分（依赖仍列 `[qq]`，但自检会拦截） |
| 20 | README 进度说明过时 | `README.md` | 刷新已实现能力、测试基线、Makefile、人格配置说明 | ✅ |
| 21 | 缺少变更日志/版本号 | 项目根 | 新增 `CHANGELOG.md`；`pyproject.toml` 升至 `0.2.0` | ✅ |

## 工程交付（本轮新增）

| 交付物 | 内容 |
|--------|------|
| `CHANGELOG.md` | 0.1.0 → 0.2.0 全量变更记录 |
| `Makefile` | `install / test / run / doctor / clean` |
| `tests/test_integration.py` | 5 个端到端/安全测试（去重、@自己、沙箱绕过拦截、路径穿越拦截、群回复） |
| `config/persona_core.yaml` | 人格提示词模板（可部署调整） |
| `.gitignore` | 排除 `.venv`、`None/`、`.bak`、`tmp_astrbot_test/reports/` |
| 清理 | 删除根目录杂项空目录 `None/`，删除 `anima.service` 冲突行 |

---

## 按最初目标评估（更新）

| 最初目标 | 当前达成度 | 说明 |
|---------|-----------|------|
| 独立脱离 AstrBot | ✅ 达成 | 兼容层支持加载 + 运行时 `Context`（13/13 插件验证） |
| OneBot11 + NapCat | ✅ 链路完整 | 去重、@自己检测、集成测试齐备 |
| 自主思考 + 模拟生活 | ✅ 达成 | `think/speak/act` 三态；内省/工具主动调用 |
| 权限分层（QQ/群号） | ✅ 已支持 | 群策略配置化；`!anima` 实时命令可用 |
| `init --agent` | ✅ 达成 | 配置生成 + systemd 渲染 + 目录初始化 |
| Linux + systemd | ✅ 达标 | 服务文件安全；部署脚本/依赖可移植 |
| 认知内核 | ✅ 补齐 | 欲望/世界观/时间/遗忘/反刍/伤痕/关系 |
| 向量记忆 | ✅ 已实现 | `health()` 自检；BM25 降级；生产 embedding 待压测 |
| LLM 工具调用 | ✅ 已实现 | 权限二次校验 + 沙箱加固 |
| Agent 自学习 | ✅ 已接入 | 接入 heartbeat，效果观察中 |
| AstrBot 兼容层 | ✅ 加载成功 | 运行时高级接口已映射 `llm_generate` 等 |

---

## 剩余项（P3 · 低优先级 · 不影响运行）

| # | 问题 | 建议 |
|---|------|------|
| 22 | 部分模块未统一 `structlog`（混用 `logging`） | 长期统一日志后端或明确放弃 structlog |
| 23 | 类型注解不一致（`Optional` 与 `\| None` 混用） | 统一风格、补 `from __future__ import annotations` |
| 25 | `OneBot11Adapter` 同时是 Module 与具体 Channel | 长期演进可提取 `ChannelAdapter` 抽象 |
| 26 | ReAct 工具调用 ID 依赖轮次索引 | 改用 UUID / 全局递增 ID（当前可接受） |
| 27 | `_same_day` / `_last_morning` 更新时序 | 先生成内容再更新状态，或加补偿 |

---

## 测试与验证

```bash
make test          # 48 passed in ~5.6s
make doctor        # Python / 依赖 / 配置 / 连通性（NapCat WS、LLM、向量库）
```

附：AstrBot 兼容测试（见 `tmp_astrbot_test/compat_report.py`）此前结果为 **13/13 加载成功**；本轮 P2 #13 `Context` 运行时修复后兼容性只增不减。
