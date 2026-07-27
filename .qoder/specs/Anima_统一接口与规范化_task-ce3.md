# Anima-next → OpenClaw 式智能 Agent 演进计划

## 设计原则

- **双路径架构**：普通群聊消息走现有 Pipeline（permission→memory→cognition→generation→guardrail），Owner 的任务指令进入新 Agent 执行路径。两条路径共享 LLM/工具/权限基础设施，但执行逻辑完全隔离。
- **渐进增强**：每个 Phase 独立可部署、可回滚。改完即 `pytest` + `_deploy.py`，不积攒大爆炸合并。
- **安全分层**：普通用户（level 2）永远无法触发 Agent 路径；Admin（level 3）可用增强沙箱；Owner（level 4）可用完整 Agent 能力。
- **现有功能零回归**：每个 Phase 完成后必须通过全量 732+ 测试 + VPS 5 分钟无错误运行验证。

---

## Phase 1：增强沙箱（Owner/Admin 分级解锁）

### 目标
让 `run_python` 对 Owner 开放网络、延长超时、放开受限目录读写，同时普通用户仍走原有严格沙箱。

### 改动文件
- `anima/tools/sandbox.py` — 核心改动
- `anima/tools/__init__.py` — `_invoke` 传递权限等级
- `anima/core/config.py` — 新增 `AgentSandboxConfig`

### 具体方案

**1.1 权限感知的沙箱配置**

```python
# anima/tools/sandbox.py 新增
@dataclass
class SandboxTier:
    """按权限等级分层的沙箱能力。"""
    network: bool = False
    timeout: int = 10
    memory_mb: int = 128
    writable_dirs: list[str] = field(default_factory=list)
    blocked_imports: set[str] = field(default_factory=lambda: {"subprocess", "socket", "requests", "ctypes", "multiprocessing"})

SANDBOX_TIERS = {
    2: SandboxTier(),  # 普通用户：现状不变
    3: SandboxTier(timeout=30, memory_mb=256),  # Admin：延长超时+内存
    4: SandboxTier(  # Owner：全能力
        network=True,
        timeout=120,
        memory_mb=512,
        writable_dirs=["data/agent_workspace"],
        blocked_imports={"ctypes", "multiprocessing"},  # 仅保留最危险阻止
    ),
}
```

**1.2 `run_python` 接受权限参数**

```python
async def run_python(code: str, timeout: int = DEFAULT_TIMEOUT,
                     input_images_b64: list[str] | None = None,
                     permission_level: int = 2) -> str:
    tier = SANDBOX_TIERS.get(min(permission_level, 4), SANDBOX_TIERS[2])
    config = SandboxConfig(
        memory_mb=tier.memory_mb,
        timeout=min(timeout, tier.timeout),
        network=tier.network,
        writable_dirs=[...] + tier.writable_dirs,
    )
    # import 守卫也按 tier 调整
```

**1.3 `_invoke` 传递权限**

在 `anima/tools/__init__.py` 的 `_invoke` 方法中，将 `permission_level` 注入 `run_python` 的 args：

```python
if tool.name == "run_python":
    ctx = get_call_context()
    args = {**args, "permission_level": ctx.get("permission_level", 2)}
```

**1.4 Agent 工作目录**

VPS 上创建 `/opt/anima/data/agent_workspace/`，Owner 沙箱可读写此目录。输出文件自动 755/644 权限。

### 兼容性保证
- `permission_level=2`（默认）走完全相同的旧路径，行为不变
- 现有 44 个 tools 测试 + 沙箱测试不受影响
- 群聊中普通用户触发的 `run_python` 仍然无网络/10s/128MB

### 验证
- 新增测试：`test_sandbox_tier_escalation.py`（验证不同 level 的配置差异）
- 现有测试全量通过
- VPS 部署后 Owner 发消息 "用python请求一下httpbin.org/get" 验证网络可用

---

## Phase 2：通用工具层

### 目标
为 Agent 路径补充文件操作、HTTP 请求、目录浏览等通用工具，均受权限门控。

### 新增文件
- `anima/tools/builtin/agent_tools.py` — Owner 专属通用工具

### 工具清单

| 工具名 | 权限 | 功能 |
|--------|------|------|
| `agent_http_request` | OWNER | 通用 HTTP 请求（GET/POST），返回 status+body（截断 8KB） |
| `agent_read_file` | OWNER | 读取 agent_workspace 内文件内容 |
| `agent_write_file` | OWNER | 写入 agent_workspace 内文件 |
| `agent_list_dir` | ADMIN+ | 列出指定目录（限定 /opt/anima 下） |
| `agent_exec` | OWNER | 在 agent_workspace 中执行任意 shell（有网络，60s 超时） |
| `agent_browser` | OWNER | Playwright 无头浏览器（navigate/click/screenshot/extract） |

### 安全约束
- 所有路径操作限定在 `data/agent_workspace/` 白名单内（路径遍历防护：`resolve()` 后检查前缀）
- `agent_exec` 禁止 `rm -rf /`、`dd`、`mkfs` 等破坏性命令（正则黑名单 + 只允许在 workspace 内操作）
- `agent_browser` 使用 Playwright chromium sandbox 模式，禁止 `file://` 协议
- 每个工具调用写入 `selfcode_audit.json` 审计日志

### 注册方式
在 `anima/tools/builtin/__init__.py` 中条件注册：

```python
# --- Agent 专属工具（仅 Owner 可见）---
from .agent_tools import register_agent_tools
register_agent_tools(registry, core)
```

工具 `min_level=LEVEL_OWNER`，普通用户 `get_schemas()` 时根本看不到这些工具。

### 兼容性保证
- 新工具全部 `min_level=LEVEL_OWNER`，不影响现有工具列表
- 不修改任何现有工具的签名或行为
- `get_schemas(permission_level=2)` 返回结果与改动前完全一致

### 验证
- 新增测试：`test_agent_tools.py`（路径遍历防护、权限门控、HTTP mock）
- 现有测试全量通过
- VPS 验证：Owner 发 "帮我查一下 httpbin.org/ip 返回什么"

---

## Phase 3：任务规划引擎

### 目标
支持多步任务分解、跨消息追踪、后台异步执行。Owner 说 "帮我做 X" 时，bot 能拆成子步骤逐一执行。

### 新增文件
- `anima/agent/planner.py` — 任务规划器
- `anima/agent/executor.py` — 异步执行器
- `anima/agent/state.py` — 任务状态持久化

### 核心设计

**3.1 任务生命周期**

```
Owner 消息 → 意图识别（是任务还是闲聊？）
  → 闲聊：走现有 Pipeline（不变）
  → 任务：进入 TaskPlanner
    → LLM 分解为 steps[]
    → 逐步执行（每步调用工具）
    → 中间结果持久化
    → 全部完成 → 汇总回复 Owner
    → 失败 → 记录原因 + 通知 Owner
```

**3.2 数据结构**

```python
@dataclass
class AgentTask:
    id: str
    request: str          # Owner 原始请求
    creator: int          # Owner QQ 号
    status: str           # planning|executing|completed|failed|cancelled
    steps: list[Step]     # 分解后的步骤
    current_step: int = 0
    context: dict = field(default_factory=dict)  # 步骤间共享上下文
    created_at: float
    updated_at: float

@dataclass
class Step:
    description: str
    tool_name: str        # 要调用的工具
    tool_args: dict
    status: str = "pending"  # pending|running|done|failed|skipped
    result: str = ""
    error: str = ""
```

**3.3 持久化**

- 存储：`data/agent_tasks.json`（复用 `PersistenceProvider`）
- 正在执行的任务在进程重启后恢复（status=executing → 从 current_step 继续）
- 已完成任务保留最近 100 条

**3.4 意图识别（任务 vs 闲聊）**

在 `generation_stage` 之前新增轻量判定（不改变现有 Pipeline 结构）：

```python
# anima/agent/intent.py
TASK_TRIGGER_RE = re.compile(
    r"(帮我|请|麻烦|去|给我).{0,20}(做|写|查|搜|下载|安装|部署|运行|执行|处理|分析|生成|创建|搭建|配置)",
    re.I,
)

def is_agent_task(text: str, permission_level: int) -> bool:
    """仅 Owner + 明确任务意图才进入 Agent 路径。"""
    return permission_level >= 4 and bool(TASK_TRIGGER_RE.search(text or ""))
```

**关键**：这个判定只在 `permission_level >= 4` 时生效。普通用户的消息永远走旧路径。

### 兼容性保证
- 意图识别是**加法**：不匹配时完全走旧路径，零影响
- 不修改 `pipeline/stages/` 中任何现有阶段的逻辑
- 任务引擎是独立模块，通过 `handler.py` 中的分支调用

### 验证
- 新增测试：`test_agent_planner.py`（任务分解、状态机、持久化恢复）
- 现有测试全量通过
- VPS 验证：Owner 发 "帮我搜索一下2026年奥运会举办地，然后把结果写入文件"

---

## Phase 4：Agent 执行循环

### 目标
将 ReAct 从 5 轮反应式升级为 Plan→Execute→Verify 无限轮次规划循环（仅 Agent 路径）。

### 改动文件
- `anima/agent/executor.py` — 核心执行循环
- `anima/agent/react.py` — 保留不变（群聊仍用）
- `anima/llm/handler.py` — 入站分支（任务 → Agent 路径）

### 核心设计

**4.1 执行循环（与现有 ReAct 并存）**

```python
class AgentExecutor:
    """Owner 专属的无限轮次 Agent 执行器。"""
    
    MAX_STEPS = 20          # 单任务最大步骤数（防无限循环）
    MAX_TOOL_RETRIES = 3    # 单步工具失败最大重试
    STEP_TIMEOUT = 180      # 单步最大执行时间（秒）
    
    async def execute(self, task: AgentTask) -> str:
        for step in task.steps[task.current_step:]:
            # 1. 执行工具
            result = await self._execute_step(step, task.context)
            # 2. 验证结果
            if not self._verify(step, result):
                # 重试或标记失败
                ...
            # 3. 更新上下文（步骤间传递信息）
            task.context[f"step_{step.index}_result"] = result
            # 4. 持久化进度
            self._save(task)
            # 5. 可选：LLM 动态调整后续步骤
            if self._should_replan(task):
                task.steps = await self._replan(task)
        return self._summarize(task)
```

**4.2 与现有 ReAct 的关系**

| 维度 | 现有 ReAct（群聊） | Agent Executor（Owner 任务） |
|------|-------------------|---------------------------|
| 触发 | 每条群聊消息 | Owner 明确任务指令 |
| 轮次 | 最多 5 轮 | 最多 20 步 |
| 工具 | 权限过滤后的全部工具 | 全部工具 + Agent 专属工具 |
| 沙箱 | 按权限分层 | Owner 全能力沙箱 |
| 持久化 | 无（单消息内） | 跨消息、跨重启 |
| 错误处理 | 返回错误文本给 LLM | 自动重试 + 替代路径 + 回滚 |
| 输出 | 直接回复 | 进度通知 + 最终汇总 |

**4.3 进度通知**

长任务执行中，通过 QQ 给 Owner 发进度：
```
[1/5] 正在搜索 httpbin.org...
[2/5] 搜索完成，正在解析结果...
[3/5] 正在写入文件...
[完成] 结果已保存到 agent_workspace/result.txt
```

### 兼容性保证
- `ReActLoop` 完全不修改，群聊路径零影响
- Agent Executor 是独立类，只在 `is_agent_task()` 为 True 时调用
- 共享 `ToolRegistry.call()` 基础设施，不重复实现工具执行

### 验证
- 新增测试：`test_agent_executor.py`（多步执行、失败重试、replan、超时）
- 现有测试全量通过
- VPS 验证：Owner 发 "帮我写一个Python脚本计算斐波那契数列前20项，运行它，把结果发给我"

---

## Phase 5：安全集成与 WebUI

### 目标
审计、回滚、WebUI 可视化、紧急停止。

### 改动文件
- `anima/webui/routes/agent.py` — 新增 Agent 面板 API
- `anima/agent/audit.py` — Agent 操作审计
- `anima/agent/guardrails.py` — Agent 安全护栏

### 核心设计

**5.1 审计日志**

每次 Agent 工具调用记录到 `data/agent_audit.jsonl`：
```json
{"ts": 1722067200, "task_id": "abc123", "step": 2, "tool": "agent_http_request", 
 "args_digest": "sha256:...", "result_len": 1024, "ok": true, "duration_ms": 320}
```

**5.2 安全护栏**

```python
# anima/agent/guardrails.py
AGENT_BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"dd\s+if=",
    r"mkfs",
    r":\(\)\{.*\};:",  # fork bomb
    r"chmod\s+777\s+/",
]

AGENT_RESOURCE_LIMITS = {
    "max_disk_write_mb": 100,      # 单任务最大磁盘写入
    "max_network_requests": 50,    # 单任务最大网络请求数
    "max_total_duration_sec": 600, # 单任务最大总时长
    "max_output_size_kb": 512,     # 单步最大输出
}
```

**5.3 紧急停止**

- QQ 命令：Owner 发 "停止任务" / "cancel" → 立即终止当前 Agent 任务
- WebUI 按钮：Agent 面板的 "紧急停止" 按钮
- 自动熔断：连续 3 个任务失败 → 暂停 Agent 能力 10 分钟

**5.4 WebUI Agent 面板**

新增 `/api/agent/tasks` 端点：
- GET：列出所有 Agent 任务（状态/进度/耗时）
- POST /cancel：取消指定任务
- GET /audit：查看审计日志
- 前端：在 WebUI 新增 "Agent" 标签页（任务列表 + 实时进度 + 审计回放）

### 兼容性保证
- WebUI 新增路由不影响现有路由
- 审计是纯追加写入，不修改现有数据结构
- 护栏只限制 Agent 路径，不影响群聊工具调用

### 验证
- 新增测试：`test_agent_guardrails.py`（资源限制、黑名单、熔断）
- WebUI 构建：`cd webui && npm run build`
- 现有测试全量通过
- VPS 全链路验证

---

## 实施顺序与依赖

```
Phase 1 (沙箱增强)
    ↓
Phase 2 (通用工具) ← 依赖 Phase 1 的分级沙箱
    ↓
Phase 3 (任务引擎) ← 依赖 Phase 2 的工具集
    ↓
Phase 4 (执行循环) ← 依赖 Phase 3 的任务结构
    ↓
Phase 5 (安全+UI) ← 依赖 Phase 4 的执行器
```

每个 Phase 完成后：
1. `py -m pytest tests/ -q` 全量通过
2. `git commit` + `py _deploy.py` 部署
3. VPS 5 分钟无错误运行
4. Owner 在 QQ 中实测新功能

---

## 不改动的部分（红线）

| 模块 | 保证 |
|------|------|
| `pipeline/stages/permission.py` | 权限判定逻辑不变 |
| `pipeline/stages/cognition.py` | 人格 prompt 不变 |
| `pipeline/stages/generation.py` | 群聊 ReAct 路径不变 |
| `pipeline/stages/guardrail.py` | 输出护栏不变 |
| `cognitive/*` | 所有认知模块不变 |
| `channels/onebot11.py` | 渠道层不变 |
| `tools/builtin/sticker.py` | 表情系统不变 |
| `tools/builtin/social.py` | 群管理工具不变 |
| SelfCode 双 Agent | 审批流程不变 |
| WebUI 现有面板 | 全部保留，只新增 Agent 面板 |

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| Owner 沙箱网络被滥用 | 审计日志 + 资源配额 + 熔断 |
| Agent 任务阻塞主事件循环 | asyncio.create_task 后台执行 + 信号量限流（最多 2 个并发任务） |
| 长任务进程重启丢失 | 每步持久化 + 重启恢复 |
| LLM 分解出危险步骤 | 护栏黑名单 + 每步执行前校验 |
| 普通用户触发 Agent 路径 | 双重门控：permission_level >= 4 AND 意图匹配 |
