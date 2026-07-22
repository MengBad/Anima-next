# Anima 优化计划（自主发现 + 人性化情感表达）2026-07-18

对齐目标：bot **自主发现并完成/排队任务**、对问题做**有温度的回答**。不扩大静默改代码/改人格面。

## 主轴 A — 自主发现与完成任务

| 优先级 | 问题 | 预期结果 | 风险 | VPS 验证 |
|--------|------|----------|------|----------|
| **A1 P0** | SelfCode 超时洪水 + 生产 synth 0.85 | 熔断：过多 running/failed 时跳过新提案；默认冷却/概率保守；生产配置降噪 | 自升级变慢 | journal 出现 throttle/skip；failed 不再分钟级暴增 |
| **A2 P0** | 随机玩具能力优先于故障工单 | 有 open fault 时不合成玩具目标（已有）+ 合成 prompt 偏向真实缺口 | 低 | 有工单时日志「跳过随机自升级」 |
| **A3 P1** | auto_repair 曾致 PermissionError | 保持 auto_repair=false；仅扫描+Owner 审批 | 修复变慢 | config override false；无 crash loop |
| **A4 P1** | 卡住的 running 任务脏队列 | 计数熔断即可；可选不强制清库 | 低 | running 计数纳入熔断 |

## 主轴 B — 人性化 / 有情感的回答

| 优先级 | 问题 | 预期结果 | 风险 | VPS 验证 |
|--------|------|----------|------|----------|
| **B1 P0** | 解题时偶发百科腔/干巴失败 | persona + 内联 SYSTEM_PROMPT 增加「先接情绪→再给办法→工具失败带情绪」 | 话变长 | 单测断言模板含关键句；线上抽日志 |
| **B2 P1** | 护栏改写句偏机械 | 保持现有猫娘改写句，不改 anti-steal | 低 | gate 单测仍绿 |
| **B3 P2** | 反刍已有情感笔记 | 保持；接线 drift 预算诚实化 | 预算过紧 | daily_spent 不秒级烧穿 |

## 次要

| 项 | 说明 |
|----|------|
| S1 | `DriftConfig` 接到 `SelfLearning`；默认 daily_budget 提到 1.5 与运行时一致 |
| S2 | WebUI health 401 属鉴权设计，不改 |
| S3 | 不重开 unrestricted auto_repair / 不自动 install SelfCode |

## 本轮落地范围（必须上 VPS）

1. 代码：自升级熔断纯函数 + danger 调用  
2. 代码：drift 接线 + DriftConfig 默认 1.5  
3. 代码/配置：persona 情感解题路径  
4. 生产：降噪 `self_improvement_synth_prob/cooldown`、`drift.daily_budget`  
5. 测试：熔断、drift、persona 关键字、原有 gate  

## 非目标

- Phase 4 skills 大改、PR 栈、无审批自动装工具  
- 保证 Claude CLI 永不超时（外部依赖）  
- 手工清全部历史 failed task ID（非成功门禁）
