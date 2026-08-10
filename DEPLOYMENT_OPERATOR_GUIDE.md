# Anima-next Deployment Operator Guide

> 本文档只描述已有部署与运维流程，不引入新的 runtime、schema 或 capability。
> 当前系统处于 Foundation Maintenance Mode：先观察和验证，再处理真实故障。

## 1. 当前部署模型

- 生产目录：`/opt/anima`
- 生产服务：`anima.service`
- 运行用户：`anima`
- Python 虚拟环境：`/opt/anima/venv`
- 生产配置：`/opt/anima/config.yaml`
- 运行数据：`/opt/anima/data/`
- WebUI/API：默认监听配置中的 `webui.host` 与 `webui.port`，通常为 `8088`
- QQ 接入：NapCat/OneBot11 WebSocket，由生产配置中的 `qq.napcat_ws_url` 指定

本仓库是唯一生产源码。历史 `G:/Anima` 项目已冻结，不作为发布源。

## 2. 接管前检查

新 operator 必须先读取：

1. `ANIMA_NEXT_OPERATOR_CONTEXT.md`
2. `AGENT_IDENTITY.md`
3. `capability_contracts/F8_GOVERNANCE_INDEX.md`
4. 本文档与 `RELEASE_POLICY.md`

随后核对：

```powershell
git status --short
git rev-parse --short HEAD
git branch --show-current
```

若发现未授权的代码、配置、schema 或数据变更，先停止发布并记录事实；不要用清理命令覆盖现场。

## 3. 环境要求

开发/发布环境要求 Python 3.12+，并按项目声明安装依赖：

```powershell
pip install -e ".[qq,vector,dev]"
```

生产环境需要可用的 Linux systemd、Python 3、网络访问，以及已配置的 LLM、NapCat 和 WebUI 凭据。密钥只放在生产配置或受控环境变量中，不写入仓库、日志、截图或报告。

关键配置来源：

- 模板：`config/default.yaml`
- 生产配置：`/opt/anima/config.yaml`
- 环境变量：以项目代码和生产配置为准，常见包括 `ANIMA_LLM_API_KEY`、`ANIMA_LLM_ENDPOINT`、`NAPCAT_TOKEN`

## 4. 安装与发布

常规发布从仓库根目录执行：

```powershell
python _deploy.py --dry-run
python _deploy.py
```

`_deploy.py` 会执行预检、同步已跟踪源码、安装依赖、保留生产配置与数据、刷新 systemd 单元、重启服务，并执行部署后的 smoke check。默认不上传本地 `config.yaml`；只有经过明确授权，且确认本地配置就是要上线的生产配置时，才使用 `--with-config`。

默认不允许脏工作区发布。`--allow-dirty` 仅用于已记录、经授权的紧急恢复；使用后必须在报告中写明原因、范围和对应 commit。

首次部署或换机器时，先准备 `/opt/anima/config.yaml`、权限、NapCat 连接和必要凭据，再运行发布脚本。不要把生产 `data/` 复制进 Git。

## 5. 启停与状态检查

在生产机上执行：

```bash
systemctl is-active anima
systemctl status anima --no-pager -l
systemctl restart anima
systemctl stop anima
systemctl start anima
systemctl show --property=ActiveEnterTimestamp anima
```

正常重启后必须确认 `ActiveEnterTimestamp` 已刷新，并确认服务仍为 `active`。不要通过反复重启掩盖启动错误。

## 6. 健康检查

本机 WebUI token 不能写入命令历史或报告。使用受控环境提供 token：

```bash
curl -H "Authorization: Bearer <webui-token>" \
  http://127.0.0.1:8088/api/health
curl -H "Authorization: Bearer <webui-token>" \
  http://127.0.0.1:8088/api/modules/status
```

重点核对：

- `/api/health` 返回服务运行状态、NapCat 实时 WS、出站回执、LLM 与心跳等状态；
- `running` 为真；
- `heartbeat_running` 为真；
- NapCat 登录/连接状态符合预期；
- 没有持续的启动异常、熔断或权限错误。

本地代码变更的最低验证：

```powershell
pytest tests/ -q
python -m anima doctor
```

以实际命令输出为准记录测试数量，不在文档中固化会过期的数字。

## 7. 日志与数据

服务日志位于 systemd journal：

```bash
journalctl -u anima -n 100 --no-pager
journalctl -u anima -f
journalctl -u anima --since "30 min ago" --no-pager
```

运行数据位于 `/opt/anima/data/`，可能包含聊天记忆、跨群记忆、审计、订阅、Skill 草案、SelfCode 备份和用量数据。它们属于生产用户数据：只在必要范围内读取，不复制到公开位置，不提交 Git，不在报告中泄露消息正文、凭据或 traceback。

## 8. 回滚

回滚前先保存证据：当前 `version.json`、服务状态、最近日志、健康检查结果和待回滚 commit。优先回到上一个已验证 commit，按同一发布流程执行；不要手工混合复制部分源码。

配置回滚必须单独处理：发布默认保留 `/opt/anima/config.yaml`，因此源码回滚不会自动回滚生产配置。若配置变更导致故障，使用已审计的配置变更回滚能力或恢复受控备份，随后重启并重新执行健康检查。

运行数据回滚是高风险动作。不得自动删除、覆盖或回退 `/opt/anima/data/`；只有明确的 owner/governance 授权、已确认备份来源和可验证恢复步骤同时具备时才执行。

## 9. 常见故障判断

### 服务无法启动

先查看 `systemctl status` 与 `journalctl`，确认是配置解析、依赖、权限、端口、NapCat 连接还是 LLM 凭据问题。配置错误应修配置并验证，不应借机改架构。

### 服务 active 但健康检查异常

区分本地 API、NapCat WS、LLM、心跳和出站回执。记录哪个子系统失败及持续时间；不要因为单个可选模块异常就改动核心流水线。

### 发布前后状态不一致

比较 commit、`version.json`、systemd 的 `ActiveEnterTimestamp`、日志和 `/api/health`。若无法证明运行中的版本身份，停止进一步发布，先恢复可识别状态。

### 需要“顺手优化”

这不是运维故障。记录观察结果，停止变更，并按 `RELEASE_POLICY.md` 判断是否存在真实 contradiction。

## 10. 禁止自动修复的情况

以下情况必须 STOP 并上报：

- 需要修改 Foundation contract、治理边界或冻结 schema；
- 需要新增 runtime layer、daemon、timer、polling 或自动 recovery；
- 需要新增 capability 或改变权限边界；
- 需要删除/覆盖生产数据、密钥或配置但没有明确授权；
- 只有“未来可能需要”“感觉更好”“顺手重构”等理由，没有 observed contradiction；
- 无法确认当前运行版本、数据备份或回滚路径。

报告使用固定顺序：事实 → 证据 → 判断 → 风险 → 下一步。

