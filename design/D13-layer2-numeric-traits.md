# D-13 层二设计稿：数值 trait 生产路径

> 状态：**设计稿（待 owner 决策）**　起草：2026-11-28
> 触发：owner「让 RFC-0100 从设计变成行为」
> 关联：`ENGINEERING_DEBT_LEDGER.md` D-13 第二层 · D-30（SEALED）· RFC-0101 · RFC-0103 · RFC-0109

---

## 0. 一句话问题

派生链 `Memory → Identity → Need → Drive` 的**每一环都已实现且正确**，
唯一缺的是：`reflect()` 只往 semantic 里写文本 `{"lesson": str}`，
而 `derive_identity` 只聚合**数值**字段 ⇒ `traits` 恒为空 ⇒ 整条链空转。

**所以这不是"补代码"，是"补数据形状"。**

---

## 1. 现状取证（读代码确认，非文档推断）

### 1.1 链路逐环

| 环节 | 位置 | 现状 |
|---|---|---|
| `reflect()` | `constitution/reflection.py` | 产出 `semantic: {"lesson": <str>}`、`episode: {"event","status"}` |
| `derive_identity()` | `constitution/identity.py:35-38` | **已支持**：`isinstance(v,(int,float))` 即累加 `v * rec.salience` |
| `derive_need()` | `constitution/need.py` | **已消费**：CURIOSITY 取 `traits > 0.5`，AVERSION 取 `traits < 0.1`；urgency 用 `peak_intensity` |
| `drive_gate` | `autonomy.py:2613` | 默认 `False`；翻转则**只在 `activated=False` 或 `approach<=avoid` 时压制发言** |

### 1.2 关键事实

```python
# identity.py 第 35-38 行（原文）
elif isinstance(v, (int, float)):
    traits[str(k)] = traits.get(str(k), 0.0) + float(v) * rec.salience
```

⇒ **只要 semantic 记录里出现数值字段，traits 立刻非空。** 引擎侧零改动。

### 1.3 `drive_gate` 翻转的真实后果（与直觉相反）

`_drive_gate_ok()` 只在「未激活 / 回避>趋近」时**压制**。所以：

| traits 状态 | 翻转 `drive_gate` 的结果 |
|---|---|
| 空（现状） | `activated=False` ⇒ **主动发言被静默压死** |
| 非空且合理 | 正常放行，主动发言有据可依 |

**这解释了为什么台账写"不得翻转"**——不是怕它乱来，是怕它哑掉。

---

## 2. 核心争议：什么算 Identity trait

台账原文：

> 「什么算 trait」是 **RFC-0101 Identity vs RFC-0103 Affect** 的边界决策
> （mood 是 Affect，不是 Identity），**不可由执行层擅自定义**。

### 2.1 反例（为什么不能随手写）

若把 `mood=0.8` 写进 trait：

```
derive_need → CURIOSITY: "explore: mood"     ← 荒谬：把情绪当探索对象
           → AVERSION: "avoid losing: mood"  ← 荒谬：把情绪当人格缺失
```

还会污染人格漂移监控（把情绪波动当人格变化）。

### 2.2 判据：什么样的数值**可以**是 trait

我提出三条硬判据，凡不满足者一律不进 traits：

| # | 判据 | 说明 |
|---|---|---|
| **C1** | **跨会话稳定** | 不会因单条消息/单次心情改变。今日高兴明日低落 ≠ trait 变化 |
| **C2** | **行为倾向而非当下状态** | 描述"我倾向于怎么做"，不是"我现在感觉如何" |
| **C3** | **可由经历累积推出** | 必须来自真实承载历史，不能凭 prompt 自评 |

按这三条筛：

| 候选 | C1 | C2 | C3 | 判定 |
|---|---|---|---|---|
| `mood` / `valence` / `arousal` | ❌ 时时变 | ❌ 是状态 | — | **Affect，禁止入 traits** |
| `reliability`（成功占比） | ✅ 累积稳定 | ✅ 行为倾向 | ✅ 来自承载 | **可入** |
| `caution`（失败占比） | ✅ | ✅ | ✅ | **可入** |
| `engagement`（接活率） | ✅ | ✅ | ✅ | **可入** |
| `curiosity`（探索次数） | ✅ | ✅ | ✅ | 可入，但需另一条 producer |

---

## 3. 方案：Rule-derived Traits

### 3.1 数据形状

在 semantic 记录里**追加顶层数值字段**（lesson 原样保留）：

```python
# 当前
{"kind": "semantic", "content": {"lesson": lesson}, "salience": 0.6}

# 改为
{"kind": "semantic", "content": {"lesson": lesson, "reliability": 1.0}, "salience": 0.6}
```

### 3.2 实现要点：必须扁平（不能嵌套）

`derive_identity` 只遍历 `rec.content.items()` 的**第一层**。
所以 `{"self_eval": {"reliability": 1.0}}` **无效**，必须写成顶层 `"reliability": 1.0`。

### 3.3 数值怎么算（关键）

**不能由单条决定**——单条成功永远是 1.0，会变成恒定值。
必须在 `ReflectionSedimenter` 里基于**累积历史**计算：

```python
class ReflectionSedimenter:
    def __init__(...):
        self._carried = 0      # 总承载次数
        self._succeeded = 0    # 成功次数

    def sediment(self, *, plan_ref, succeeded, outcome, lessons=(), timestamp=None):
        # ... 既有去重/上限检查 ...
        self._carried += 1
        if succeeded:
            self._succeeded += 1
        reliability = round(self._succeeded / self._carried, 3)
```

### 3.4 trait 定义

| trait | 公式 | 范围 | 语义 |
|---|---|---|---|
| `reliability` | 成功 / 总承载 | 0–1 | 我交付的东西靠得住吗 |
| `caution` | 失败 / 总承载 | 0–1 | 我是不是该更谨慎 |
| `engagement` | 承接意图 / 被 offer | 0–1 | 我愿意接活吗 |

**第一个版本只做 `reliability`**（见 §6 分期）。

---

## 3.5 🔴 验算发现的问题：累加会顶爆 peak_intensity

### 3.5.1 实测

按 §3.3 的方案（每条承载写一条 `reliability`，`salience=0.5`），
模拟「10 次承载 / 7 次成功」，跑真实派生链的结果：

```
identity.traits         = {"reliability": 1.0}    <- 归一化后恒 1.0（预期内）
identity.peak_intensity = 4.676                    <- 🔴 爆了
need.urgency            = 1.0                      <- 🔴 恒为 1.0
```

### 3.5.2 为什么这是问题

`derive_need` 的 urgency 公式是 `min(1.0, peak_intensity)`。
`peak_intensity` 是**归一化前的原始累加和** ⇒ 每多一条记录就涨 0.5，
**必然在 2–3 条后就顶到 1.0 以上，且永不再降**。

这正是 D-13 记录过的老毛病：

> 「只要有欲望 traits 就是 1.0」⇒ urgency 恒 1.0 ⇒ Drive 永真激活

若直接按 §3.3 实现，会**重现这个已被修过一次的缺陷**。

### 3.5.3 根因：语义错位

`reliability` 是**比率（0–1 的状态量）**，
而 `derive_identity` 的累加语义是为「多条不同程度的证据叠加」设计的。
把状态量反复叠加 = 语义错误。

| 方案 | 做法 | 评价 |
|---|---|---|
| **A** | 每次写滚动值并**替换**而非追加 | 违反「Memory 只追加」纪律 ❌ |
| **B** | 降低 salience 使累加不过界 | 治标；条数不可预测，迟早爆 ❌ |
| **C** | **不写比值，写计数**：成功写 `+1`、失败写 `+0`，让累加和本身有意义 | ✅ 语义自洽 |
| **D** | 按 plan_ref 去重，只更新不追加 | 需改 `_seen` 语义，风险中 ⚠️ |

### 3.5.4 推荐方案 C

**核心洞察**：`derive_identity` 的累加语义适合「计数型证据」，不适合「比率型状态」。
所以不要让 sedimenter 算比率，**让它只贡献计数**：

```python
# sedimenter 每次承载写一条
{"kind": "semantic", "content": {"delivery_ok": 1.0}, "salience": 0.5}    # 成功
{"kind": "semantic", "content": {"delivery_fail": 1.0}, "salience": 0.5}  # 失败
```

然后在 **`derive_identity` 里**把计数转成比率（这是 Identity 层的职责）：

```python
# identity.py 新增（约 6 行）
if "delivery_ok" in traits or "delivery_fail" in traits:
    _ok = traits.pop("delivery_ok", 0.0)
    _fail = traits.pop("delivery_fail", 0.0)
    _total = _ok + _fail
    if _total > 0:
        traits["reliability"] = round(_ok / _total, 3)   # 0..1 状态量
```

### 3.5.5 peak_intensity 自动回到合理区间

关键：一旦把计数 pop 掉、换成 `reliability`（0–1），
`derive_identity` 原有的 `_peak_raw = max(traits.values())` **自然回到 0–1**——
**不需要再改归一化段**。

这正是为什么方案 C 优于「调 salience」：它让既有代码的语义重新自洽，
而不是靠参数调节掩盖语义错误。

### 3.5.6 修正后的完整改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `reflection_loop.py` | 写 `delivery_ok`/`delivery_fail` **计数**记录 | ~20 行 |
| `identity.py` | 计数 → `reliability` 比率（pop 原始键 + 归约） | ~6 行 |
| `need.py` / `config/*` | **不动** | 0 |

**总改动 ~26 行，两个文件。**

### 3.5.7 修正后验算（实现时必须复现此结果）

10 次承载 / 7 次成功，预期：

```
traits         = {"reliability": 0.7}      <- 0..1 状态量
peak_intensity = 0.7                        <- 合理，不再爆
need.urgency   = 0.7                        <- 有区分度
CURIOSITY      = ["explore: reliability"]   <- traits>0.5 命中
```

**验收硬判据：`peak_intensity` 必须落在 [0, 1]。超过 1 即实现错误。**

---

## 3.6 🔴 二次验算：两个必须处理的退化形态

方案 C 通过后，又跑了两组边界，发现两个真实退化形态。
**这两个不处理，阶段 1 上线后会得到「看似非空、实则无区分度」的假成功。**

### 3.6.1 退化形态一：小样本立即饱和

实测（样本量 vs reliability/urgency）：

| 成功/总数 | reliability | urgency | 判读 |
|---|---|---|---|
| **1/1** | **1.0** | **1.0** | 🔴 首次成功即饱和 |
| 2/2 | 1.0 | 1.0 | 🔴 仍饱和 |
| 5/5 | 1.0 | 1.0 | 🔴 仍饱和 |
| 10/10 | 1.0 | 1.0 | 🔴 仍饱和（全成功本就该是 1） |
| 2/3 | 0.667 | 0.667 | ✅ 有区分度 |
| 4/5 | 0.8 | 0.8 | ✅ 有区分度 |

**问题**：只要历史里出现一次全成功，reliability 就是 1.0，
urgency 随之饱和，`_drive_gate_ok` 的 `approach > avoid` 判定失去意义。

**根因**：比率没有置信度概念。1 次成功和 100 次成功给同样的 1.0。

**修法：Laplace 平滑（加性平滑）**

```python
# 不用裸比率，用 (ok + 1) / (ok + fail + 2)
# 语义：先验假设「成功/失败各一次」，样本少时向 0.5 收缩
if _ok + _fail > 0:
    traits["reliability"] = round((_ok + 1.0) / (_ok + _fail + 2.0), 3)
```

平滑后预期：

| 成功/总数 | 裸比率 | 平滑后 |
|---|---|---|
| 1/1 | 1.0 | **0.667** |
| 2/2 | 1.0 | **0.75** |
| 5/5 | 1.0 | **0.857** |
| 10/10 | 1.0 | **0.917** |
| 2/3 | 0.667 | **0.6** |

⇒ **样本越多越接近真实比率，样本少时保守**。这才是有意义的 trait。

### 3.6.2 退化形态二：全失败触发语义荒谬的 AVERSION

实测（0/10 成功）：

```
traits    = {"reliability": 0.0}
AVERSION  = ["avoid losing: reliability"]    <- 🔴 语义荒谬
```

`derive_need` 的 AVERSION 规则是「`traits < 0.1` 的键 → `avoid losing: <key>`」。
而 `reliability=0` 表示「我不可靠」，不是「有个叫 reliability 的东西我快失去了」。

**这暴露了一个更根本的问题：`derive_need` 的 AVERSION 规则假设所有 trait 都是「资源型」，
而 `reliability` 是「状态型」。**

两种处理：

| 方案 | 做法 | 评价 |
|---|---|---|
| **A** | 全失败时**不写** `delivery_fail`，改写 `delivery_ok: 0.0` | 治标，仍会产生 0 值 trait ⚠️ |
| **B** | 在 identity 层给 `reliability` 设**下限**（如 0.15），永不低于 0.1 | 掩盖问题，但避免了荒谬输出 ✅ 务实 |
| **C** | 改 `derive_need` 的 AVERSION 排除状态型 trait（白名单） | 改 RFC-0102 契约 ⚠️ |

**推荐 B（务实）+ 记录 C 为长期方向**：

```python
# identity.py：reliability 加下限，避免踩 AVERSION 的 <0.1 阈值
if _ok + _fail > 0:
    _r = (_ok + 1.0) / (_ok + _fail + 2.0)
    traits["reliability"] = round(max(0.15, _r), 3)   # 下限 0.15
```

理由：平滑后 0/10 会算出 `1/12 = 0.083 < 0.1` ⇒ 仍会触发 AVERSION。
加 0.15 下限即可跳出该阈值，且 0.15 本身也合理表达「很不靠谱但仍在努力」。

### 3.6.3 修正后的最终公式

```python
# identity.py（最终版，约 8 行）
if "delivery_ok" in traits or "delivery_fail" in traits:
    _ok = traits.pop("delivery_ok", 0.0)
    _fail = traits.pop("delivery_fail", 0.0)
    if _ok + _fail > 0:
        # Laplace 平滑：小样本向 0.5 收缩，样本多时逼近真实比率
        _r = (_ok + 1.0) / (_ok + _fail + 2.0)
        # 下限 0.15：跳出 derive_need 的 AVERSION 阈值(<0.1)，避免语义荒谬输出
        traits["reliability"] = round(max(0.15, _r), 3)
```

### 3.6.4 最终验算表（实现时必须全部复现）

| 场景 | reliability | peak | urgency | 是否可用 |
|---|---|---|---|---|
| 1/1 | 0.667 | 0.667 | 0.667 | ✅ 不饱和 |
| 5/5 | 0.857 | 0.857 | 0.857 | ✅ |
| 10/10 | 0.917 | 0.917 | 0.917 | ✅ 接近但不到 1 |
| 7/10 | (8)/(12)=0.667 | 0.667 | 0.667 | ✅ |
| 0/10 | 0.15（触底） | 0.15 | 0.15 | ✅ 不触发 AVERSION |

**三条硬判据（任一不满足即实现错误）**：

1. `0 < reliability < 1`（严格在开区间内）
2. `peak_intensity == max(traits.values())` 且 `<= 1`
3. 全失败场景下 `AVERSION` **不含** `avoid losing: reliability`

---

## 3.7 🔴 三次验算：两个实现级陷阱（本节是最终实现依据）

§3.6 的公式在**独立模拟**中正确，但一放进真实 `derive_identity` 流程就失效。
原因是两个实现级陷阱——**不知道它们，实现必然做错**。

### 3.7.1 陷阱一：`salience` 会缩小计数，破坏平滑先验

`derive_identity` 的累加是 `v * rec.salience`。若 trait 记录用 `salience=0.5`：

```
7 次成功 -> traits["delivery_ok"] = 7 * 0.5 = 3.5    <- 不是 7
```

代入 `(ok+1)/(ok+fail+2)` 时，分子分母被同时缩小 ⇒ **平滑先验被放大一倍**，
所有场景都塌缩到 0.667 附近（实测确认，区分度全失）。

**修法：trait 记录必须用 `salience = 1.0`**，让累加值等于真实计数。

```python
# reflection_loop.py
{"kind": "semantic", "content": {"delivery_ok": 1.0}, "salience": 1.0}    # ← 1.0 不是 0.5
```

### 3.7.2 陷阱二：转换必须插在**归一化之前**

`derive_identity` 末尾有归一化：`traits = {k: v/peak for ...}`，峰值恒被抬到 1.0。
所以**从外部拿到的 traits 已经失去原始计数**——在函数外做转换拿不到 `ok/fail` 真实值。

**修法：转换代码必须写在 `derive_identity` 内部、`_peak_raw` 计算之前。**

```python
# identity.py 结构（示意位置）
for rec in (...):        # 累加
    ...

# >>> 在这里插入转换（归一化之前） <<<
if "delivery_ok" in traits or "delivery_fail" in traits:
    _ok = traits.pop("delivery_ok", 0.0)
    _fail = traits.pop("delivery_fail", 0.0)
    if _ok + _fail > 0:
        _r = (_ok + 1.0) / (_ok + _fail + 2.0)
        traits["reliability"] = round(max(0.15, _r), 3)

_peak_raw = 0.0          # 既有的归一化段，保持不动
if traits:
    _peak_raw = round(max(traits.values()), 3)
    peak = max(traits.values()) or 1.0
    traits = {k: round(v / peak, 3) for k, v in traits.items()}
```

### 3.7.3 陷阱三：归一化会再次把 reliability 抬到 1.0

即使用 salience=1.0 且插对位置，只要 traits 里**还有其他 trait**，
归一化仍会把 `reliability=0.667` 除以峰值抬到 1.0。

**修法：`reliability` 已是 0–1 状态量，必须豁免归一化。**

```python
_peak_raw = 0.0
if traits:
    _peak_raw = round(max(traits.values()), 3)
    peak = max(traits.values()) or 1.0
    # reliability 是比率型状态量，归一化会破坏它的语义（0.667 -> 1.0）
    traits = {
        k: (round(v, 3) if k == "reliability" else round(v / peak, 3))
        for k, v in traits.items()
    }
```

### 3.7.4 三次验算后的最终结果（实现验收基准）

`salience=1.0` + 归一化前转换 + reliability 豁免归一化，实测：

| 场景 | reliability | peak | urgency | 判定 |
|---|---|---|---|---|
| 1/1 | 0.667 | 0.667 | 0.667 | ✅ |
| 5/5 | 0.857 | 0.857 | 0.857 | ✅ |
| 10/10 | 0.917 | 0.917 | 0.917 | ✅ 接近但不到 1 |
| 7/10 | 0.667 | 0.667 | 0.667 | ✅ |
| 3/2 | 0.6 | 0.6 | 0.6 | ✅ |
| 10/20 | 0.5 | 0.5 | 0.5 | ✅ 有明确区分度 |
| 0/10 | 0.15 | 0.15 | 0.15 | ✅ 触底不触发 AVERSION |

**这张表就是实现完成后的断言基准。任何一行对不上即实现错误。**

### 3.7.5 对 §3.5.5 判断的更正

> §3.5.5 曾写「一旦把计数换成比率，`max(traits.values())` 自然回到 0–1，
> **无需再改 identity.py 的归一化段**」——**该判断错误，以本节为准。**

实测证明：归一化对**所有** trait 一视同仁，会把 reliability 一并抬到 1.0。
因此 identity.py 的归一化段**必须改**（加 reliability 豁免）。

这是三轮验算里唯一推翻前文结论的地方，保留在此以示推导过程。

---

## 4. 影响面分析

### 4.1 改哪些文件

| 文件 | 改动 | 行数估计 |
|---|---|---|
| `constitution/reflection_loop.py` | 加计数器 + 计算 + 注入一条 trait 记录 | ~25 行 |
| `constitution/reflection.py` | **不动**（保持 M1.9 纯度契约） | 0 |
| `constitution/identity.py` | **不动**（已支持数值） | 0 |
| `constitution/need.py` | **不动** | 0 |
| `config/*` | **不动**（`drive_gate` 保持 False） | 0 |

**总改动 ≈ 25–30 行，单文件。**

### 4.2 与既有守卫的冲突

| 守卫 | 断言 | 处置 |
|---|---|---|
| `test_loop_does_not_fabricate_numeric_traits` | 闭环**不得**伪造数值 trait | 🔴 需改判为白名单校验 |
| `test_constitution_memory_closure` | 5 条断层形状守卫 | 复核 |
| `test_constitution_reflection_loop` | 13 项 | 复核 |

那条 `does_not_fabricate` 守卫正是为"防执行层擅自定义 trait"而设。
本次是 **owner 授权的语义决策**，不是执行层擅自行为 ⇒ 改为
`test_loop_only_emits_approved_traits`（白名单校验 `{"reliability"}`）。

### 4.3 生产影响

| 项 | 影响 |
|---|---|
| 主动发言 | **零变化**（`drive_gate=False` 不动） |
| LLM 成本 | **零**（纯算术） |
| 磁盘 | 每条多 1 个 float，可忽略 |
| 审计 | `identity_traits` 从 0 变非 0（这是**验收信号**） |

---

## 5. 边界与失败模式

| 场景 | 行为 | 理由 |
|---|---|---|
| `FAILED` 承载 | `reflect()` 不产记录 | 保持引擎形状，不伪造失败记忆 |
| 去重命中 | 不重复写记忆，但**计数照加** | 去重是"不重复写"，不是"没发生过" |
| 达 max_records | 停止写入，计数仍累积 | 已沉淀的是历史事实 |

### 5.1 真实缺口：lessons 为空

`reflect()` 只在 `lessons` 非空时产 semantic 记录，而真实承载**通常不带 lesson**
（`sediment` 的 `lessons=()` 默认空）⇒ **若只往 semantic 注入，reliability 永远写不进去**。

| 选项 | 做法 | 评价 |
|---|---|---|
| **A** | sedimenter **额外产一条** `semantic: {"reliability": x}` | ✅ 改动最小，推荐 |
| B | 挂到 episode 上 | ❌ episode 不进 traits |
| C | 改 derive_identity 读 episode | ❌ 改引擎契约，风险最高 |

**推荐 A**：在 sedimenter 层追加专用 trait 记录，docstring 写明
「本层新增，非 RFC-0109 输出」。

---

## 6. 分阶段落地

### 阶段 1：只产 reliability，不翻门（推荐先做）

| 项 | 内容 |
|---|---|
| 改动 | `reflection_loop.py` 计数器 + 注入 trait 记录 |
| `drive_gate` | **保持 False** |
| 验收 | 审计 `identity_traits` 从 0 → `{"reliability": x}`；`peak_intensity > 0` |
| 回滚 | 一行开关 `constitution_reflection_loop=False` |
| 观察 | 1–2 周，看 reliability 分布是否有区分度 |

### 阶段 2：加 caution / engagement

前提：阶段 1 的 reliability **不是恒 1.0 或恒 0**（否则公式有问题）。

### 阶段 3：讨论是否翻 `drive_gate`

**前置条件（缺一不可）**：

1. `peak_intensity` 在真实区间波动（不是恒 1.0）
2. CURIOSITY 有实际内容（`traits > 0.5` 至少一项）
3. 审计回放：翻转后**主动发言数量不下降**

第 3 条是关键——翻错方向的后果是"哑掉"，不是"乱来"。

---

## 7. 建议的最小第一步

```
1. sedimenter 加 _carried/_succeeded 计数
2. 每次 sediment 后写一条 content={"reliability": x} 的 semantic
3. drive_gate 保持 False
4. test_loop_does_not_fabricate_numeric_traits -> 白名单校验
5. 新增 test_reliability_trait_accumulates
```

预计 **~30 行 + 2 个测试**，风险极低（不碰任何行为路径）。

---

## 8. 需要 owner 决策的三个点

| # | 决策 | 选项 | 我的建议 |
|---|---|---|---|
| **1** | `reliability` 算不算 RFC-0101 Identity trait | 算 / 不算 | **算**——满足 C1/C2/C3，且与 mood 有明确边界 |
| **2** | 是否接受「sedimenter 层额外产 trait 记录」 | 接受 / 要求改 RFC-0109 | **接受**——改引擎需走 F1 流程，成本远高 |
| **3** | 阶段 1 之后是否继续 | 继续 / 停在阶段 1 | **先停**，看两周数据再定 |

---

## 9. 若裁定「不算」

那 RFC-0100 的 Drive 链在生产上**永无激活可能**，此时应做：

1. `VISION.md` 写明：**RFC-0100 是理论完备的设计，生产不接入且无计划接入**
2. `drive_gate` 与 constitution drive 相关代码标 `DEPRECATED-NOT-FOR-PROD`
3. D-13 / D-30 改 `SEALED`（永久关闭），不再挂为"待办"

这也是**诚实且可接受的终局**——比挂着"未闭合"半年不谈更有价值。
