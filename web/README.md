# Anima Console

自托管的 Anima QQ Bot 管理面板。单文件 FastAPI 后端 + 单文件前端（零构建），
直接读取 bot 的 `config.yaml` 与 `data/` 真实数据，无需额外数据库。

## 功能

- **仪表盘**：服务 / QQ 在线态、运行时长、主动发言概率；自主行为配置（@/私聊闸门、主动概率、沉默门控、全局冷却）实时读写；最近活动、近期日志、一键重启。
- **人格设定**：在线编辑 `persona_core.yaml`（system_prompt），保存即生效；查看关联的认知模块（自我笔记 / 世界观 / 欲望 / 关系）。
- **记忆库**：浏览对话、向量、自我笔记、世界观、欲望、关系、时间感等长期记忆。
- **插件商店**：查看 AstrBot 兼容插件目录与兼容层启用状态。
- **运行日志**：journalctl 实时日志，支持关键字过滤与自动刷新。
- **外观设置**：明暗主题、强调色换肤（任意实例可一键换肤）、实例名称。

## 架构

```
web/
├── server.py            # FastAPI：同源托管 console.html + /api 端点
├── console.html         # 单文件前端（玻璃拟态、可主题化）
├── requirements.txt
├── .env.example
├── anima-console.service  # systemd 单元（部署用）
└── README.md
```

后端直接读取真实文件，不经过重型业务模块：

| 端点 | 数据来源 |
|------|----------|
| `/api/status` | `systemctl is-active` + NapCat WS `get_login_info` |
| `/api/config` | `config.yaml` (autonomy 段) |
| `/api/persona` | `config/persona_core.yaml` |
| `/api/memory` | `data/*.json` / `data/anima.db` (memory_meta) |
| `/api/logs` | `journalctl -u anima` |
| `/api/restart` | `systemctl restart anima` |
| `/api/plugins` | `plugins/` 目录 + `compat.astrbot_enabled` |

## 本地开发

```bash
cd /opt/anima   # 或你的项目根
python -m venv venv && source venv/bin/activate
pip install -r web/requirements.txt

# 设置令牌（本地开发可留空，但生产必须设置）
export ANIMA_CONSOLE_TOKEN=dev-token
# 可选：指向你的 config / data
export ANIMA_CONFIG=config.yaml

python -m web.server
# 打开 http://localhost:8080
```

> 本地无 `systemctl` / `journalctl` 时，状态与日志接口会优雅降级（返回提示），
> 记忆 / 人格 / 配置接口仍可正常读取与写入。

## 生产部署（VPS / Linux）

```bash
# 1. 安装依赖
source venv/bin/activate
pip install -r web/requirements.txt

# 2. 放置 systemd 单元（先填入强随机令牌）
sed -i 's/__SET_STRONG_TOKEN_HERE__/<你的强随机令牌>/' web/anima-console.service
cp web/anima-console.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now anima-console

# 3. 验证
curl -H "Authorization: Bearer <令牌>" http://localhost:8080/api/health
```

### 安全提示

- 面板仅以 Bearer 令牌鉴权，**未做账号体系**，应仅在可信网络/反代后暴露。
- 建议用 Nginx/Caddy 反代并加 HTTPS，限制来源 IP。
- `anima-console.service` 以 root 运行，以便读取 journal 与重启 bot；若收紧权限，
  可将日志读取改为 bot 进程写入的日志文件，并将 restart 权限通过 `sudoers` 限定。
- `config.yaml` 含 LLM / 向量库密钥，切勿提交到公开仓库（根 `.gitignore` 已忽略 `reports/` 等）。

## 已知限制

- `PUT /api/config` 修改需**重启 bot** 才生效（bot 启动时加载配置）。
- 插件「启用」需在 VPS 上传插件目录并将 `compat.astrbot_enabled` 置 true，
  面板当前仅展示状态，不直接安装（兼容层为 Tier 1 部分垫片，建议灰度验证）。
- 配置写回使用 `yaml.safe_dump`，会重排 `config.yaml` 格式（含注释会丢失）。
