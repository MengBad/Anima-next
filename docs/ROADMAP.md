# 扩展计划（ROADMAP）

> **本文档是唯一的路线图。** 根目录 `ROADMAP.md` 停更于 2026-08-17，仅作历史参考。
>
> 与其它文档的分工：
> - [`VISION.md`](VISION.md) —— 定位与边界
> - [`ARCHITECTURE_CURRENT.md`](ARCHITECTURE_CURRENT.md) —— 实现层
> - **本文档** —— 接下来做什么、为什么、怎么验收
> - [`ENGINEERING_DEBT_LEDGER.md`](../ENGINEERING_DEBT_LEDGER.md) —— 已发现的问题
> - [`CONTRIBUTING.md`](../CONTRIBUTING.md) —— 怎么改代码

最后更新：2026-11-27

---

## 0. 当前坐标

> 最后校准：**2026-11-27**（此前为 2026-10-28，以下数字均已按生产实测更新）

| 维度 | 状态 |
|---|---|
| 版本 | `0.3.0rc2`（未变，但底层网关已换代） |
| 测试 | 6600+ 用例；核心相关模块 637 项全绿 |
| 工程债 | 44 条，**1 个 OPEN**（D-41，已部分实现） |
| 生产 | `active`、`NRestarts=0`；今日 LLM 562 次调用、零输出 0 |
| **模型网关** | **CPA → Stravia**（2026-11-27 完成迁移，7 个供应商） |
| 出站通道 | 双通道（WS + HTTP 回退）；断连/失败均有告警 |
| 磁盘 | 51%；`data/` 857MB；`embedding_cache` 39MB / 7112 条 |

### 已完成（2026-11-27 本轮）

| # | 事项 | 证据 |
|---|---|---|
| 1 | **模型网关换代：CPA → Stravia** | CPA 容器已下线；Stravia 占 8317 + CF 443；6 provider 启用 |
| 2 | 自研 WorkBuddy Bridge 插件 | 腾讯 CodeBuddy 流式转换 + 渠道消毒 + token 刷新；26 模型同步 |
| 3 | 出站内容净化（三类漏洞） | `（附带表情）` / `<img src="file://...">` / 裸 file 路径全部剥离 |
| 4 | 告警落盘 + 补发 | 通道全灭时证据不丢；恢复后 `_flush_persisted_alerts` 补发 |
| 5 | NapCat 断连告警 | 连续 10 次失败告警一次（此前 689 次重试无人知晓） |
| 6 | iterate 预算闸门 | 输入 >20k 字符直接跳过；冷却 1h→6h（此前每天 10 次全废） |
| 7 | 僵尸单归档 | 131 条 `failed/expired/rejected` → `archived` |
| 8 | 文档精简 | 根目录 md 19→8；63 个历史文档入 `docs/archive/` |

### 已完成（2026-10-28 之前，仍在生效）

| # | 事项 | 证据 |
|---|---|---|
| 1 | 统一模块注册表（33 模块） | `registry` 启动日志 ok=33 failed=0 |
| 2 | Bot 自判（`is_robot` + 评分制） | 394 成员实测 bot 8/8、真人 0/386 误判 |
| 3 | 部署停机竞态修复 | 连续 3 次部署 EROFS 0 |
| 4 | README + 架构 + 贡献指南重写 | 302 / 337 / 380 行 |

### 本轮暴露、尚未闭合

| 项 | 现状 | 影响 |
|---|---|---|
| `gateway.send()` 无净化参数 | 直发路径仍靠调用方自觉 | 主动发言/Agent 交付可能漏净化 |
| D-41 私聊通道 | 台账仍 `OPEN`，但 `owner_agent` 等已直发私聊 | 主动产出仍以群为主 |
| 交互量偏低 | 今日 562 次调用仅 7 条回复（`gate` 闸掉 127 次 idle） | 需判断是流量稀疏还是闸门过紧 |
| NapCat 未托管 | `systemctl is-active napcat` = inactive | QQ 掉线需手动拉起 |

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

## 3.5 未来展望（2027 视角）

> 本节是**方向判断**，不是承诺。每条都标了「现在能不能做」，避免变成空想清单。

### 3.5.1 三阶段演进

| 阶段 | 主题 | 判据（做到什么算进入下一阶段） |
|---|---|---|
| **现在 → 2026 Q4** | **运行底座巩固** | 连续 30 天 `outbound_failures=0`；无人工救场部署；告警闭环率 >90% |
| **2027 H1** | **主动性与交付能力** | owner 私聊成为主要交付面；Agent 任务自主完成率 >70%；Goal 持续推进不停摆 |
| **2027 H2+** | **数字生命的连续性** | 跨月记忆一致性可测；人格漂移在窗口内收敛；自我叙事有可追溯的因果链 |

**为什么这个顺序**：底座不稳时做主动性 = 放大故障；主动性不足时谈"生命感" = 自说自话。

### 3.5.2 四条值得投入的主线

#### 主线 A：网关层从「能用」到「看不见」

现在 Anima 还知道自己在用 Stravia。目标是把这层彻底透明化：

| 现状 | 目标 | 卡点 |
|---|---|---|
| 手改 `config.yaml` 切模型 | Stravia 内部按健康度自动选路，Anima 无感 | Stravia 已支持多目标层，缺 **健康度反馈回路** |
| 主模型下架需人工发现 | 上游 404/400 自动摘除 + 告警 | 已有 `credential_status`，缺「模型级」健康度 |
| 单点故障靠 fallback 链 | 按延迟/成本/成功率动态加权 | 需要 Stravia 侧 usage 数据回流 |

**现在能做**：把 Anima 的 `llm_usage` 成功率按模型聚合，反哺 Stravia 路由权重。改动小、收益直接。

#### 主线 B：出站内容质量从「事后补」到「事前防」

这两天的三个 bug（`（附带表情）`、`<img file://>`、来源标注）都是**同一类**：模型输出了它"以为该有"的格式，而系统没有兜底。

| 层次 | 现状 | 下一步 |
|---|---|---|
| 事后剥离 | ✅ 已有（3 类正则） | 扩充到「markdown 残留」「伪引用块」 |
| 事前引导 | 部分（prompt 写"不要"） | **改成正例示范**（写什么，而不是不写什么） |
| 结构约束 | ❌ 无 | 出站前做 `OutboundMsg` 结构校验（只允许 text/image/at 三类段） |

**判断**：结构约束是最值钱的一层——不管模型吐什么，出站只认白名单段类型。

#### 主线 C：交互密度（当前最大隐忧）

今日 562 次 LLM 调用 → **7 条回复**。`gate_skip_idle=127`、`gate_skip_unfamiliar=34`。

| 可能原因 | 判据 | 处置 |
|---|---|---|
| 群真的没话说 | 入站总量本身低 | 正常，不干预 |
| idle 闸太紧 | 入站不少但 90% 被闸 | 放宽阈值或加"久未发言"补偿 |
| 陌生人不回是对的 | 陌生入站占比高 | 保持现状 |

**现在能做**：加一个「入站→闸门→回复」的三段漏斗埋点，一周后看数据再决定。**不要凭感觉调闸门。**

#### 主线 D：工程纪律自动化

本轮手动做了很多可自动化的事：

| 手工动作 | 可自动化 | 价值 |
|---|---|---|
| 提交前跑测试 | pre-commit hook | 防脏树部署 |
| 检查失效链接 | CI 脚本 | 文档精简后必做 |
| 归档僵尸任务单 | 定时任务（>30 天自动 archived） | 台账清爽 |
| 文档时效检查 | CI 检查 `[HISTORICAL]` 标记与最后修改时间差 | 防文档腐烂 |

### 3.5.3 明确不追的风口

| 不追 | 理由 |
|---|---|
| 多模态视频生成 | 成本/延迟与 QQ 场景不匹配 |
| 多 Agent 辩论（`society_review`） | 有代码无入口，token 消耗大；先证明单 Agent 交付率 |
| 本地大模型 | 3.9G 内存机器跑不动有意义的模型 |
| 语音交互 | owner 已否决 |

### 3.5.4 一个诚实的判断

这个项目最稀缺的不是功能，是**"它真的在活着"的可验证证据**。

- 今天能证明的：它会记忆、会反刍、会模拟生活、会说人话
- 今天证明不了的：跨月它还是"同一个它"吗？它记得的承诺会兑现吗？

**下一步最该投入的，是把"连续性"变成可测量的指标**，而不是再加一个能力。

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
