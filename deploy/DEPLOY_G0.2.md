# G0.2 生产部署 Runbook —— 上下文隔离修复（止血线上串台）

> 目标：将已验证的 `(群,用户)` 级隔离修复部署到生产 `/opt/anima`，
> 重启后旧群级共享桶被丢弃、私聊历史保留、身份污染条目清洗，从根本上消除跨用户串台与人格污染。

## 前置条件

- 你能 SSH 到生产机（VPS），拥有 `root`（或能 sudo 的用户）。
- 生产机已装有 NapCat（正向 WS）且与 bot 连通（部署脚本不负责 NapCat）。
- 本机已 `cd` 到 `G:/Anima-next`（即 `Anima-next` 项目根）。

## 第一步：本地预检（沙箱内已完成，可复核）

隔离修复代码已通过：
- ✅ 8/8 核心模块在生产 `.venv` 下导入通过
- ✅ 全代码库 `memory.add` / `get_context` 调用点已改用新签名（带 `user_id`），无残留旧 5 参数调用
- ✅ 本地迁移模拟（用真实 `data/chat_memory.json` 副本）：旧群桶丢弃、私聊桶保留、加载 0.3ms 无崩溃、污染条目写盘前被剥离

## 第二步：设置部署环境变量

```bash
export ANIMA_DEPLOY_HOST=pve.aicaowo.com   # 生产机地址
export ANIMA_DEPLOY_USER=root
export ANIMA_DEPLOY_PASS='<你的密码>'
```

## 第三步：dry-run 预检（不改动远程，强烈建议先跑）

```bash
python _deploy.py --dry-run
```

会输出：远程 Python 版本、venv 是否存在、**待上传文件数与大小**、是否覆盖 config.yaml。
确认无误（尤其“待上传文件”数量合理、config.yaml 默认跳过）再正式部署。

## 第四步：正式部署

```bash
# 默认不会上传 config.yaml（保护生产配置）
python _deploy.py

# 仅当你确实要用本地 config.yaml 覆盖生产时才加：
# python _deploy.py --with-config
```

脚本会自动：
1. 创建 `anima` 用户与目录、安装系统依赖、建 venv；
2. 上传项目文件（排除 `.venv`/`data`/`config.yaml` 等）；
3. `pip install -e '.[qq,vector,dev]'`；
4. **备份远程旧记忆**：`/opt/anima/data/chat_memory.json.pre-isolation-<时间戳>`；
5. 渲染并安装 systemd 单元 `anima.service`；
6. **重启 anima 服务**（新代码首次加载时自动完成迁移）；
7. 运行 `anima doctor` 自检。

## 第五步：验证迁移生效

```bash
# 1) 服务状态
ssh root@$ANIMA_DEPLOY_HOST 'systemctl is-active anima'

# 2) 记忆文件已迁移为 (群,用户) 级
ssh root@$ANIMA_DEPLOY_HOST 'cat /opt/anima/data/chat_memory.json' \
  | python -c "import sys,json; d=json.load(sys.stdin); print('keys:', list(d.keys()))"
# 期望：只剩 private_xxx 与 group_{gid}_{uid} 形式，无裸 group_{gid}

# 3) 看一眼日志确认无串台相关报错
ssh root@$ANIMA_DEPLOY_HOST 'journalctl -u anima --since "5 min ago" -n 50'
```

## 回滚（必要时）

若迁移后行为异常，旧数据已备份：

```bash
ssh root@$ANIMA_DEPLOY_HOST \
  'cp /opt/anima/data/chat_memory.json.pre-isolation-* /opt/anima/data/chat_memory.json && systemctl restart anima'
```

> 注意：回滚会回到旧群级桶（即存在串台风险的状态），仅作紧急止血用。

## 风险与说明

- **历史群聊记忆会被清空**：旧 `group_{gid}` 桶因无法归属到具体用户（数据中 `name` 字段全空），升级时整桶丢弃。这是“消除串台”的必然代价，比保留污染数据更安全。私聊 `private_*` 桶保留。
- **污染条目永不落盘**：`save()` 写盘前剥离身份混淆条目（如“我是 Antigravity”），结合 `_restore()` 加载时清洗，双重保险。
- 部署前请确认生产 `config.yaml` 是正确版本（默认不上传，由你手动管理）。
