# 雨云签到 runner（独立进程）

**不属于 anima 主环境**。anima 通过 `subprocess` 调用它。

## 为什么独立

需要 `numpy 1.26`（2.x 要求 X86_V2 指令集，本机 CPU 只有 SSE2）
+ `opencv` + `onnxruntime` + `ddddocr` + `selenium` + Chrome。
装进 anima 主 venv 有冲突风险，体积约 1.2G。

## 部署

```bash
# 1. 建目录 + venv
mkdir -p /opt/anima/rainyun && cd /opt/anima/rainyun
python3 -m venv venv
./venv/bin/pip install --index-url https://pypi.org/simple/ \
    "numpy==1.26.4" opencv-python-headless onnxruntime \
    ddddocr selenium

# 2. 拷贝文件
#    sign_in.py / stealth.min.js 从仓库 anima/rainyun_runner/ 复制

# 3. 生成主密钥（一次性）
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#    → 写入 /opt/anima/.secrets:  RAINYUN_MASTER_KEY=<上面那串>
```

## 契约

```
stdin  : {"user": "...", "pass": "..."}      密码走管道，不走 argv
stdout : 最后一行 JSON {"ok": true, "points": 29100}
退出码 : 0 成功 / 1 失败
```

## 两个必须的 bug 修复

原项目（chizw/Rainyun-Qiandao）有两个 bug，不修则**每次都会登录失败**：

1. `is_displayed()` 判断不了从屏幕外移入的 iframe
   → TCaptcha 预加载时锚在 `y=-1000000`，弹出才移入视口
2. 登录环节用 `EC.visibility_of_element_located`，同样依赖 `is_displayed()`

两者都已在本文件里用 `getBoundingClientRect()` 修掉。
