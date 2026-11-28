#!/bin/sh
# 安装 pre-commit 钩子（2026-11-27）
#
# `core.hooksPath` 是**本地** git 配置，不随仓库分发。
# 克隆后跑一次本脚本即可启用；CI 不依赖它（CI 自己跑完整检查）。
set -e
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
chmod +x scripts/pre-commit.sh
git config core.hooksPath scripts
echo "[install-hooks] core.hooksPath = scripts"
echo "[install-hooks] 已启用：提交时会跑文档健康检查 + anima/ 改动时的快速测试"
echo "[install-hooks] 跳过：git commit --no-verify"
