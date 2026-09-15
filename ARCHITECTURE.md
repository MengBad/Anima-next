> [HISTORICAL] 本文档停更于 2026-08-16，仅作历史参考；现状以 README.md / MAINTENANCE.md / schemas/config.schema.yaml 为准。

# ARCHITECTURE — ANIMA 系统架构

> 反映当前（R6 系列）真实架构。随推进更新。

## 当前架构（R6 阶段）

```
                         OWNER
                           |
                           |
                    Governance Layer
                     (capability/governance)
                           |
                           |
                       ANIMA CORE
        +------------------+----------------+
        |                  |                |
    Principal         Capability        Policy Engine
    (R6.1 owner       Gateway          (R6.3 RBAC)
     boundary)         (resolver)          |
        |                  |                |
        +------------------+----------------+
                           |
                     Provider (runtime/providers)
                           |
                       Sandbox (tools/sandbox)
                           |
                      Execution + Evidence
                     (R6.4 isolation + R6.5 trace/audit/replay)
```

## 关键子系统

### Governance 层（anima/capability）
- `governance/gateway.py`：Capability Gateway（fan-in + fail-open 回退）
- `governance/audit.py`：ResolutionEvent / ResolutionTrace（resolver 选择过程审计）
- `governance/attestation.py`：运行时漂移检测（running SHA vs approved SHA）
- `audit/persistent_sink.py`：ResolutionEvent 持久化（JSONL append-only）
- `audit/event_chain.py`：**端到端审计事件链**（caller/capability/permission/provider/sandbox/result/timestamp）—— R6.2
- `rbac.py`：**企业级 RBAC 策略引擎**（Principal→Role→Scope→Policy→Audit）—— R6.3
- `isolation.py`：**工业级隔离策略层**（CPU/内存/超时/进程/seccomp + 资源记账）—— R6.4
- `evidence.py`：**行为证据系统**（trace→artifact→audit→replay）—— R6.5

### Runtime 层（anima/runtime）
- `capability_resolver.py`：Line B Resolver（候选评分选择 + 审计）
- `providers/`：provider 实现（python_exec 等）

### 工具层（anima/tools）
- `sandbox.py`：沙箱（SandboxTier 分级、bwrap/unshare 隔离、RLIMIT/cgroup 资源限制）
- `__init__.py`：ToolRegistry / call_capability / call context（可信身份源）

## 依赖方向纪律

- governance 包**不得在模块顶层 import anima.runtime / anima.tools**（避免循环依赖）；
  provider 以鸭子类型注入，运行时只按属性访问。
- R6.3/R6.4/R6.5 模块均为**加性新增独立模块**，不修改核心，由未来接线点注入。

## 安全边界（5 条不可违反）

1. Fail Closed —— 异常 DENY
2. No Hidden Authority —— 禁止隐藏管理员权限
3. Identity Cannot Be Forged —— 身份必须来自 trusted context（call context 的 user_id）
4. Audit Everything —— 所有 ALLOW/DENY/CHANGE/DEPLOY 必须记录
5. Production Is Sacred —— 未经测试/验证/批准禁止修改生产