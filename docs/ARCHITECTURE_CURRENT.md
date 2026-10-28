# 架构（当前实现）

> **本文档反映 2026-10-28 的真实代码架构。**
>
> 与既有文档的分工：
> - [`VISION.md`](VISION.md) —— 定位与边界（这是什么，不做什么）
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) —— Foundation 导航入口（RFC-0100 三件套）
> - **本文档** —— 实现层：代码怎么组织的、改哪里、有哪些债
> - 根目录 `ARCHITECTURE.md`（停更 2026-08-16）—— **已过期，仅作历史参考**

---

## 1. 五层单向依赖

```
channels/       QQ 接入（OneBot11 WS + HTTP）
     │
     ▼
llm/handler    对话处理器 + group_gate
pipeline/      8 阶段流水线（见 §2）
     │
     ▼
cognitive/     认知内核（71 文件）
memory/        记忆（对话/向量/长期/情节/宪法）
constitution/  RFC-0100 链
     │
     ▼
core/          AnimaCore、Gateway、Config、Persistence、Audit、autonomy、registry
```

**下层绝不 import 上层。** 违反会造成循环依赖与启动顺序死锁。

---

## 2. 消息流水线（8 阶段）

装配点在 `llm/handler.py::_build_pipeline()`：

```python
pipe = (Pipeline()
        .use(prefilter_stage)        # 睡眠/quiet_hours/群策略/忽略名单/回复闸门
        .use(attachment_text_stage)  # 附件正文提取（图片/文档/转发）
        .use(bullying_stage)         # 霸凌检测（写 ctx.meta，后面据此冷回）
        .use(permission_stage)       # 权限分级过滤
        .use(memory_stage)           # 读/写对话记忆与上下文
        .use(cognition_stage)        # 人格/时间/欲望/关系/世界观注入
        .use(generation_stage)       # ReAct + LLM 生成
        .use(guardrail_stage))       # 出站守卫（泄漏/编造/装饰）
```

**统一签名**：

```python
async def xxx_stage(ctx: PipelineContext) -> StageResult
```

`StageResult` 三态：

| 值 | 含义 |
|---|---|
| `CONTINUE` | 继续下一阶段 |
| `SKIP_REPLY` | 终止且不回复（静默） |
| `STOP` | 终止并记 `ctx.stop_reason` |

> ⚠️ 前三个阶段（`prefilter` / `attachment_text` / `bullying`）定义在**同一个**
> `prefilter.py` 里（历史原因），其余各占一个文件。
>
> ⚠️ 开启 `compat.astrbot_enabled` 时会**重建**一次 pipeline，在 cognition 与
> generation 之间插入 `hook_stage`（插件改 prompt 用）⇒ 变成 9 个阶段。
> 这段代码是 `try/except` 包着的，装配失败会回落到 8 阶段版本。

### 阶段耗时

`observability.py` 统一记录：

```
[阶段耗时] total=7716ms prefilter_stage=0.1 memory_stage=1695.2
          cognition_stage=164.3 generation_stage=4444.1 guardrail_stage=1241.8
```

**0 值照样进分布**——「快回合」是最快的一端，丢掉会让 p50 骗人。

---

## 3. 模块系统（2026-10-28 重建）

### 3.1 两套注册，各管一层

| 注册表 | 装什么 | 数量 | 位置 |
|---|---|---|---|
| `AnimaCore.modules` | 继承 `Module` 的基础设施 | **2**（`OneBot11Adapter` / `RecoveryModule`） | `core/module.py` |
| `ModuleRegistry` | 业务模块（认知子系统） | **32** | `core/registry.py` |

**为什么分两套**：`core/module.py` 的 `CognitiveModule` 契约文档写着
「所有 `anima/cognitive/` 下的子系统应继承此类」，但**实测 0 个类继承**、
`register_service()` 全库零调用 ⇒ 契约是空壳。

`ModuleRegistry` **不重写那些模块**，只加一层适配：

```
ModuleSpec（数据）  →  ModuleAdapter（鸭子类型包装）  →  统一生命周期
```

### 3.2 为什么需要适配层

改造前实测 34 个模块级 `get_xxx()`：

| 维度 | 实测 |
|---|---|
| 缺 `close`/`shutdown` | **23/34** |
| `save` 写法 | **七种**：`save` `persist` `_save` `_save_state` `_save_decay_state` `flush` `flush_deferred_if_any` |
| `close` 写法 | **四种**：`close` `_persist_daily` 模块级 `close_xxx_manager` 无 |
| 工厂签名 | **四种**：`(core)` 8 / `(data_dir)` 25 / `()` 8 / 其它 4 |

**后果已在生产暴露**：`desire_decay_state.json` 那次 37 字节残文件，
就是「模块有落盘需求但 `AnimaCore.shutdown` 里没对应调用」。

### 3.3 生命周期

```
registry.startup(core)    →  逐个 construct() + restore()
registry.persist_all()    →  逐个 persist()
registry.shutdown()       →  逆序：persist(若 dirty) → close()
```

**fail-soft**：单个模块失败只记日志（`summary()["errors"]` 可查），不阻断启动/退出。

### 3.4 四种工厂签名

| kind | 调用 | 例子 |
|---|---|---|
| `KIND_DATA_DIR` | `fn(data_dir)` | `get_mood_system` |
| `KIND_CORE` | `fn(core)` | `get_exploration_manager`（读 `core.config`） |
| `KIND_NOARG` | `fn()` | `get_quirk_system` |
| `KIND_CUSTOM` | `fn(**kwargs)`，支持 `needs_core`/`needs_data_dir` 注入 | `get_sticker_manager` |

**`core=None` 时不塞假 core** —— 标记 `deferred`，等真实 core 传入再构造。

### 3.5 同步/协程自适应

```python
ret = fn(*args)
if inspect.isawaitable(ret):
    await ret
```

**为什么必须**：`XBrowserSession.close` 是 `async def`，同步调用只得到
`RuntimeWarning: coroutine ... was never awaited`，**并且实际什么都没执行**（会话泄漏）。

### 3.6 可观测

`/api/modules/status` 的 `business_modules`（32 条）+ `business_modules_summary`：

```json
{
  "name": "mood",
  "instantiated": true,
  "dirty": false,
  "declared_save": true,    // spec 声明的
  "declared_close": false,
  "has_save": true,         // 实例上真实存在的
  "has_close": false,
  "error": "",
  "note": "情绪价态/唤醒度，衰减与触发"
}
```

`declared_*` 与 `has_*` 都给，因为**实例方法形态的 close 在未构造时探测不到**
（如 `XBrowserSession.close`），只给一个数会误导。

### 3.7 加一个新业务模块

```python
# 1) 写工厂
def get_my_module(data_dir: str = "data") -> MyModule: ...

# 2) core/registry.py::default_specs() 加一条
ModuleSpec("my_module", "anima.cognitive.my_module:get_my_module",
           kind=KIND_DATA_DIR,
           save=["_save"],   # 用实测到的真实方法名
           close=None,       # 无外部资源就留 None
           note="一句话说明用途"),
```

**不用改 `anima_core.py`** —— shutdown 自动落盘并关闭。

---

## 4. 工具系统

### 4.1 注册

```python
registry.register(Tool(
    "generate_image",
    "生成图片。当用户要求画图/生图时使用。",
    {"prompt": {"type": "string", "description": "画面描述"}},
    make_generate_image(core),
    min_level=LEVEL_USER,
    priority=1,
))
```

### 4.2 权限分级

| `LEVEL_*` | 谁能用 |
|---|---|
| `READONLY` | 任何人（只读） |
| `USER` | 普通用户 |
| `ADMIN` | 群管理 |
| `OWNER` | 仅 owner |
| `BANNED` | 拒绝 |

`private_only=True` 强制仅私聊。ReAct 执行前二次校验。

### 4.3 可观测（2026-10-28 补）

```python
registry.names()        # 全部工具名（排序）
registry.list_tools()   # 元信息快照（可 JSON 序列化，不含 handler）
registry.status()       # {total, by_level, private_only, capability_mode}
```

**此前只有 `register`/`unregister`** —— 能加，但看不见。

实测：22 个内置工具，`by_level={1:1, 2:18, 3:2, 4:1}`。

---

## 5. 记忆分层

| 层 | 文件 | 作用域 | 保留 |
|---|---|---|---|
| 对话 | `memory/chat_memory.py` | 群号+QQ号 | 20 轮（LRU） |
| 向量 | `memory/vector_memory.py` | 全局 | 8000 条 |
| 长期 | `memory/long_term.py` | 全局 | 事件→知识→行为模式 |
| 情节 | `memory/episodes.py` | 全局 | 有界 |
| 宪法 | `constitution/memory_engine.py` | 全局 | 只读水合 |

### 轮边界双闸门（2026-10-27）

**只在 bot 回复后切轮**会被「bot 不回复」打破 —— 实测出现 222 小时 / 93 条消息的「一轮」。

```python
MAX_ENTRIES_PER_ROUND = 60     # 条数上限
MAX_ROUND_SPAN_SEC = 1800.0    # 跨度上限（与注入窗口 30min 对齐）
```

---

## 6. 自治循环

```
Heartbeat（30s tick）
  ├── 情绪衰减（mood decay）
  ├── 欲望衰减（desire decay）
  ├── 议程检查（agenda pending）
  ├── debt 到期（承诺跟进）
  ├── 探索（B站/X/维基 → 笔记 → 分享）
  └── 主动发言判断
        ├── 闸门：daily_limit / cooldown / quiet_hours / 群活跃度
        ├── 去重：与近期自主发言比对（4h 窗口，0.76 相似度）
        └── 接地网关：含排障/索要话术的丢弃
```

### 主动发言的输出路径

`autonomy._speak_locked()` → `gateway.send()` **直发**，不进 pipeline。

> ⚠️ **pipeline 的 guardrail 在这条路上不生效。**
> 主动发言必须自己净化（见 `exploration._sensitive_topic` 强制标注出处）。

---

## 7. Agent 任务

```
Owner 请求 → planner（LLM 规划 N 步）
           → executor（逐 step ReAct）
               ├── verify 失败 → replan（换方案，受节流闸管）
               └── 全部 done → deliver
           → owner_agent._deliver_text（出站）
           → chat_memory 写 [已交付 xxx]（供后续对话知晓）
```

### 交付三条纪律

1. **只给产物，不给过程** —— `【步骤N】` 是内部脚手架，不进用户可见文本
2. **出站前净化** —— `_sanitize_delivery` 去步骤标签/QQ号/元话语
3. **短任务不推进度** —— `_should_notify_steps`（≥5 步或 ≥60s 才逐步报）

> ⚠️ Agent 交付走 `gateway.send()` **直发**，绕过 pipeline ⇒
> guardrail 的泄漏拦截不生效，必须靠 `_sanitize_delivery`。

---

## 8. 出站守卫（guardrail）

`guardrail_stage` 里的四道闸：

| 闸 | 触发 | 处置 |
|---|---|---|
| `_detect_prompt_leak` | 账号形态长数字 / 指令标记 | **整条拒发**（删了号还是大段泄密） |
| `_looks_like_fake_completion` | 声称完成但零活动 | **SKIP_REPLY**（拒发 → 引追问，越解释越假） |
| `_looks_like_fake_image_claim` | 声称给图但无图 | **改写**（拒发像 bot 死了） |
| `strip_kinetic_decorations` | emoji/颜文字/`~~` | 删除（**保留**单个 `~` 和 `\n\n`） |

**处置策略分档的原则**：

- 「编造任务完成」→ 拒发（用户会追问，越解释越假）
- 「编造出图」→ 改写（拒发看起来像 bot 挂了）

---

## 9. 已知架构债

| 债务 | 现状 | 影响 |
|---|---|---|
| **29 个单例** | `get_xxx()` 各自维护缓存/锁/TTL | 已用 `ModuleRegistry` 收敛**生命周期**，内部实现未重写 |
| **两套直发路径** | pipeline + `gateway.send()` | 直发路径必须自己接净化 |
| **`CognitiveModule` 空壳** | 契约完整，0 个实现 | 用适配器绕过，未清理旧契约 |
| **`register_service()` 零调用** | `_services` 永远空 | WebUI 已移除该字段，代码还在 |
| **测试间污染** | 全量跑偶发失败，单跑通过 | 共享全局状态（`_KNOWN_BOTS_CACHE` 等），未隔离 |
| **`prefilter.py` 装 3 个阶段** | 历史原因 | 拆文件会动 import 路径，暂不动 |

---

## 10. 扩展点速查

| 想加什么 | 改哪里 | 注册在哪 |
|---|---|---|
| 一个工具 | `tools/builtin/xxx.py` 写 `make_xxx(core)` | `register_all_builtin()` |
| 一个 pipeline 阶段 | `pipeline/stages/xxx.py` 写 `async def xxx_stage(ctx)` | `handler.py::_build_pipeline()` |
| 一个业务模块 | `cognitive/xxx.py` 写 `get_xxx()` | `registry.py::default_specs()` |
| 一个 Channel | `channels/xxx.py` 继承 `Module` | `cli.py` `core.register()` |
| 一个 Skill | `skills/builtin/` 写 manifest | 自动扫描 |
| 一个 WebUI 页面 | `webui/src/views/XxxView.vue` | `navigation.ts` |
