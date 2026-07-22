# Anima 生产模块健康审计（2026-07-18）

> 证据：`{SCRATCH}/vps_health_pre.log`（SSH 实机）、`{SCRATCH}/pytest_gates.log`  
> 主机：`pve.aicaowo.com` `/opt/anima` · 服务 `anima` · 部署 commit **6c5cdd0**（审计时刻）

## 总表

| 模块 | 状态 | 证据摘要 |
|------|------|----------|
| Gateway / systemd | **OK** | `active`，`ActiveEnterTimestamp=2026-07-18 05:10:30 UTC`，NRestarts=0 |
| Deploy / version | **OK** | `version.json` commit `6c5cdd053dd6` branch `codex/webui-0.3-rc2` |
| Doctor | **OK** | Python/依赖/数据可写/NapCat WS/LLM/向量记忆均 OK |
| QQ OneBot11 | **OK** | doctor NapCat `ws://127.0.0.1:3001` 可达；journal 有渠道流量 |
| Pipeline 回复闸门 | **OK** | 本地 gates 49 passed；含 other-bot 反抢话 `is_addressed_to_other_bot` |
| Memory | **OK** | `chat_memory.json` 更新于 05:10；向量 doctor OK |
| Rumination / Learning | **OK** | `反思已保存` / 语义去重；`daily_spent=0.2`；笔记 307 行 |
| Drift 配置接线 | **WARN** | YAML `drift.daily_budget=0.3` 未真正驱动 `SelfLearning`（代码写死 1.5） |
| Desire / world_model | **OK** | desires=10；world_model entries=32；身份稳定度 1.0 |
| Tools / ReAct | **WARN** | 故障工单 open：`empty_should_reply`、`tool_spins`（待 Owner/受控迭代） |
| Heartbeat / autonomy | **OK** | heartbeat 持续写；群消息计数活跃（727631526 等） |
| Danger 关系/追问/记忆感染 | **OK** | journal 有「关系推断」「主动追问」「记忆感染检查」 |
| Danger 自升级 SelfCode | **FAIL→目标修复** | selfcode failed=17 running=48；Claude 超时 rc=124；synth_prob 生产 0.85 过激 |
| Controlled iteration | **OK（安全）** | `auto_repair=false` 覆盖；扫描在跑；不静默改配置 |
| Core mutation | **OK（武装未开火）** | 开关开但无 `persona_core.proposed.yaml` |
| Persona / 护栏 | **OK** | persona 文件在位；guardrail 单测绿 |
| WebUI | **WARN** | `:8088` 监听；`/api/health` 401（需 token，属预期） |

## 详细证据指针

- 服务/doctor：`vps_health_pre.log` sections `service` `version` `doctor`
- 认知状态：section `key_state`（learning / rumination / tasks / desires）
- 高危配置：section `cfg_danger_iter_drift` — `self_improvement_synth_prob: 0.85`、`cooldown: 300`
- 自升级失败：section `journal_cognitive` / `journal_errors`；selfcode_audit 超时
- 反刍正常：`anima.learning INFO 反思已保存` 多条（03:26–04:59）
- 本地闸门：`pytest_gates.log` → **49 passed**

## 阻塞自主发现 / 情感表达的项

1. **P0** SelfCode 洪水（running 堆积 + Claude 超时）→ 挤占「发现真故障→排队」路径  
2. **P0** 生产自升级合成概率过高 → 玩具目标淹没真实工单  
3. **P1** drift YAML 与代码脱节 → 运维误判预算  
4. **P1** 故障工单已开但自动修关闭 → 正确；应靠 Owner/受控路径，不靠玩具 selfcode  
5. **P1** 人格提示对「问题解决 + 情绪接住」可再强化（已有气质，缺显式解题路径）

## Post-deploy 验证（2026-07-18）

证据：`{SCRATCH}/vps_health_post.log`、`vps_throttle_journal.log`、`vps_throttle_evidence.log`、`api_health.json`、`pytest_post.log`

| 检查项 | 结果 |
|--------|------|
| 服务 | **active**，`ActiveEnterTimestamp=2026-07-18 05:27:37 UTC`，NRestarts=0 |
| 部署 commit | **a8ec0f0**（含熔断 + 情感解题 + 心跳发现路径） |
| doctor | 全 OK（含 NapCat / LLM / 向量） |
| 自升级降噪 | journal 每 tick：`自升级熔断 skip reason=too_many_running running=48` |
| SelfCode 洪水 | running 维持 48，**未再增长**；无新 PermissionError/Traceback |
| auto_repair | **false** |
| drift.daily_budget | **1.5**（已接线） |
| anti-steal 实机 | VPS `is_addressed_to_other_bot` 断言通过 |
| persona 情感解题 | `解决问题时` / 温暖句在 `/opt/anima/config/persona_core.yaml` |
| WebUI /api/health | **200** running/channel_connected/heartbeat_running true |
| 本地 pytest | **47 passed**（gates + 熔断 + persona/drift） |

### 模块状态更新（相对审计前）

| 模块 | 审计前 | 现在 |
|------|--------|------|
| Danger 自升级 | FAIL（超时洪水） | **OK（熔断）** — 发现路径仍在，不再开新 SelfCode |
| Drift 接线 | WARN | **OK** |
| Persona 情感解题 | 弱 | **OK**（显式段落已部署） |
| 自主发现调度 | 依赖主动发言群 | **OK** — 每 heartbeat 扫描/熔断 |
