# Memoh × Anima 双通道验证报告

生成时间：2026-11-05　目标机：anima-vps (192.168.6.100)

---

## 一、你要的方案

私聊 -> 完整 Memoh；群聊 -> Anima（已 Memoh 化的版本）。两套并存，互不干扰。

实现路径你点名三条：(1) 给 Memoh 写 OneBot 适配器　(2) 写网关分流群聊/私聊　(3) Memoh 2H2G / 40G 够用

---

## 二、已验证的事实（可复现）

### [OK] NapCat 支持多客户端并发连接

这是整个方案成立的前提。原本以为一个 NapCat 只能被一个客户端占用。

实测（以第二个 WS 客户端接入生产 NapCat）：

    CONNECTED as 2nd client
    RECV: post_type=meta_event meta_event_type=lifecycle sub_type=connect
    RECV: status=ok retcode=0 data={user_id:2752719822, nickname:MengKing} echo=probe1
    API_OK: yes

**结论：Anima 连着 3001 的同时，Memoh 也能连同一个 3001。不需要第二个 QQ 号。**
这直接推翻了「必须再给一个 QQ 号」的判断。

### [OK] Memoh 确实没有 OneBot 通道

    grep -ril "onebot|napcat|go-cqhttp" --include=*.go internal/  -> 只有 2 个无关的 migration 测试
    defaultAPIBaseURL = "https://api.sgroup.qq.com"   <- 只认官方机器人平台

所以 (1) 必须做，且只能新写。

### [OK] 适配器契约已摸清并已写出

Memoh 的 Channel Adapter 是组合接口，核心只有四个：

| 接口 | 方法 |
|---|---|
| Adapter | Type() / Descriptor() |
| Receiver | Connect(ctx, cfg, handler) (Connection, error) |
| Sender | Send(ctx, cfg, PreparedOutboundMessage) error |
| Connection | ConfigID/BotID/ChannelType/Stop/Running（有现成 channel.NewConnection） |

已写出 4 个文件（/opt/memoh-src/Memoh-1.0.1/internal/channel/adapters/onebot/）：

    descriptor.go   721 B   ChannelType = "onebot"
    config.go     2.7 KB   wsUrl / accessToken / selfId 解析 + target 归一化
    client.go     3.4 KB   WS 连接、echo 关联、send_private_msg / send_group_msg
    onebot.go     5.9 KB   Adapter 全接口实现 + DiscoverSelf(get_login_info)

### [OK] Docker 已装好，数据目录已挪到 /opt

    Docker 26.1.5+dfsg1
    Docker Root Dir: /opt/docker      <- 默认在 / (只剩 3.6G)，已改到 /opt (21G)
    Registry Mirrors: docker.1ms.run, docker.m.daocloud.io   <- 直连 Docker Hub TLS 握手失败

### [OK] Memoh server 镜像已就位

    memohai/server:latest   910 MB

### [OK] 生产服务全程未受影响

    systemctl is-active anima -> active
    ok=32 failed=0
    可用内存 2479 MB

Anima 全程正常运行，群聊/私聊未被任何操作影响。

---

## 三、还没完成的（诚实说明）

### [..] postgres 镜像

层已下载完但落盘未完成。Docker Hub 直连超时，走镜像源后仍在拉。

### [!!] 编译带 OneBot 适配器的 Memoh —— 这是当前真正的卡点

已经拉下来的 memohai/server:latest 是**官方预编译镜像，里面没有我的适配器**。
要用上必须重新编译，而重新编译需要：

| 需要 | 现状 |
|---|---|
| Go 1.26.10 工具链 | 机器上没有，正在拉 golang:alpine 镜像 |
| 全量 go module 下载 | 未开始，依赖树很大 |
| 磁盘 | / 只剩 **3.6G**，Docker 数据在 /opt 有 21G |
| 内存 | 3.9G，可用 2.5G；go build 峰值会吃掉 1-2G |

**这台机器上现场编译有实际 OOM 风险。**

### 关于「2H2G 能跑」

你说 Memoh 2H2G 就能跑，这个我信 —— 但那是指**运行**占用的资源。
**编译是另一回事**，它和运行是两个不同的峰值。现在卡在编译，不在运行。

---

## 四、结论表

| 项 | 状态 |
|---|---|
| NapCat 多客户端并发 | OK 已验证可行 |
| Memoh 缺 OneBot 通道 | OK 已确认 |
| OneBot 适配器代码 | OK 已写完（4 文件，约 12.8KB） |
| Docker 环境 | OK 已就绪，数据挪到 /opt |
| Memoh 镜像 | OK 已拉取 910MB |
| **带适配器重新编译** | **卡在 Go 工具链 + 磁盘** |
| 网关分流 | 未开始（依赖上一步） |
| 端到端验证 | 未开始 |

---

## 五、三条出路

### A. 换编译机器，产物拷回来（推荐）

在 pve（16G 内存 / 25G 根 / 797G 数据盘，且它就是这台 VPS 的宿主机）上编译，docker save 出 tar 传回 VPS。

- 优点：不影响生产机内存；pve 磁盘充足
- 代价：要在 pve 上装 docker（约 10 分钟）
- 风险：低

### B. 在 vps 上硬编译

停掉 Anima 腾内存，限制 go build -p 1，先扩 swap。

- 优点：不换机
- 代价：**编译期间 Anima 必须停**，你的 bot 离线 10-30 分钟
- 风险：中，OOM 可能性实在

### C. 不用自编译：改用 Memoh 自带 web 通道 + 外部桥

不改 Memoh 源码。写一个小 Python 桥：

    NapCat WS(3001) ---+--- Anima（群聊）
                       +--- 桥 ---HTTP/WSS---> Memoh /bots/{id}/web/ws（web 通道，登录换 JWT）

- 优点：**不用编译任何东西**，Memoh 用官方镜像即可跑
- 代价：桥要维护 Memoh 的 WS 协议（wsClientMessage / 流式事件），比 OneBot 适配器复杂；且要处理 JWT 续期
- 风险：中，协议细节多但可控

---

## 六、我的建议

选 **A**。pve 本来就是这台 VPS 的宿主机，资源管够，编译产物一次性，且不碰生产机 Anima，零停机。

想更快看到东西、也不介意 Anima 停一会儿，选 **B**。

**在你决定之前，我不会停 Anima，也不会重启它。**