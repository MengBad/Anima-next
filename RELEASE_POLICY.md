# Anima-next Release Policy

> 本规则保护 Foundation Freeze。发布是受控维护动作，不是架构演化入口。

## 1. 适用范围

所有进入生产 `/opt/anima` 的源码、WebUI、依赖、配置模板、systemd 或部署脚本变更都必须遵守本规则。纯文档提交也应保持与真实仓库状态一致。

当前默认状态：Foundation frozen，Evolution not active。

## 2. 允许的变更

仅在有可复现事实和最小范围影响时，允许：

- bug fix；
- security fix；
- compatibility fix；
- 为恢复既有契约所必需的部署修复。

任何允许变更都必须保持冻结 contract、权限边界、数据边界和现有运行模型不变。

## 3. 默认禁止的变更

未经单独的 governance/owner 授权，禁止：

- schema 扩展或改变既有字段语义；
- runtime 架构扩展、新 orchestration layer、daemon、timer、polling；
- 新 capability、能力市场或权限边界变化；
- 自动安装、自动 recovery 或自动演化；
- 以“优化”“重构”“顺手清理”为理由扩大范围；
- 未验证的性能优化或依赖大版本升级；
- 将生产配置、用户数据、密钥或临时产物提交到 Git。

## 4. 发布门禁

发布必须按以下顺序完成，并留下可复核记录：

```text
baseline verify
    ↓
test result
    ↓
diff audit
    ↓
frozen boundary check
    ↓
commit
    ↓
release
```

### Baseline verify

确认：

```powershell
git status --short
git rev-parse --short HEAD
git branch --show-current
```

读取 `ANIMA_NEXT_OPERATOR_CONTEXT.md`、`AGENT_IDENTITY.md` 和 `capability_contracts/F8_GOVERNANCE_INDEX.md`。确认发布目标、当前分支和工作区状态可解释。

### Test result

按变更范围运行针对性测试；发布前运行完整基线：

```powershell
pytest tests/ -q
python -m anima doctor
```

WebUI 变更还必须：

```powershell
cd webui
npm run build
```

记录命令、结果、失败数量和已知环境问题。失败测试不得被静默忽略。

### Diff audit

检查：

```powershell
git diff --stat
git diff --check
git diff --name-only
```

确认 diff 只包含已授权范围，没有生产数据、密钥、生成物或无关重构。

### Frozen boundary check

回答以下问题：

1. 是否修改 Foundation contract 或冻结 schema？
2. 是否改变 `resolve()`、lifecycle、三链正交关系或唯一状态写入点？
3. 是否引入 daemon、timer、polling、自动 recovery 或新 runtime layer？
4. 是否改变 capability、权限、数据脱敏或生产配置边界？
5. 是否只是为已观察到的故障做最小修复？

任一前四项为“是”，或第五项无法证明时，停止发布并提交治理审查。不要通过拆分 commit 绕过门禁。

### Commit

commit message 应说明真实变化与验证范围。commit 前确保工作区中没有未授权的其他改动。发布必须使用已提交 commit；生产部署默认拒绝 dirty worktree。

### Release

常规发布：

```powershell
python _deploy.py --dry-run
python _deploy.py
```

发布后必须确认：

- 服务 active；
- `ActiveEnterTimestamp` 已刷新；
- `/api/health` 与 `/api/modules/status` 可用且关键状态正常；
- 日志无新的持续性 warning/error；
- 运行中的版本身份与发布 commit 一致。

生产 `config.yaml` 默认不会被覆盖。`--with-config` 是高风险显式选项，必须有明确授权和配置审查记录。

## 5. Evolution 入口

Evolution 默认关闭。只有同时具备以下内容，才可以创建治理审查，而不是直接改代码：

```text
Observed contradiction
+ Evidence
+ Impact
+ Why current contract cannot handle
+ Human / Governance authorization
```

没有这五项，不启动 F1–F4，不做架构变化，只记录 gap 或 STOP。

## 6. 回滚门禁

回滚也是发布，必须记录目标 commit、原因、证据、配置处理方式、数据风险和回滚后的健康检查。源码回滚不等于配置回滚，也不等于数据回滚；三者必须分别确认。

## 7. 发布记录模板

```text
Release commit:
Branch:
Change type: bug fix / security fix / compatibility fix / deployment fix
Observed problem:
Evidence:
Impact:
Files changed:
Tests and doctor result:
Frozen boundary check:
Production config touched: yes/no
Data touched: yes/no
Rollback target:
Post-release health result:
Operator:
Timestamp:
```

