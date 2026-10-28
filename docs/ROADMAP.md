# 扩展计划（ROADMAP）

> **本文档是唯一的路线图。** 根目录 `ROADMAP.md` 停更于 2026-08-17，仅作历史参考。
>
> 与其它文档的分工：
> - [`VISION.md`](VISION.md) —— 定位与边界
> - [`ARCHITECTURE_CURRENT.md`](ARCHITECTURE_CURRENT.md) —— 实现层
> - **本文档** —— 接下来做什么、为什么、怎么验收
> - [`ENGINEERING_DEBT_LEDGER.md`](../ENGINEERING_DEBT_LEDGER.md) —— 已发现的问题
> - [`CONTRIBUTING.md`](../CONTRIBUTING.md) —— 怎么改代码

最后更新：2026-10-28

---

## 0. 当前坐标

| 维度 | 状态 |
|---|---|
| 版本 | `0.3.0rc2` |
| 测试 | 6680 passed / 4 skipped |
| 工程债 | 44 条，**1 个 OPEN**（D-41，已部分实现） |
| 生产 | 稳定，`registry` 33 模块全部构造成功 |

### 已完成（2026-10-28 单日）

| # | 事项 | 证据 |
|---|---|---|
| 1 | 统一模块注册表（33 模块） | `registry` 启动日志 ok=33 failed=0 |
| 2 | Bot 自判（`is_robot` + 评分制） | 394 成员实测 bot 8/8、真人 0/386 误判 |
| 3 | 部署停机竞态修复 | 连续 3 次部署 EROFS 0 |
| 4 | README + 架构 + 贡献指南重写 | 302 / 337 / 380 行 |

---

## 1. 近期（P0）：把已有的东西用起来

### 1.1 D-41 完整化 —— owner 私聊通道

**现状**：`_owner_digest_tick` 每日推待办摘要（已工作）。`owner_notify` 通道已建
（urgent 即时 / normal 队列），`control_surface` 健康告警已接入。

**待做**：

| 子项 | 状态 | 验收 |
|---|---|---|
| `owner_notify.py` 通道 | ✅ 完成 | 19 项测试 |
| `control_surface` 接入 | ✅ 完成 | 未知——需真实告警触发验证 |
| `autonomy` 心跳 flush | ✅ 完成 | 未知——需队列有内容 |
| **其余心跳产出接入** | ⏳ 待做 | 见下表 |

**还没接入 `owner_notify` 的产出**：

| 位置 | 产出 | 建议 kind |
|---|---|---|
| `autonomy.py:1901` | DevIssue → 诊断任务 | `normal` |
| `evolution/` | 演化提案待审 | `normal` |
| `capability/` | 能力撤销/熔断 | `urgent` |
| `cognitive/controlled_iteration.py` | 补丁待审批 | `normal`（已有独立通知，可保留） |

**验收口径**（沿用 D-41 三条）：

1. `owners` 为空 ⇒ fail-closed 不伪造发送
2. 新增通道自建节流（不绕过群侧防刷屏纪律）
3. 群侧行为零变化

### 1.2 验证两项待真实场景的修复

| 修复 | 状态 | 怎么验 |
|---|---|---|
| 追问兜底（`哈基米说说详细的` 8 字） | 只有单元测试 | 群里问「哈基米详细说说那个 X」→ 日志应出现 `[追问兜底] ... 意图命中` |
| 敏感题材强制出处 | 只有单元测试 | 等下一条涉及死亡/犯罪的探索分享 → 正文应带来源 |

**风险**：判据可能过宽（把普通对话当追问）或过窄（该触发不触发）。

---

## 2. 中期（P1）：让扩展变简单

### 2.1 消除「两套直发路径」

**问题**：出站有两条路：

```
pipeline（有 guardrail）  ← 被动回复
gateway.send() 直发       ← 主动发言 / Agent 交付 / owner 通知
```

直发路径**必须自己接净化**，忘了就是泄漏（`【步骤1】`、QQ 号进用户视野）。

**方案**（三选一，按成本排序）：

| 方案 | 做法 | 成本 | 效果 |
|---|---|---|---|
| A | 给 `gateway.send()` 加一个 `sanitize=True` 参数，内部调 `_sanitize_delivery` | 低 | 覆盖所有直发 |
| B | 抽 `SafeGateway` 包装类，强制净化 | 中 | 同上 + 可测 |
| C | 把直发也走 pipeline（传一个最小 ctx） | 高 | 统一，但改动大 |

**建议 A**：`gateway.send(msg, sanitize=True)` 默认 True，只有明确知道内容安全时传 False。
风险：`gateway` 是所有出站的必经之路，改错影响面大。

**验收**：
- 直发路径的正文自动过 `_sanitize_delivery`
- 现有 `gateway.send()` 调用点行为不变（除净化）
- 新增测试：直发含 `【步骤1】` 时被清掉

### 2.2 清理 `CognitiveModule` 空壳

**问题**：`core/module.py` 的 `CognitiveModule` 契约文档说「所有 cognitive 子系统应继承」，
但 **0 个实现**、`register_service()` 零调用。

**方案**：

| 方案 | 做法 | 风险 |
|---|---|---|
| A | 删掉 `CognitiveModule` 与 `register_service`，只留 `Module` | 低（本来没人用） |
| B | 让 `ModuleAdapter` 继承 `CognitiveModule` 接口 | 中 |
| C | 保持现状，只在文档标注「deprecated」 | 零 |

**建议 A**：删掉死代码。但需先确认 `_services` 没有别的读者
（`webui/server.py:355` 的注释说已移除）。

### 2.3 拆分 `prefilter.py`

**问题**：`prefilter.py` 里挤了 3 个阶段（`prefilter_stage` / `attachment_text_stage`
/ `bullying_stage`），其余阶段各占一个文件。

**风险**：拆了要动 import 路径，有 20+ 处引用。

**建议**：**暂不动**。收益低（只是整齐），风险中等。如果将来要加第 4 个阶段再拆。

### 2.4 测试隔离

**问题**：全量跑偶发失败、单跑通过（`test_crossgroup_injection` /
`test_group_turn_serialization` / `test_injects_user_memory_in_private`）。

**根因**：共享全局状态 ——
`_KNOWN_BOTS_CACHE` / `_recent`（bot_social）/ `_managers`（多个 get_xxx）。

**方案**：

| 方案 | 做法 | 成本 |
|---|---|---|
| A | 每个测试文件加 `autouse` fixture 清缓存 | 低，但要找全所有缓存点 |
| B | 改成 pytest-xdist 分进程跑 | 低，但 CI 要改 |
| C | 把缓存改成 `ContextVar` | 高 |

**建议 B**（`pytest -n auto`）—— 最省事，且分进程天然隔离。

---

## 3. 长期（P2）：能力扩展

### 3.1 多模态输出

**现状**：能发文字 / 图片 / 表情包 / 语音（可选）。

**可加**：

| 能力 | 依赖 | 价值 |
|---|---|---|
| 发文件（已有 `write_file` + `_format_text_attachments`） | 无 | 中 |
| 发视频 | NapCat `upload_group_file` | 低 |
| 语音合成 | TTS 服务 | 中 |

**风险**：视频/语音的体积与转码成本。`media_prepare_cache` 已经 14MB。

### 3.2 记忆的语义召回

**现状**：`memory.long_term_semantic` 默认 OFF，`sqlite-vec` + 远程 embedding。

**问题**：`embedding_cache.json` 40MB / 7672 条（上限 8000）。

**可做**：
- 缓存换 LRU + 更小的向量（1024 → 512 维）
- 或改用本地 embedding 模型（省 API 调用）

**验收**：缓存 < 20MB，召回质量不降。

### 3.3 多 Agent 协作已就绪但未启用

**现状**：`agent/society_review.py` / `orchestrator.py` / `devloop.py` 都有实现，
但生产没开（`capability_layer.*` 默认 OFF）。

**判断**：这是「有代码无入口」的典型（见 `ENGINEERING_DEBT_LEDGER` 的 D-43）。
**要不要接入需要 owner 决定**——它会显著增加 token 消耗。

---

## 4. 明确不做

| 不做 | 原因 |
|---|---|
| 商业化 / Marketplace / 付费层 | VISION 明确的边界 |
| 多租户 / 多 owner | 同上 |
| 企业权限扩展 | 同上 |
| 可视化流程编辑器 | 与「自然语言指挥」的定位冲突 |
| 把 `CognitiveModule` 强行推给 34 个模块 | 内部缓存/锁/TTL 都在工作，强改引入 bug |

---

## 5. 扩展点总览

改代码前先看这张表，找到对应位置：

| 想加什么 | 文件 | 注册在哪 | 文档 |
|---|---|---|---|
| **一个工具** | `tools/builtin/xxx.py` 写 `make_xxx(core)` | `register_all_builtin()` | [CONTRIBUTING §2.2](../CONTRIBUTING.md) |
| **一个 pipeline 阶段** | `pipeline/stages/xxx.py` 写 `async def xxx_stage(ctx)` | `handler.py::_build_pipeline()` **两处** | 同上 |
| **一个业务模块** | `cognitive/xxx.py` 写 `get_xxx()` | `registry.py::default_specs()` | 同上 |
| **一个 Channel** | `channels/xxx.py` 继承 `Module` | `cli.py` `core.register()` | — |
| **一个 Skill** | `skills/builtin/` 写 manifest | 自动扫描 | — |
| **一个 WebUI 页面** | `webui/src/views/XxxView.vue` | `navigation.ts` 的 `routeMap` | — |
| **一条 owner 通知** | 调 `get_owner_notifier(core).notify(kind, text)` | 无需注册 | 本文档 §1.1 |

### 加东西的三条铁律

1. **别绕过 `registry`** —— 新业务模块必须登记，否则 shutdown 时不会落盘
2. **别忘两处 pipeline** —— `_build_pipeline()` 有普通路径和 astrbot 兼容路径
3. **别把内部实现给用户看** —— 新增出站路径必须接 `_sanitize_delivery`

---

## 6. 验收清单（每次改动）

```
□ pytest -q 全绿（或只有已知环境失败）
□ 若改 config 默认值 ⇒ default.yaml 同步改
□ 若改出站文本 ⇒ 跑 -k "sanitize or guardrail or fake"
□ 若改 registry spec ⇒ 跑下面的一致性检查
□ 部署后 journalctl 无 Traceback / EROFS
□ /api/modules/status 的 business_modules_summary 正常
```

registry 一致性检查：

```bash
python -c "
import sys, asyncio, tempfile; sys.path.insert(0,'.')
from anima.core.registry import ModuleRegistry, default_specs
from anima.core.config import load_config
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

---

## 7. 怎么用这份文档

**要决定做什么时**：看 §1（近期）/ §2（中期），按 P0 → P1 → P2。

**要改代码时**：看 §5 找对应位置，再看 [`CONTRIBUTING.md`](../CONTRIBUTING.md) 的规则。

**要判断「这个该不该做」时**：先看 §4 的「明确不做」。

**发现新问题时**：登记进 [`ENGINEERING_DEBT_LEDGER.md`](../ENGINEERING_DEBT_LEDGER.md)，
不要直接加进本文档 —— 这里是**计划**，那里是**问题**。
