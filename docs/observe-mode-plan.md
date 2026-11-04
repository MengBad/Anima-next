# 执行代理改造方案：从「计划驱动」到「观察驱动」

> 目标：让 owner 私聊的 Agent 任务真正能做完（对齐 Memoh 的 runtime 骨架）。
> 硬约束：**群聊行为逐字不变**（tests/test_group_behavior_frozen.py 是锁）。
> 状态：**方案待你批准，未写一行代码。**

---

## 一、先承认现状的问题

用生产数据说话（/opt/anima/data/agent_tasks.json）：

| 任务 | 总耗时 | 步数 | replan | 第 2 步实际调用的工具 |
|---|---|---|---|---|
| 285f251b52 | **21 秒** | 4 | **0** | browse_credential, ask_user |
| e7d6a67518 | 77 秒 | 4 | 0 | browse_credential, ask_user |
| 01f70c6a75 | — | 4 | 0 | — |
| 13e8368520 | — | 5 | 0 | — |

**四个任务，replan_count 全是 0。** 我上一轮写的重规划提示词、failure_log 反馈机制，**一次都没被执行过**。

### 三个结构性缺陷

**1. 计划锁死，不能边看边改**

    planner 一次性产出 4 步 → executor 逐步执行 → 每步用 LLM 判 PASS/FAIL

第 2 步「打开登录页并登录」在**没有打开过浏览器**的情况下，因为 browse_credential 返回「没配置」就判失败。计划里没有「先看看页面有什么」这一步，它也不会自己加。

**2. 验收器读文本，不读现实**

    prompt = f"步骤实际输出：{(step.result or '（空）')[:VERIFY_OUTPUT_CHARS]}"

验收器看到的是**字符串**。它无法判断「页面是否真的显示已登录」。而 Memoh 的验收是看截图/读文件/查响应。

**3. 预算和轮次是为「短任务」设计的**

    total_budget_sec: float = 300.0      # 注释写"实测 30-70s，留 4 倍余量"
    max_rounds=6                          # 单个 step 内 ReAct 轮次
    STEP_TIMEOUT = 180
    MAX_STEPS = 20

打开浏览器 + 登录 + 截图 + 读积分，**光浏览器冷启动就 ~4 秒**，一次完整交互 20-40 秒。300 秒的总预算连两轮试错都不够。

---

## 二、目标形态

### Memoh 的骨架（我从源码读到的）

    while 未完成 and 未超预算:
        observe   → 看当前状态（截图/页面文本/文件/命令输出）
        decide    → 模型决定下一个动作（可多个工具）
        act       → 执行
        record    → 把结果写进上下文

关键点：**没有预设步骤列表**。步数是执行的产物，不是前提。

### 改造后的 Anima

    AgentTask.goal = "执行华为云积分任务"
            ↓
    ┌─────────────────────────────────────┐
    │  loop（最多 N 轮 / T 秒）            │
    │   1. 观察：把上轮产物 + 当前状态     │
    │      拼进 prompt（含截图路径）        │
    │   2. ReAct：模型自选工具，可多轮     │
    │   3. 检查：产物是否满足完成判据？     │
    │      - 有截图文件？读得到？          │
    │      - HTTP 200？内容含关键词？      │
    │      - 文件存在且非空？              │
    │   4. 满足 → 交付；不满足 → 继续      │
    └─────────────────────────────────────┘
            ↓
    交付：文本总结 + 媒体（截图）发到私聊

---

## 三、改哪些文件

### 新增

**anima/agent/loop.py**（~350 行，核心）

    @dataclass
    class Observation:
        """一轮观察到的现实。"""
        kind: str          # "screenshot" | "text" | "http" | "file" | "tool_log"
        path: str = ""     # 截图/文件绝对路径
        content: str = ""  # 文本内容（截断）
        ok: bool = True

    class ObserveLoop:
        async def run(self, task, *, deadline) -> str: ...
        def _collect_observations(self, task) -> list[Observation]: ...
        async def _check_completion(self, task, obs) -> tuple[bool, str]: ...

**anima/agent/artifact.py**（~120 行）

产物的**真实**校验，替代 LLM 猜：

    def verify_artifact(kind: str, value: str, *, expect: str = "") -> tuple[bool, str]:
        """截图存在且 >1KB？文件非空？HTTP 响应含关键词？"""

### 修改

**anima/agent/executor.py**

| 改动 | 说明 |
|---|---|
| 新增 execute_observe_mode() | 新入口，不动 execute() 现有路径 |
| execute() 开头分流 | if task.context.get("mode") == "observe": return await self.execute_observe_mode(task) |
| 保留 _verify_step / _replan_or_give_up | 旧任务（磁盘上已有 steps）继续走老路，不破坏兼容 |

**关键：加法，不是替换。** 旧任务一行不动，新任务走新路。这样 agent_tasks.json 里现有 46 条任务不会变成孤儿。

**anima/agent/planner.py**

新增 plan_goal()：不再产出 4 个步骤，而是产出**完成判据**。

    {"goal": "...", "done_when": ["积分页面截图已保存", "页面文本含我的积分"]}

**anima/core/config.py**

    class TaskAgentConfig(BaseModel):
        # ... 现有字段不动 ...
        observe_mode: bool = True          # 新任务走观察驱动
        observe_max_rounds: int = 40
        observe_budget_sec: float = 1800.0
        observe_step_timeout: float = 300.0

---

## 四、群聊不受影响的三重保证

**1. 入口隔离（已有）**

    # handler.py:1226 —— test_group_behavior_frozen.py:25 锁的就是这一行
    if detail_type == "private" and is_agent_task(text):
        await self.core.agent.submit(...)

群聊永远进不了 agent.submit，也就永远到不了 executor。

**2. 工具隔离（已有）**

    Tool(..., private_only=True)   # browse_web_act / browse_credential / ask_user

**3. 测试锁（现有 + 新增）**

- test_group_behavior_frozen.py —— 一条不改，跑通即证明群聊没动
- 新增 test_observe_loop.py —— 新循环的单元测试，用 fake LLM + fake tools

**验收时会先跑 test_group_behavior_frozen.py，全绿才继续。**

---

## 五、分三个阶段交付（每阶段可独立回滚）

### P1｜骨架（先让它能跑，不改提示词）

- loop.py + artifact.py
- executor.execute_observe_mode()
- config.observe_mode 开关，**默认 False**，先只在你手动开启时生效
- 测试：test_observe_loop.py

**验收**：用假账号跑华为云登录（我已在生产验证过这条路），能自己走完 6 个动作并产出截图。

### P2｜观察能力

- 每轮把**上一轮截图路径**喂给模型
- 支持模型主动 screenshot 后「看」到（当前只能拿到路径，模型看不到图）
- ⚠️ 这里有个真问题：**当前模型 gemini-3-flash 支持视觉**，但 executor 从没把图传给它。P2 要把截图作为多模态输入送进去。

**验收**：它能说出「页面上此刻显示的是登录框，没有二维码」。

### P3｜判据与交付

- done_when 判据真实校验
- 完成后自动交付截图到私聊
- 默认打开 observe_mode

---

## 六、我要你先确认的 4 件事

**Q1｜P1 默认关还是开？**

我倾向 **P1 默认关**（observe_mode: false），你手动在配置里打开，先只在华为云这个任务上验证。跑顺了 P3 再默认开。

理由：新循环没有生产磨合，直接默认开会让所有任务都换引擎，出问题不好回退。

**Q2｜旧任务怎么办？**

磁盘上 46 条任务有 steps。我的方案是「有 steps 的走旧路，没有的走新路」。你新建任务永远是新的。

要不要清掉历史任务？我倾向留着（能看历史），但如果你觉得碍眼我清。

**Q3｜预算给多少？**

建议 1800s / 40 轮。华为云完整流程估 3-5 分钟。你要更宽松我就给 3600s。

**Q4｜模型要不要换？**

gemini-3-flash 便宜但推理偏弱。Memoh 那种「看屏幕决定下一步」很吃推理。要不要给 agent 任务单独配一个更强的模型（比如 gemini-3-pro），私聊闲聊仍用 flash？

代价是贵一些。我倾向**先不换**，等 P1 跑出来看是不是模型的问题。

---

## 七、我不打算做的事

- **不抄 Memoh 代码**（AGPL-3.0，而且它是 Go）
- **不动群聊任何一行**
- **不删现有 executor 逻辑**（加法，不是重写）
- **不在没跑通 P1 之前动 P2/P3**

---

## 八、时间估计

| 阶段 | 工作量 | 说明 |
|---|---|---|
| P1 | 主要工作 | 新循环 + 产物校验 + 测试 |
| P2 | 中等 | 多模态图片输入要试通 Gemini 接口 |
| P3 | 小 | 判据落地 + 默认开关 |

每个阶段我都会：改完 → 跑全量回归 → 部署 → **生产实测**（用假账号，不碰你真实账号）→ 把结果给你看。

**P1 没在生产上真跑通之前，我不会说「好了」。**

---

请回答 Q1-Q4（或者直接说「按你建议的来」），我从 P1 开始。