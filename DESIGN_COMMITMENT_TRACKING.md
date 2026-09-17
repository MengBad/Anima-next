> [HISTORICAL] 本文档停更于 2026-07-29，仅作历史参考；现状以 README.md / MAINTENANCE.md / schemas/config.schema.yaml 为准。
>
> ⚠️ **2026-09-18 改名**：原名 `DESIGN_DEBT_TRACKING.md`。改名原因：本文档写的是
> **「承诺追踪系统」** —— 让 bot 记得自己欠了谁什么（**人际承诺**），属于功能设计；
> 而名字里的 "debt" 会被读成**工程债**。工程债台账是另一份：
> [`ENGINEERING_DEBT_LEDGER.md`](ENGINEERING_DEBT_LEDGER.md)。两者无关。
> 实现位于 `anima/cognitive/debt.py`，开关为 `debt_tracking.enabled`。

# 【设计文档】承诺追踪系统（进化2 · debt）

> 目标：让 bot 从"答完就忘"进化成"说话算数、记得自己欠了谁什么"的数字生命。
> 三支柱：**抽取**（用户说"我帮你做X"→ 记一笔债）、**自我感知**（bot 在对话中"记得"自己欠债）、
> **主动履约**（到点/被问时主动提醒或推进）。
> 设计原则：**复用已有骨架**——抽取挂在 `handler._extract_and_store` 同款后台任务；自我感知走
> `cognition_stage` 的 `add_system_fragment`；提醒直接复用 autonomy 的 `propose_agenda` 主动发言
> 管线（天然继承 `daily_max`/`cooldown`/`active_daily_max_per_group` 防骚扰）。

---

## 0. 现状复用点（必须先看）

| 资产 | 位置 | 复用方式 |
|---|---|---|
| 后台抽取任务 | `llm/handler.py:881-885` `asyncio.create_task(self._extract_and_store(...))` | 同函数内追加 `await self._extract_debt(...)`（同非阻塞、同静默降级） |
| 事实抽取器 | `crossgroup/extraction.py` `extract_user_facts`（工具调用 + fallback） | 参考其"关键词预筛→LLM 结构化"模式做 debt 版 |
| 认知注入 | `pipeline/stages/cognition.py:245` `add_system_fragment(key, content, priority)` + `ContextComposer` 预算器 | 注入 `debt` 片段（priority≈50），自动受 token 预算保护 |
| 主动发言管线 | `core/autonomy.py` `propose_agenda(group_id, text, priority, source, expires_at)` | 到点提醒时调 `propose_agenda`，继承 `daily_max=20`/`cooldown=90s`/`active_daily_max_per_group=3` |
| 持久化 | `core/persistence.py` `PersistenceProvider`（原子 temp+os.replace） | `DebtManager` 同款落盘 `data/debts.json` |

---

## 1. 提取策略：关键词 vs LLM，触发条件

**决策：两层漏斗**（与现有 `extraction.py` 一致，零成本预筛 + LLM 结构化）。

### 1.1 关键词预筛（零 LLM 成本）
在 `_extract_and_store` 内、事实抽取之后，对 `text` 做轻量正则命中：
```
承诺触发词：答应|保证|我帮你|我给你|记得帮我|别忘了|提醒我|待会|等下给你|
           我来做|包在我身上|下周前|明天前|周末前|稍后发|回去就|一定
完成/取消词（用于完成信号，见 §4）：搞定了|弄完了|已经发|发你了|做完了|
           不用了|算了|取消|搞不了|做不到
```
- 命中"承诺触发词" → 进入 LLM 结构化抽取（**仅在此时烧一次 LLM**）。
- 未命中 → 直接 return，零开销（保证不拖慢正常回复）。

### 1.2 LLM 结构化抽取（仅触发时）
仿 `EXTRACT_TOOLS` 定义 `extract_promise` 工具，schema：
```json
{
  "name": "record_promise",
  "parameters": {
    "properties": {
      "content":   {"type":"string", "description":"承诺做的事，简洁陈述，如「帮阿布查 B 站 up 主开播时间」"},
      "beneficiary": {"type":"string", "description":"受益人称呼（消息中出现形式），如「阿布」；无则空"},
      "due_hint":  {"type":"string", "description":"时间线索原文，如「明天」「这周末」「下周」；无则空"},
      "urgency":   {"type":"string", "enum":["low","normal","high"]}
    },
    "required": ["content"]
  }
}
```
- `temperature=0, max_tokens=160`，与事实抽取同款低成本。
- 任何异常/空返回 → 静默丢弃，**绝不抛、绝不阻塞**。

### 1.3 时间解析
LLM 返回 `due_hint` 文本后，二次轻量解析为 `due_ts`（Unix 时间戳）或留 `due_hint` 原文：
- "明天/后天" → `now + N*86400`；"这周末" → 最近一个周六 20:00；"下周X" → 解析为具体日期。
- 解析失败（如"下次""有空"）→ `due_ts=None`，`due_hint` 保留原文，标记 `fuzzy=True`，
  走"模糊提醒"策略（见 §3.3），不硬算时间戳。

---

## 2. 存储模型：Debt 字段 + 状态机

新增 `cognitive/debt.py` → `DebtManager`（单例 + `get_debt_manager(data_dir)`）。

### 2.1 Debt 数据类
```python
@dataclass
class Debt:
    id: str                      # uuid4 hex
    owner_id: int               # 承诺方：永远是 bot 自身（哈基米），预留字段
    beneficiary: str            # 受益人称呼（原文），"群友"/空→"大家"
    beneficiary_uids: list[int] # 解析后的 QQ 号（find_mentioned 反查），可空
    content: str                # 承诺内容
    source_group: str           # 承诺发生的群（提醒默认回此群）
    source_user: str            # 谁让 bot 承诺的（发起人 QQ 号 str）
    created_at: float
    due_ts: float | None       # 明确时间；None=模糊
    due_hint: str               # 模糊时间原文
    fuzzy: bool = False
    urgency: str = "normal"     # low|normal|high
    status: str = "pending"     # pending|reminded|completed|cancelled|expired
    reminded_count: int = 0
    last_reminded_at: float = 0.0
    completed_at: float = 0.0
```

### 2.2 状态机
```
                 ┌─────────┐
   创建 ───────► │ pending │ ◄──────────────┐
                 └────┬────┘                │
           到点/被问   │ 提醒一次            │ 用户说"搞定/不用了"
                      ▼                     │
                 ┌─────────┐                │
                 │ reminded │ ─── 再提醒 ────┘ (最多 reminded_max 次)
                 └────┬────┘
           ┌──────────┼───────────────┬──────────────┐
           ▼          ▼               ▼              ▼
      ┌─────────┐ ┌─────────┐   ┌─────────┐    ┌─────────┐
      │completed│ │cancelled│   │ expired │    │(回到pending│
      │ (履约)  │ │ (取消)  │   │ (超时)  │    │ 若仍未到期)│
      └─────────┘ └─────────┘   └─────────┘    └─────────┘
```
- `pending → reminded`：触发提醒后置（保留 pending 语义，用 `reminded_count` 记次数）。
- 终态：`completed` / `cancelled` / `expired`——入终态后不再提醒。
- `expired`：有明确 `due_ts` 且 `now > due_ts + grace_hours`（默认 48h）仍未 completed → 自动 expired。

### 2.3 持久化
`data/debts.json`：`{"debts": [...], "last_run": 0.0}`，`PersistenceProvider.write_json` 原子落盘。
环形裁剪：保留最近 200 条（含历史终态，供复盘/统计），超出删最旧。

---

## 3. 提醒策略：主动发言时机 + 防骚扰

### 3.1 触发检查（挂心跳，非阻塞）
在 `core/autonomy.py` 的 `_life_checkpoints`（或独立 10-min scheduler tick）末尾追加：
```python
await debt_manager.check_due(
    core=self.core,
    now=time.time(),
    propose_agenda=self.core.propose_agenda,
)
```
`check_due` 逻辑：
1. 遍历 `status in (pending, reminded)` 且未到期的债。
2. 明确时间：`now >= due_ts - remind_lead`（提前 1h 提醒）→ 触发。
3. 模糊时间：`fuzzy=True` → 按 `urgency` 给一个软窗口（normal=24h、high=6h、low=72h 后首次提醒），之后每 `remind_interval`（默认 24h）再提醒，直到 `reminded_max`（默认 3）次或终态。
4. 触发时调 `propose_agenda(group_id=source_group, text=提醒文案, priority=0.85, source=f"debt:{id}", expires_at=now+6*3600)`。

### 3.2 文案生成（口吻自然，不爆 AI 感）
> 例：「诶，本喵之前说帮 {beneficiary} 查 {content} 的，还没弄呢，待会搞一下喵」
> 高紧急：「那个 {content} 本喵答应 {beneficiary} 的，今天得给人家了，别鸽」

文案在 `propose_agenda` 后由 autonomy 主动发言管线发出（继承猫娘口吻、关系分寸、话题纪律护栏），
**不另写发送逻辑**，避免破坏既有人格护栏。

### 3.3 防骚扰（关键，复用 autonomy 既有限流）
- **不自建限流**：提醒经 `propose_agenda` → autonomy 主动发言管线，自动受
  `daily_max=20`（全局）、`active_daily_max_per_group=3`（每群）、`cooldown_sec=90`、
  `active_global_cooldown_sec=1800` 约束。**债务提醒只是 agenda 的一种 source，天然不超限。**
- 债自身再保险：`reminded_max`（默认 3）次后停提醒，转 `expired` 或留待用户问起。
- 同一债 30 分钟内不重复提醒（`last_reminded_at` gate）。

---

## 4. 自我感知：让 bot "记得"自己欠了债

### 4.1 认知注入（对话中可见）
在 `cognition_stage` 末尾、注入跨群记忆的同区块（`cognition.py` ~line 496 附近）追加：
```python
from anima.cognitive.debt import get_debt_manager
dm = get_debt_manager(data_dir)
pending = dm.pending_for(user_id=str(ctx.user_id), group_id=ctx.group_id)
if pending:
    frag = "## 你欠下的承诺（对方问起/相关话题时自然提起，别硬凑）\n" + "\n".join(
        f"- 答应{p.beneficiary}：{p.content}" + (f"（约{p.due_hint}）" if p.due_hint else "")
        for p in pending[:3]
    )
    add_system_fragment("debt_self_aware", frag, 50)  # priority 50，受预算器保护
```
- 效果：用户说"你之前说帮我查的咋样了"，bot 因 system 里有这笔债，**自然接得住**而不失忆。
- 受 `ContextComposer` token 预算约束：债多时自动降级，不挤占人格/关系等高分片段。

### 4.2 完成信号（handler 落锤）
在 `handler._extract_and_store` 同款后台任务中，若命中"完成/取消词"（§1.1）且当前用户/群有
`pending/reminded` 债 → 轻量匹配：
- 关键词匹配（content 含债的片段）+ 同 (user, group) 范围 → 标 `completed`/`cancelled`，
  写 `completed_at`，发一条释然短回（"哦对，那个 {content} 你说搞定了是吧，本喵记下了喵"）。
- 模糊时（多笔可能匹配）→ 调一次 LLM 判"这句话是否在宣告某笔债完成"，避免误标。
- 误标保护：completed 后仍保留原文 30 天，用户说"没啊还没"可 `DebtManager.reopen(id)` 回 `pending`。

---

## 5. 边界情况

| 场景 | 处理 |
|---|---|
| 时间模糊（"下次"/"有空"） | `due_ts=None, fuzzy=True`，走软窗口提醒（§3.1.3），不硬算时间；认知片段仍显示原文 `due_hint` |
| 多受益人（"帮我们查"） | `beneficiary="大家"`，`beneficiary_uids` 填被 @ 的多个 QQ；提醒文案用"大家" |
| 跨群承诺 | 债 `source_group`=承诺发生群；提醒默认回该群。`pending_for(user_id, group_id=None)` 支持跨群查询——用户在任意群说话，bot 都"知道自己欠 ta 的" |
| 自言自语式承诺（"本喵明天要写周报"） | `beneficiary="自己"`，仍追踪，到点轻提醒（不对外群发，仅在有上下文时提） |
| 重复承诺（同 content 短时间内再说一次） | `DebtManager.add` 做去重：`(content, beneficiary, source_group)` 近 1h 内已有 pending → 合并，不重复记 |
| 用户开玩笑（"你答应嫁给我"） | LLM 抽取层可标 `urgency=low` 且 content 明显非任务类 → 不入库（过滤调侃/不可能履约项） |
| 债太多（>30 笔 pending） | 认知注入封顶 3 条；提醒受 `daily_max` 限；超出部分仅存档，靠用户问起才提 |

---

## 6. 测试策略：模拟时间流逝验证提醒

放 `tests/test_debt_tracking.py`，用**假时钟**驱动，不依赖真实 LLM：

| 测试 | 验证点 | 手法 |
|---|---|---|
| `test_extract_keyword_gate_skips_llm` | 无触发词不调 LLM | mock LLM 断言未被调用 |
| `test_extract_records_promise` | 命中→记债 | fake extract 返回 promise → 断言 `debts.json` 新增 pending |
| `test_due_time_triggers_reminder` | 到点提醒 | 构造 `due_ts=now+1h`，advance fake clock 1h → `check_due` 调 `propose_agenda`（mock 断言） |
| `test_fuzzy_due_soft_window` | 模糊时间软窗口 | `fuzzy=True`，advance 24h → 触发；advance 23h → 不触发 |
| `test_reminded_max_stops` | 提醒上限 | 连推 4 次 → `reminded_count==3` 后转 expired，第 4 次不再 `propose_agenda` |
| `test_completion_signal_marks_done` | 完成信号 | 用户发"搞定了" → 匹配 pending → `status==completed` |
| `test_cancel_signal` | 取消信号 | 用户发"不用了" → `status==cancelled` |
| `test_reopen_after_mistake` | 误标回滚 | completed 后 `reopen(id)` → 回 pending |
| `test_daily_max_inherited` | 防骚扰复用 | 单 tick 推 50 笔到期债 → `propose_agenda` 调用次数 ≤ autonomy `daily_max`（mock 计数） |
| `test_cognition_injects_pending` | 自我感知 | `pending_for` 返回 2 笔 → `add_system_fragment("debt_self_aware", ...)` 被调用且内容含债 |
| `test_cross_group_promise` | 跨群 | 群A承诺、群B对话 → `pending_for(user_id, group_id=None)` 仍命中 |
| `test_atomic_persist_on_error` | 落盘安全 | `write_json` 抛异常 → 旧 `debts.json` 不变、异常被吞 |

**时间模拟**：`check_due(now=...)` 接收 `now` 参数（不读 `time.time()`），测试注入确定性时间戳；
`scheduler` 集成测试用 `unittest.mock.patch("time.time", return_value=...)` 推进。

---

## 7. 配置开关（延续"默认关、显式开"）

config 新增 `debt_tracking:`：
```yaml
debt_tracking:
  enabled: true
  keyword_only_mode: false     # true=仅关键词，不做 LLM 抽取（极省 token）
  remind_lead_sec: 3600        # 明确时间提前 1h 提醒
  reminded_max: 3
  soft_window: {low: 72h, normal: 24h, high: 6h}
  remind_interval_sec: 86400
  grace_hours: 48              # 超时宽限
  max_pending_per_user: 30
  inject_to_cognition: true   # 自我感知开关
```
关闭时：`_extract_debt` 直接 return，`check_due` return，认知不注入——完全退化为无债状态。

---

## 8. 文件改动清单（实现阶段用，本设计文档不含代码）

| 文件 | 改动 |
|---|---|
| `cognitive/debt.py` | **新增**：`Debt` / `DebtManager` / `get_debt_manager()` / `check_due()` / `pending_for()` / `reopen()` |
| `llm/handler.py` | `_extract_and_store` 内追加 `_extract_debt`（同后台任务、同静默降级）；完成/取消信号匹配 |
| `pipeline/stages/cognition.py` | ~line 496 区块追加 `debt_self_aware` 片段注入 |
| `core/autonomy.py` | `_life_checkpoints` 末尾挂 `debt_manager.check_due(core=..., propose_agenda=...)` |
| `crossgroup/extraction.py` | 可选：复用其关键词预筛范式（不强制改） |
| `core/config.py` | 新增 `DebtTrackingConfig` + schema |
| `tests/test_debt_tracking.py` | 上述 12 项单测 |

---
*设计基线：Anima-next 生产路径 `G:/Anima-next`，部署于 VPS `/opt/anima`。*

---

## 9. 实现状态（2026-09-15：已实现，含偏差）

新增 `anima/cognitive/debt.py`（`Debt` / `DebtManager` / `get_debt_manager` / `parse_due_hint`），
测试见 `tests/test_debt_tracking.py`（21 项）。开关 `config.debt_tracking.enabled`（默认 true）。

**实现偏差（相对本设计）**

| # | 设计原文 | 实际实现 | 原因 |
|---|---|---|---|
| 1 | §1.2 命中触发词后走 LLM 结构化抽取 | **不做 LLM**（`keyword_only_mode` 默认 true），直接用既有 agenda 的 intent 原文记账 | 零成本、零延迟；保留 `keyword_only_mode=false` 的扩展位 |
| 2 | §8 在 `llm/handler.py:_extract_and_store` 追加 `_extract_debt` | 改为挂在 `pipeline/stages/_shared.py:_detect_promise_and_schedule` | 后者已有一套承诺触发正则并在建 agenda；再开一条抽取路径就是双轨（设计 §0 明令避免）。现在：触发只有一处，agenda 管「何时说」，debt 管「欠什么、还了没」 |
| 3 | §3 提醒文案生成 | 复用 `propose_agenda` 主动管线，继承限流/冷却/quiet_hours | 不新造主动发言通道 |

**未实现**：`max_pending_per_user` 上限、`soft_window` 分级窗口、`beneficiary_uids` 反查 QQ 号。
