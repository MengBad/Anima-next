# 贡献指南

> 本项目是 **owner 长期自用的个人项目**（见 [`docs/VISION.md`](docs/VISION.md)），
> 不是开放协作的开源社区项目。这份文档主要写给 **未来的自己** 和 **AI 助手**。
>
> 但它也诚实地记录了这个项目**踩过的坑**，对这些坑感兴趣的人可以读。

---

## 0. 开始之前

```bash
python -m venv venv && source venv/bin/activate
pip install -e '.[qq,vector,dev]'
pytest -q                      # 应该 6673 passed / 4 skipped
```

跑不通先看 [README 的「测试」节](README.md#测试)——有 10 个测试在缺少
`sylann-core` / `sqlite-vec` / `python-docx` / `openpyxl` 时会失败，
那是**环境缺失，不是代码问题**。

---

## 1. 六条硬规则

每条都对应一次真实事故。**不是审美偏好。**

### 1.1 注释不是约束，测试才是

**事故**：`rainyun_account.py` 里有一段注释写着

```python
# ⚠️ 但不能记成 ok=True —— 短路只说明「上次我们记过今天签了」
#    不等于雨云侧真签了
```

而紧接着的代码写的是：

```python
record_result(core, uid, c["email"], True, "今日已签到（按记录）")
```

**注释与实现自相矛盾，存活了整整一段时间**，直到有人查 `rainyun_history.json`
发现三条 `{"ok": true, "r": "今日已签到"}` 里没有一条能证明真签过。
后果：`consecutive_failures` 恒为 0 ⇒ 失败降级永久失效。

**规则**：

- 改行为**必须**同时改/加测试
- 如果旧测试锁定了错误行为，**改测试**，不要迁就它
- 例：`test_agent_text_delivery.py` 曾有 `assert "【步骤2】" in joined`
  这条断言把「给用户看工程日志」写成了规格。正确做法是反转成
  `assert "【步骤" not in joined`

### 1.2 降级永不阻塞主链

每个可选模块都静默失败：向量库不可用、Sylann 缺失、任何模块抛异常 ——
**核心功能必须继续**。

```python
try:
    optional_thing()
except Exception:
    logger.debug("已隔离", exc_info=True)   # 不要 raise
```

**反面例子**：在 `registry.shutdown()` 里如果某个模块 `close()` 抛异常就
`raise`，会导致后面 31 个模块都不落盘。

### 1.3 状态文件必须原子写

```python
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh)
os.replace(tmp, path)      # 原子
```

**事故**：`desire_decay_state.json` 曾经变成 37 字节残文件（`{"last_decay": 1792910124.560561}` 被截断），
下次启动读到残缺状态。项目里有 `.corrupt.bak` 文件就是那次留下的。

项目已提供 `PersistenceProvider.write_json()`，直接用。

### 1.4 别把内部实现给用户看

**这些都不该出现在 QQ 消息里**：

| 泄漏物 | 例子 | 正确做法 |
|---|---|---|
| 步骤脚手架 | `【步骤1】分析用户需求` | `_sanitize_delivery` 删掉 |
| 任务 ID | `[已交付 abc12345]` | 只进 `chat_memory`，不进用户文本 |
| 工作笔记 | `当前步骤：…执行：…` | 只保留「执行：」之后 |
| 下游计划 | `下一步可直接将该图片发送给用户` | 删掉 |
| QQ 号 | `主人的QQ号是1562290139` | 打码或删除 |
| 内部状态码 | `verify_failed` / `step_failed` | 换成「换了个做法」 |

**已有的守卫**（新增出站路径时要手动接上）：

- `guardrail._looks_like_fake_image_claim`
- `guardrail._looks_like_fake_completion`
- `guardrail._detect_prompt_leak`
- `agent/owner_agent._sanitize_delivery`
- `memory/chat_memory._is_bot_meta_line`

**事故**：2026-10-27 user 直接质问「为什么要把步骤给我看」。查下来
`_deliver_text` 的 docstring 明确写着「交付步骤结果而不是 result_parts 清单」，
且有测试锁着——**设计从第一天就是错的**。

### 1.5 代码默认 ≠ 生产实配

`config/default.yaml` 里 `false` 的开关，生产 `/opt/anima/config.yaml` 可能是 `true`。
`_deploy.py` 默认**不覆盖**生产配置。

```
⚠️ 「代码里有」≠「线上在跑」
```

核实手段：`python -m anima doctor`、WebUI 配置页、`journalctl`。

### 1.6 同一个 `0`，语义可能相反

```
exploration.daily_limit: 0        = 不限制（内部读取配额）
exploration.daily_share_limit: 0  = 当天完全不分享（外发闸门 → fail-closed）
```

写配置时先确认这个字段是「上限」还是「闸门」。

---

## 2. 加东西之前

### 2.1 先判断属于哪一层

| 层 | 判据 | 位置 |
|---|---|---|
| 纯函数 | 不依赖 `self.*`、不读写文件 | `anima/util/` 或就地 |
| 认知子系统 | 有状态、要落盘、要注入 prompt | `anima/cognitive/` |
| pipeline 阶段 | 处理 `PipelineContext` | `anima/pipeline/stages/` |
| 工具 | 模型可调用、有 JSON schema | `anima/tools/builtin/` |
| 渠道 | 接一个 IM 平台 | `anima/channels/` |

**别把有状态的逻辑塞进 pipeline 阶段**——阶段应该是薄的编排层。

### 2.2 三条扩展路径

#### 加一个工具

```python
# 1) anima/tools/builtin/my_tool.py
def make_my_tool(core):
    async def _run(**kwargs) -> str:
        ...
        return "给模型看的结果"
    return _run

# 2) anima/tools/builtin/__init__.py::register_all_builtin()
registry.register(Tool(
    "my_tool",
    "一句话说明什么时候该用它（模型靠这个判断）",
    {"param": {"type": "string", "description": "参数说明"}},
    make_my_tool(core),
    min_level=LEVEL_USER,
    required=["param"],
))
```

**测什么**：
- 参数校验（缺参/错类型）
- 权限分级（低权限用户不该拿到）
- 失败路径（返回错误串而不是抛异常）

#### 加一个 pipeline 阶段

```python
# 1) anima/pipeline/stages/my_stage.py
async def my_stage(ctx: PipelineContext) -> StageResult:
    ...
    return StageResult.CONTINUE

# 2) anima/llm/handler.py::_build_pipeline()
.use(my_stage)      # 两处都要加（普通路径 + astrbot 兼容路径）
```

> ⚠️ `_build_pipeline()` 里有**两个** `Pipeline()` 构造——普通路径 8 阶段，
> astrbot 兼容路径 9 阶段（多 `hook_stage`）。**两处都要改**，否则开兼容层时
> 你的阶段会消失。

#### 加一个业务模块

```python
# 1) anima/cognitive/my_module.py
def get_my_module(data_dir: str = "data") -> MyModule:
    ...

# 2) anima/core/registry.py::default_specs()
ModuleSpec("my_module", "anima.cognitive.my_module:get_my_module",
           kind=KIND_DATA_DIR,
           save=["_save"],     # ⚠️ 用实测到的真实方法名，不要猜
           close=None,
           note="一句话说明用途"),
```

**怎么确认 `save`/`close` 的真实方法名**：

```bash
python -c "
import sys; sys.path.insert(0,'.')
from anima.cognitive.my_module import get_my_module
m = get_my_module('data')
print([n for n in dir(m) if any(k in n.lower() for k in ('save','persist','flush','close','shutdown'))])
"
```

**不要猜**——34 个模块里 `save` 有七种写法。写错了 `registry` 会静默跳过
（`has_save=False`），但 `/api/modules/status` 会显示 `declared_save=true, has_save=false`
这个不一致，能查出来。

---

## 3. 测试怎么写

### 3.1 优先测行为，不要测源码文本

```python
# ❌ 坏：断言源码里有某个字符串
src = open("xxx.py").read()
assert "<= 6" in src          # 加个注释就挂了

# ✅ 好：断言行为
from xxx import _is_followup
assert _is_followup("哈基米说说详细的") is True
```

**事故**：`test_share_followup_and_slim.py` 曾断言「追问长度判据必须 `<= 6`」，
结果把「哈基米说说详细的」（8 字）永久锁在门外 —— 而这是最自然的追问方式。
注释加长后甚至误伤了改注释的人。

### 3.2 测试必须能用反例校准

```python
# 正例：必须触发
for t in ("哈基米说说详细的", "详细说说", "发", "哪看的"):
    assert f(t)

# 反例：绝不能触发（这才是真正抓 bug 的部分）
for t in ("哈基米你觉得这图好笑吗", "你在干嘛", "好的"):
    assert not f(t)
```

**只写正例的判据一定是过宽的。**

### 3.3 权重/阈值必须来自实测，不能拍脑袋

```python
# ❌ 坏：看起来合理就给 15 分
"no_card": 15,        # 「没群名片」看起来很像 bot

# ✅ 好：实测 320/386 个真人也没有群名片 ⇒ 几乎是噪声
"no_card": 2,
```

`test_score_weights_are_regression_calibrated` 锁死这条 —— 改权重会失败。

### 3.4 覆盖 fail-soft 路径

```python
def test_construct_failure_is_isolated(tmp_path):
    reg.register_spec(ModuleSpec("bad", "no_such_module:fn"))
    reg.register_spec(ModuleSpec("good", "anima.cognitive.mood:get_mood_system"))
    r = asyncio.run(reg.startup(core))
    assert r["failed"] == 1 and r["ok"] == 1   # bad 挂了不影响 good
```

---

## 4. 提交前检查

```bash
# 1. 全量测试
pytest -q

# 2. 如果改了 config 默认值，确认 default.yaml 也改了
#    （这个坑犯过至少两次：tick_sessions_limit / group_reply_max_chars）

# 3. 如果改了出站文本，跑一遍净化相关测试
pytest -q -k "sanitize or guardrail or fake"

# 4. 如果改了 registry spec，验证声明与实际一致
python -c "
import sys; sys.path.insert(0,'.')
import asyncio
from anima.core.registry import ModuleRegistry, default_specs
from anima.core.config import load_config
import tempfile
td=tempfile.mkdtemp()
cfg=load_config('config/default.yaml'); cfg.persistence.data_dir=td
class C: pass
core=C(); core.config=cfg
reg=ModuleRegistry(td); reg.register_all(default_specs())
asyncio.run(reg.startup(core))
bad=[s['name'] for s in reg.status()
     if s['declared_save']!=s['has_save'] or s['declared_close']!=s['has_close']]
print('不一致:', bad or '无')
"
```

### Commit message

用 `type(scope): 一句话` 格式：

```
fix(rainyun): 短路不得记 ok=True —— 注释与实现矛盾了整整一段时间
feat(core): 统一模块注册表 —— 32 个业务模块统一构造/落盘/关闭/观测
docs(readme): 重写 —— 从工程台账改为可读的项目说明
```

**正文要写「为什么」而不是「改了什么」**——`git show` 已经能看到 diff。

好的正文：

```
## 问题
cognitive/ 下 34 个单例，23 个没有 close/shutdown
save 有七种写法，签名四种
后果：加新模块若忘了手写 close_xxx() 就是资源泄漏

## 解法
ModuleRegistry + ModuleSpec + ModuleAdapter，不改现有模块
```

---

## 5. 部署

```bash
python _deploy.py --dry-run    # 先看计划
python _deploy.py              # 真部署
```

**`_deploy.py` 会做的保护**：

| 保护 | 原因 |
|---|---|
| 不覆盖生产 `config.yaml` | 生产实配可能与仓库默认值不同 |
| 不覆盖生产 `data/` | 先快照，原子换名恢复 |
| 停机后等进程退净 | 否则 `data/` inode 被换掉 → EROFS |
| 5 分钟自动拉起 | 部署中途断连不至于永久下线 |

**部署后验证**：

```bash
journalctl -u anima --since '-3min' | grep -E "Traceback|ERROR|registry"
curl -s http://127.0.0.1:8088/api/modules/status | jq '.business_modules_summary'
```

---

## 6. 别做的事

| 不要 | 因为 |
|---|---|
| 把 `gateway.send()` 改成走 pipeline | 主动发言/Agent 交付是**设计上**要绕过 pipeline 的（拿不到 ctx） |
| 给 `_QQ_DIGITS_RE` 做全局数字打码 | 会把「积分从 29100 涨到 29600」毁成 `***` |
| 把 `CognitiveModule` 强推给现有模块 | 34 个模块的内部缓存/锁/TTL 都在工作，强改会引入新 bug |
| 移动根目录 `.md` | 129 处引用（含代码注释），移了路径就失效 |
| 降低 `familiar_min` | 注释明确写「没有数字就调阈值 = 拍脑袋」，而 `reply_learning` 还没跑出数字 |
| 用 `pgrep -f 'anima start'` 判残留进程 | 会匹配到执行它的 shell 自身，计数恒 ≥1 |

---

## 7. 有事问谁

这是个人项目，没有 reviewer 队列。

**遇到不确定的**：

1. 先读 [`docs/ARCHITECTURE_CURRENT.md`](docs/ARCHITECTURE_CURRENT.md)（实现层）
2. 再读 [`MAINTENANCE.md`](MAINTENANCE.md)（接线状态与踩坑）
3. 还没答案就看代码 —— 注释里的 `🔴 2026-XX-XX` 通常记录了当时的判断依据
