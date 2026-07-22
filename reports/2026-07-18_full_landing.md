# 全量建议落地报告（2026-07-18）

## 已落地项

| 建议 | 实现 |
|------|------|
| 熟人乱回收紧 | Gate B ≤12 字 + 短语表；生产 familiar_min=0.55 |
| 防抢话 | other_bot 闸门 + 遥测 `gate_skip_other_bot` |
| 旧梗粘滞 | 护栏 `_looks_like_unprompted_fixation` + 笔记/世界观 purge |
| SelfCode 洪水 | 熔断 + **僵尸 running 取消**（CLI/心跳） |
| danger 降噪 | 生产：关 core_mutation/memory_infection/stance；自升级低概率 |
| 群策略 | 主群改为 **mention**（仅 @/哈基米） |
| 观测 | reply_quality 闸门跳过桶 + fixation 改写计数 |
| 玩具欲望 | 低强度 self_improvement 批量 resolved |
| auto_repair | 保持 false |

## 命令

```bash
# 本地/VPS
anima ops-cleanup --data-dir data
# 或
python -m anima ops-cleanup --data-dir /opt/anima/data
```

## 生产验证点

- `owner_tasks` selfcode running → 0 cancelled 堆积
- self_notes 无「小鱼干/不叫沙琪玛/朗姆酒」粘滞行
- world_model entries 下降且无粘滞 belief
- journal: `[回复闸门] skip reason=...`
- 喊沙琪玛沉默；喊哈基米可回

## 生产落地结果（VPS 2026-07-18 05:50 UTC）

| 项 | 结果 |
|----|------|
| commit | `208e185` |
| service | active @ 05:50:24 UTC |
| selfcode | running→**cancelled 48**（heartbeat ops_cleanup） |
| notes purge | 删 33 粘滞行；小鱼干/沙琪玛/朗姆酒 **False** |
| world_model | 32→12，sticky **0** |
| 主群策略 | **mention**（727631526 / 552994229） |
| danger | core_mutation/memory_infection/stance/active_info **关**；synth 0.05 |
| auto_repair | false |

证据：`vps_full_landing.log`

## 后续建议（落地后再迭代）

见会话最终回复。
