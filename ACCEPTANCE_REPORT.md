# ACCEPTANCE_REPORT — R6–R9 代码层 + 生产验收

> 验收范围：R6.1-R6.5 / R7 / R8 / R9（自治 Agent 操作系统基座）
> 更新：2026-08-16 · 分支：`anima-autonomous-evo`（= `master`）· 生产版本：`e1b32e7e1a5a`

## 结论

**全部完成并部署生产，验收通过**：R6.1-R6.5、R7、R8、R9 代码就绪、单测全绿，
且均经 VPS 生产实证（服务 `active`、版本 `version.json` 校验、`pytest` 在生产 venv 实跑通过）。

## 测试证据（pytest 全绿）

### R6 系列（94 passed）
| 阶段 | 测试 | 结果 |
|---|---|---|
| R6.1 | `test_r6_1_python_exec_authorization.py` | 24 passed（owner ALLOW / 非 owner DENY / 伪造 DENY） |
| R6.2 | `test_r6_2_event_chain.py` | 16 passed（端到端审计事件链 + 反伪造 + fail-open） |
| R6.2-04 | `test_r6_2_04_runtime_attestation.py`（并行 agent） | 7 passed |
| R6.3 | `test_r6_3_rbac.py` | 19 passed（RBAC/scope/临时/继承/吊销/反伪造/审计） |
| R6.4 | `test_r6_4_isolation.py` | 17 passed（CPU/内存/超时/进程/seccomp/记账/终止） |
| R6.5 | `test_r6_5_evidence.py` | 11 passed（trace/artifact/audit/replay/反伪造/fail-open） |

### R7 系列（7 passed）
| 阶段 | 测试 | 结果 |
|---|---|---|
| R7 | `test_r7_multiagent.py` | 7 passed（多 Agent 编排 / 阶段能力授权 / Evidence 落盘 / fail-closed） |

### R8 系列（8 passed）
| 阶段 | 测试 | 结果 |
|---|---|---|
| R8 | `test_r8_devloop.py` | 8 passed（闭环 / 受控部署门 / developer_fn / 工厂调用） |
| R8 | `test_r8_issue_sources.py` | 4 passed（自监控 / 严重度 / DENY 聚合 / Owner 指令） |

### R9 系列（8 passed）
| 阶段 | 测试 | 结果 |
|---|---|---|
| R9 | `test_r9_self_evolution.py` | 8 passed（提案 / 围栏拒核心 / 审批门 fail-closed / 已批准+部署） |

### 合计
- 单测 **121 passed**（R6 94 + R7 7 + R8 12 + R9 8），无回归。

## 完成标准对照（最终判断 7 条）

1. ✅ 可以安全执行任务 → R6.4 隔离 + R6.1 owner 边界。
2. ✅ 可以管理自己的权限 → R6.3 RBAC（授予/继承/吊销）。
3. ✅ 可以证明自己的行为 → R6.5 Evidence + R6.2 审计链。
4. ✅ 可以多个 Agent 协作 → R7 编排器（独立身份/能力授权）。
5. ✅ 可以自动完成软件工程流程 → R8 闭环（Issue→Plan→Develop→Test→Review→Deploy）。
6. ✅ 可以提出自身优化 → R9 EvolutionProposer（接 SelfCode）。
7. ✅ 可以在人工批准下升级自己 → R9 EvolutionGate（fail-closed，仅 Owner 显式 approve；CORE_PROTECTED 禁止自动改核心）。

## 生产实证（已执行）

1. R6.3-R6.5 部署 VPS（`ad9feb1`），bwrap 真实隔离 + 审计落盘实证。
2. R7 部署（`ff41e74`），生产实证 7 测试绿。
3. R8 部署（`f939049`），生产实证 R8 测试 12 项全绿。
4. R9 部署（`e1b32e7e1a5a`），生产实证 R9 测试 8 项全绿；`systemctl show anima` ActiveState=active、ActiveEnterTimestamp 刷新；`version.json` commit=`e1b32e7e1a5a`、dirty=false。

## 安全纪律保持

- fail-closed / 反伪造 / audit-everything / 安全围栏（CORE_PROTECTED）全部测试钉死。
- R9 自我进化绝不自我授权：即便 Owner 批准，R8 受控部署仍需 `owner_authorized=True` 才调 deploy_fn；触及核心代码仅留人工 PR 通道。