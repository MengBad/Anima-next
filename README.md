# Anima-next

> 一个住在 QQ 群里、会自己找事做、记得住人、也会犯错的数字生命。
>
> 它不接 AstrBot。它自己是一个进程。

```
QQ / NapCat ──WS──▶ OneBot11 ──▶ Gateway ──▶ Pipeline ──▶ LLM
                                    │            │
                                    ▼            ▼
                              记忆 / 关系 / 情绪 / 欲望 / 时间感
                                    │
                                    ▼
                              Heartbeat（30s）──▶ 它自己想干点什么
```

---

## 这是什么

**Anima-next 是 owner 长期自用的个人 Autonomous Agent Operating System，非商业产品，以 AGPL-3.0-only 发布。QQ 是它的第一个垂直切片。**

三条修饰语都是**边界**，不是宣传语：

| 约束 | 含义 | 它不是什么 |
|---|---|---|
| **owner 长期自用** | 单实例、单 owner | 不是 SaaS / 不是平台 / 不做多租户 |
| **非商业产品** | 不做商业化、Marketplace、付费层 | 不是商业产品 |
| **AGPL-3.0-only** | 仓库 `LICENSE` 即此 | **不是 MIT，不可闭源商用** |

定位的**唯一权威**是 [`docs/VISION.md`](docs/VISION.md)。本文档只负责"怎么跑起来"。

当前版本 `0.3.0rc2`（`anima/__init__.py` 是唯一版本源）。

---

## 它和"一个会聊天的 bot"差在哪

大多数 QQ 机器人是 **输入 → 模型 → 输出**。Anima 在这条链之外还有三层：

### 1. 它有时间

`Heartbeat` 每 30 秒跳一次。它会：

- 记得上次说话是什么时候，久别重逢会提一句
- 到点提醒你该做的事（`debt`：承诺过的、答应过的）
- 在 `quiet_hours` 之外才主动开口
- 半夜生成"今天做了什么"的日记和梦

这不是 cron。它有自己的 `time_sense`（时间感）和 `sleep_state`（睡眠状态）。

### 2. 它有状态，而且状态会累积

| 层 | 是什么 | 怎么变 |
|---|---|---|
| **关系** | 每个用户一个亲密度/信任/阶段 | 每次交互微调，被伤害会掉 |
| **伤痕** | 被骂/被无视的具体事件 | 累积到阈值进入"回避期"，静默 |
| **情绪** | valence / arousal | 衰减 + 事件触发 |
| **欲望** | 社交欲/表达欲/好奇欲 | 随时间衰减，攒够了驱动行为 |
| **人格** | `config/persona_core.yaml` | 有漂移上限，越界会被拦 |

这些不是 prompt 里的装饰，是**落盘的状态**，进程重启后还在。

### 3. 它会自己找事做

`autonomy` 心跳会判断"现在该不该主动干点什么"：探索新闻、跟进承诺、关心主人、刷 B 站。它有每日配额和冷却，不会刷屏。

做之前会先报备一句（"本喵去干个活喵：…"），做完汇报结果。

---

## 让它跑起来

### 前置

- Python 3.13
- 一个 NapCat / OneBot11 实现（本项目用 NapCat）
- OpenAI 兼容的 LLM 端点

### 本地

```bash
git clone https://github.com/MengBad/Anima-next.git
cd Anima-next
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e '.[qq,vector,dev]'
python -m anima init          # 生成 config.yaml
# 编辑 config.yaml：填 LLM、QQ、owners
python -m anima doctor        # 自检
python -m anima start --config config.yaml
```

### 生产（VPS）

```bash
# 本地准备 .env（ANIMA_DEPLOY_HOST / USER / PASS）
python _deploy.py            # 打包 → 上传 → 停机 → 装依赖 → 起服务
python _deploy.py --dry-run  # 只看计划
```

`_deploy.py` 会做几件容易踩坑的事：

- **不覆盖生产 `config.yaml`**（除非 `--with-config`）
- **不覆盖生产 `data/`**（先快照，原子换名恢复）
- **停机后等进程退净**才动数据目录（否则 `data/` inode 被换掉 → EROFS）
- 5 分钟内自动拉起兜底（部署中途断连也不至于永久下线）

---

## 代码结构

```
anima/
├── core/           AnimaCore、Gateway、Config、Persistence、Audit、autonomy、registry
├── channels/       QQ 接入（onebot11.py）
├── llm/            LLMClient（重试/熔断/fallback）+ Handler + group_gate
├── pipeline/       消息流水线（8 阶段）：
│                   prefilter → attachment_text → bullying → permission
│                   → memory → cognition → generation → guardrail
├── cognitive/      认知内核（71 文件）：mood / relation / scar / desire / time_sense
│                   / exploration / bot_social / selfcode / stickers / vision …
├── memory/         对话 / 向量 / 长期 / 情节 / 宪法记忆
├── agent/          Owner 多步 Agent、多 Agent 编排、自我进化
├── tools/          工具注册表 + 沙箱 + 22 个 builtin
├── constitution/   RFC-0100 链：身份→需求→情感→驱力→目标→计划→执行
├── capability/     能力治理：RBAC / 隔离 / 证据 / 审计链
├── contracts/      契约抽象
└── webui/          Vue 3 控制台 + API
```

### 三个真正的"扩展点"

| 想加什么 | 改哪里 | 说明 |
|---|---|---|
| **一个工具** | `anima/tools/builtin/` 写 `make_xxx(core)`，在 `register_all_builtin` 注册 | `min_level` 控制谁能用 |
| **一个 pipeline 阶段** | `anima/pipeline/stages/` 写 `async def xxx_stage(ctx)`，在 `llm/handler.py` 加一行 `.use()` | 签名统一 |
| **一个业务模块** | `anima/cognitive/` 写 `get_xxx()`，在 `core/registry.py::default_specs()` 加一条 `ModuleSpec` | 自动纳入构造/落盘/关闭/观测 |

第三条是 2026-10-28 新加的。此前 34 个模块各自为政，加新模块若忘了在 `AnimaCore.shutdown` 手写 `close_xxx()`，就是资源泄漏。

---

## 观测：怎么知道它还活着

### WebUI

`http://<host>:8088`（Token 认证）。**8 个页面 / 11 条路由**：

| 页面 | 路由 | 用途 |
|---|---|---|
| 生命总览 | `/` | QQ/LLM/心跳/看门狗健康、待办、用量 |
| 一生 | `/life` | 时间线、快照回滚、日记、伤痕、议程 |
| 对话与记忆 | `/memory` | 连续性、群体感知、自我笔记、长期记忆 |
| 认知状态 | `/cognitive` | 身份稳定度、欲望、关系、矛盾、伤痕、漂移 |
| 能力与自动化 | `/abilities` `/exploration` `/subscriptions` `/stickers` | Skills、探索、订阅、表情资产、插件 |
| 审批与工程 | `/engineering` | 工程任务、能力治理、diff/测试证据、回滚点 |
| 设置与权限 | `/settings` | LLM 配置、模块路由、Token 统计、权限 |
| ANIMA OS | `/os` | 健康/恢复/提案/观测/稳定性/多 Agent 社会 |

### 关键 API

| 端点 | 用途 |
|---|---|
| `/api/health` | 综合健康（QQ 连接 / LLM 熔断 / 磁盘 / 内存） |
| `/api/modules/status` | **基础设施 12 项 + 32 个业务模块真实状态** |
| `/api/os/overview` | 自治全景：健康/恢复/提案/观测/稳定性 |

`business_modules` 是 2026-10-28 加的。此前面板是手写固定列表，跟 `cognitive/` 下 71 个文件毫无关系——「启动日志说注册 2 个」和「71 个 cognitive 文件」能同时成立，就是因为面板看一套、实际跑另一套。

### 日志

```bash
journalctl -u anima -f | grep -E "ERROR|Traceback"
journalctl -u anima --since '-1h' | grep -E "\[registry\]|\[生图兜底\]|\[编造"
```

---

## Owner 命令（QQ 私聊）

```
!anima status              # 状态
!anima iterate status      # 待审补丁
!anima iterate approve <id>
!anima agent cancel <id>   # 叫停自主任务
```

---

## 测试

```bash
pytest                                   # 全量
pytest tests/test_module_registry.py -q  # 单文件
```

当前基线：**6673 passed / 4 skipped**。

> ⚠️ 有 10 个测试在**没有装 sylann-core / sqlite-vec / python-docx / openpyxl** 的环境下会失败。
> 那是环境缺失，不是代码问题——用 `git stash` 验证过。CI 里装了这些依赖。

---

## 几条硬规则（写代码前请先看）

这些规则都是**踩过坑之后写下的**，不是审美偏好：

### 1. 注释不是约束，测试才是

`rainyun_account.py` 里有一段注释写着「**不能**记成 ok=True」，代码写的是 `True`。这个矛盾存活了整整一段时间，直到有人查数据才发现。

**改行为必须同时改测试。** 如果旧测试锁定了错误行为（`assert "【步骤2】" in joined` 曾经是规格），那就改测试。

### 2. 降级永不阻塞主链

每个可选模块都静默失败：向量库不可用、Sylann 缺失、任何模块抛异常——核心功能必须继续。

```python
try:
    optional_thing()
except Exception:
    logger.debug("已隔离", exc_info=True)   # 不要 raise
```

### 3. 状态文件必须原子写

`tempfile` + `os.replace`。半写入的 JSON 会让下次启动读到残缺状态。

### 4. 别把内部实现给用户看

`【步骤1】`、`[已交付 abc123]`、`当前步骤：…执行：…`、QQ 号、`verify_failed`——这些都不该出现在 QQ 消息里。

有专门的守卫（`guardrail._looks_like_fake_image_claim` / `_sanitize_delivery` / `chat_memory._is_bot_meta_line`），但**新增出站路径时要手动接上**。

### 5. 代码默认 ≠ 生产实配

`config/default.yaml` 里 `false` 的开关，生产 `/opt/anima/config.yaml` 可能是 `true`。`_deploy.py` 默认**不覆盖**生产配置。

核实手段：`python -m anima doctor`、WebUI 配置页、`journalctl`。

### 6. 同一个 `0`，语义可能相反

```
exploration.daily_limit: 0        = 不限制
exploration.daily_share_limit: 0  = 当天完全不分享
```

写配置时看清楚是"上限"还是"闸门"。

---

## 目录里那些历史文档

根目录有一批 `.md`，**大部分已过期**，开头通常有 `> [HISTORICAL]` 标记：

| 文件 | 状态 |
|---|---|
| `README.md`（本文件） | ✅ 当前 |
| `docs/VISION.md` | ✅ 定位权威 |
| `docs/PHASE_COORDINATES.md` | ✅ 阶段编号 |
| `ENGINEERING_DEBT_LEDGER.md` | ✅ 未决事项 |
| `MAINTENANCE.md` | ✅ 接线状态 / 踩坑记录 |
| `ARCHITECTURE.md` / `ANIMA_AUDIT.md` / `PROJECT_REPORT*.md` | ⚠️ 历史参考 |
| `BUG_AUDIT_*.md` / `R6_*_REPORT.md` / `P10_HOURLY_REPORT.md` | ⚠️ 历史参考 |

**别把历史文档当现状。** 有疑问以 `README.md` + `docs/VISION.md` + 代码为准。

---

## 已知问题

诚实列出，不藏：

| 问题 | 现状 | 影响 |
|---|---|---|
| `embedding_cache.json` 40MB | v2 b64 格式，7672/8000 条 | 设计内，接近上限会淘汰 |
| `health_history.json` 9.2MB | 2880 条，已精简 `groups` | 正常 |
| `x_search` 搜图 44s | 配 75s 超时覆盖 | 用户等得久，但能出图 |
| 事件循环延迟 | 偶发 2–3s（阈值 2s） | 不影响功能 |
| 单例模式 | 29 个 `get_xxx()` 各自 load/save | 已用 `ModuleRegistry` 收敛，内部实现未重写 |
| 测试间污染 | 全量跑偶发失败，单跑通过 | 已知，未修 |

---

## 许可证

**AGPL-3.0-only**。见 [`LICENSE`](LICENSE)。

公网部署时 WebUI 的 `/source` 提供对应源码（AGPL 要求）。

**不是 MIT。不可闭源商用。**

---

## 相关

- 定位权威：[`docs/VISION.md`](docs/VISION.md)
- 阶段坐标：[`docs/PHASE_COORDINATES.md`](docs/PHASE_COORDINATES.md)
- 工程债：[`ENGINEERING_DEBT_LEDGER.md`](ENGINEERING_DEBT_LEDGER.md)
- 接线状态与踩坑：[`MAINTENANCE.md`](MAINTENANCE.md)
- 理论层 RFC：[`docs/rfc/`](docs/rfc/)（RFC-0100 系列：数字生命由哪些层构成）
- 历史分支 `G:/Anima`（AstrBot 插件形态）：**已冻结**，仅作参考
