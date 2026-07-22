# Anima 部署指南

Anima 是**独立进程**（不依赖 AstrBot）。两种部署形态：

## 一、裸进程 / systemd（同机 NapCat）

```bash
# 1. 安装依赖
pip install -e '.[qq]'

# 2. 生成配置（交互 / agent 两种）
anima init                       # 交互：生成 config.yaml + 运行时目录 + systemd 单元
anima init --agent --spec spec.json --path config.yaml   # 非交互：agent 自动部署

# 3. 安装 NapCat（同机）
npm i -g napcat-cli
napcat start --ws 3001 --token <your-token>
# 浏览器打开 NapCat WebUI 登录机器人 QQ 账号

# 4. 启动
anima start --config config.yaml
# 或注册为系统服务（init 已生成 anima.service）
cp anima.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now anima

# 5. 自检
anima doctor --config config.yaml
```

## 二、Docker Compose（推荐 agent / 隔离部署）

```bash
cp .env.example .env     # 填入 ANIMA_LLM_API_KEY / NAPCAT_TOKEN
docker compose up -d
# 打开 http://localhost:3001 用 NapCat WebUI 登录 QQ
```

> config.yaml 中 `qq.napcat_ws_url` 应指向 `ws://napcat:3001`（compose 网络内服务名）。

## 三、agent 非交互部署契约

`anima init --agent --spec <file> --path <out>` 的行为：
1. 以 `config/default.yaml` 为默认模板；
2. 将 `--spec`（JSON/YAML，仅含要覆盖的字段，支持嵌套深合并）叠加到默认值；
3. 经 `AnimaConfig`(pydantic) 校验并补全默认值，写出 `config.yaml`；
4. 创建 `data/ data/output/ plugins/` 运行时目录；
5. 输出**机器可读 JSON**：
   ```json
   {"ok": true, "config_path": "...", "checks": [...], "next": ["...", "..."]}
   ```
   退出码 0=自检通过，1=存在阻断项。

最小 spec 示例（让机器人真正可用）：
```json
{
  "llm": {"api_key": "sk-xxx", "endpoint": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
  "permissions": {"owners": [10001]}
}
```

## 四、迁移（旧群级 chat_memory → 新 (群,用户) 级）

新版 `ChatMemory` 在 `_restore()` 加载时自动：
- 丢弃旧的 `group_{gid}` 整桶（群级共享大桶，存在串台风险）；
- 清洗含 "antigravity" 等身份污染的 bot 条目；
- 之后所有写入走 `group_{gid}_{uid}` 级隔离键。

首次启动即完成迁移，无需手工操作。
