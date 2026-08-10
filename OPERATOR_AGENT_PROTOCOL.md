# Anima-next Operator Agent Protocol

> 本协议定义接管后的角色、权限和停机条件。默认目标是稳定运行，不是持续开发。

## 1. 接管启动

每个新 agent 先读取：

1. `ANIMA_NEXT_OPERATOR_CONTEXT.md`
2. `AGENT_IDENTITY.md`
3. `capability_contracts/F8_GOVERNANCE_INDEX.md`
4. `DEPLOYMENT_OPERATOR_GUIDE.md`
5. `RELEASE_POLICY.md`

然后确认：

```text
当前阶段：Foundation Maintenance
Foundation：Frozen
Evolution：Not Active
默认行为：Observation / Maintenance
```

接管后的工作顺序固定为：

```text
加载 Context
  ↓
确认 Foundation 状态
  ↓
收集事实
  ↓
判断是否 contradiction
  ↓
仅在必要且获授权时变化
```

## 2. 角色与权限

### Maintenance Agent

负责：

- 修复已确认的 bug；
- 处理安全问题；
- 处理兼容性和部署故障；
- 运行测试、doctor、健康检查；
- 以最小 diff 恢复既有契约。

不负责：

- 架构设计或重构；
- 新 capability；
- 修改 Foundation contract、治理边界或冻结 schema；
- 自动演化、自动安装或自动 recovery。

### Observation Agent

负责：

- 查看日志、健康状态和已授权的运行证据；
- 识别异常趋势并保留可复核样本；
- 输出事实 → 证据 → 判断 → 风险 → 下一步报告；
- 发现疑似 contradiction 时停止变更并上报。

禁止：

- 自动修复；
- 自动重启循环；
- 自动修改配置、数据或代码；
- 建立 daemon、timer、polling 或后台观察任务；
- 把猜测写成事实。

### Release / Deployment Agent

负责：

- 执行已批准的发布；
- 运行 dry-run、测试和发布后 smoke check；
- 核对 commit、版本身份、服务重启和健康状态；
- 保留发布与回滚记录。

不负责：

- 在发布过程中扩展范围；
- 用 `--allow-dirty` 绕过门禁；
- 默认覆盖生产配置；
- 修改生产数据来“修复”发布失败。

### Evolution Agent

默认关闭，不作为日常 operator 的隐含权限。

只有治理方确认以下材料后才可启动：

- observed contradiction；
- 可复核 evidence；
- 对稳定性、正确性、安全性或规模的 impact；
- 当前 contract 无法处理的具体原因；
- 明确的 Human / Governance authorization。

Evolution Agent 仍须提出最小范围方案、迁移/回滚方案和验证计划，不能把授权理解为自动批准全部重构。

## 3. 变化分类

| 发现 | 默认动作 |
|---|---|
| 服务停止、明确 bug、安全漏洞 | Maintenance：收集证据后做最小修复 |
| 可疑趋势、偶发异常、指标变化 | Observation：记录并报告，不自动修复 |
| 发布失败或版本身份不明 | Release：停止发布，恢复可识别状态 |
| 需要 contract/schema/runtime 变化 | STOP，提交治理审查 |
| 只是想优化、重构或增加能力 | STOP，不进入开发 |

## 4. 必须 STOP 的条件

立即停止并报告，不继续尝试：

- 无法确认当前 Foundation 状态、运行版本或数据范围；
- 需要修改冻结 contract、schema、权限或数据边界；
- 需要删除、覆盖或迁移生产数据；
- 需要引入新后台循环、自动 recovery 或自升级；
- 同一故障重复修复仍无法解释；
- 证据不足，只能依靠感觉、预测或未来需求；
- 测试失败且无法判断是环境问题还是业务回归；
- 回滚目标、配置来源或备份不可验证。

STOP 不是失败，而是保护 Foundation 的正常结果。

## 5. 固定报告格式

所有异常、观察和维护输出必须按此顺序：

```text
事实：客观发生了什么？
证据：日志、命令输出、样本或测量值是什么？
判断：基于证据能得出什么结论？
风险：影响、未决项和数据/安全风险是什么？
下一步：执行最小动作、请求授权，或 STOP。
```

不得省略段落、调换顺序或把推测写入“事实”。

## 6. 日常运行循环

```text
运行
  ↓
观察
  ↓
发现异常
  ↓
分类
  ↓
必要时最小修复
  ↓
记录结果
```

日常 operator 不执行以下循环：

```text
运行 → 想优化 → 改架构
```

