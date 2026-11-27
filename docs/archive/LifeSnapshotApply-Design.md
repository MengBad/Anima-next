# 一生「快照恢复」staged→live 生效 · 设计文档

> 状态：设计定稿（未启用写生效端点） · 日期：2026-08-25 · 生效开关需 Owner 拍板
> 隶属：纵1 一生视图 · 底座「身份快照恢复」最后一步

## 1. 两个层级的区分

- 人格 persona = 静态配置：config/persona_core.yaml（宪法/基底），运行时载入。
- 授权身份 identity = 权限模型：QQ号+群号分级 + owner，由配置/权限系统管理。
- 身份快照 = 生命周期数据：某时刻序列化的 identity_*.json / persona_*.bak / *snapshot*.json，随时间演化，用于传承/回滚。

已交付的 /api/life/snapshot/restore（Owner 门控）只做『可逆暂存』（data/life_restore/restored/ + backups/<ts>/，原子替换、可回滚），未触碰 live 身份/记忆/chat_memory。

## 2. staged→live 生效的目标域

| 目标 | live 生效位 | 生效方式 | 约束 |
|---|---|---|---|
| 人格候选（persona_*.bak / 提案） | config/persona_core.yaml | 原子替换 | 唯一闸：Owner 显式 confirm 开启 SAFE_APPLY_PERSONA；改前备份到 data/life_restore/backups/live/<ts>/；改后 Persona 热更或重启；留回滚 |
| 身份快照（identity_*.json） | data/identity_<active>.json（若有权威位） | 原子替换 | 仅该命名空间内；恢复前备份；可回滚 |
| 永不生效 | chat_memory.json / data/life_restore/ 之外 | 拒绝 | hard deny（内存大文件、隔离前身、日志） |
| 配置 non-persona | config/*.yaml（token/QQ 等） | 拒绝 | 部署纪律：不覆盖生产 config.yaml |

## 3. 安全协议（生效端点才启用，默认 OFF）

1. SAFE_APPLY_PERSONA=false 时 /api/life/snapshot/apply 一律 403（fail-closed）。
2. 生效需 owner（Bearer 门控）+ confirm=true + 显式 target 与快照匹配。
3. 改前强制备份当前 live 位（带时间戳）；走『临时文件→os.replace』原子替换。
4. 写后立即读取校验（JSON/UTF-8/最小长度），失败则自动回滚到备份并报错。
5. 每一步 audit.emit（life_snapshot_applied / life_snapshot_rollback）留痕。
6. 失效时回滚端点可随时用最近一次备份恢复。

## 4. 与既有纪律的关系

- 不覆盖生产 config.yaml：persona 为独立文件，启用开关后仅改 persona_core.yaml（人格层，非连接/密钥），且改前备份+可回滚；token/QQ 等其余配置一律拒绝。
- 部署不因本文档改变：CI 不自动改码/部署/绕过失败。
- 前端只展示已暂存与回滚（Owner 操作）；『生效』按钮在 SAFE_APPLY_PERSONA 开启且 Owner 确认后才出现。

## 5. 待办（启用前必须 Owner 拍板）

- [ ] 是否开放 SAFE_APPLY_PERSONA（默认 OFF）。
- [ ] 确定『生效位』权威清单（尤其身份 active 位命名）。
- [ ] 实现 /api/life/snapshot/apply + rollback_live + 相应测试。
- [ ] 前端将『生效』按钮仅展示给 Owner 且在门控开启时可用。
