#!/bin/sh
# Anima pre-commit 钩子（2026-11-27）
#
# 只做两件事，都是「事后发现代价高」的：
#   1. 快速测试子集 —— 防止把明确跑红的改动提交（全量留给 CI）
#   2. 文档健康检查 —— 归档/移动文档后链接断裂，只有提交时最容易发现
#
# 跳过：git commit --no-verify
set -e

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

echo "[pre-commit] 文档健康检查..."
if [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "[pre-commit] 未找到 python，跳过文档检查"
    PY=""
fi

if [ -n "$PY" ]; then
    $PY scripts/check_docs_health.py || {
        echo "[pre-commit] 文档检查失败（失效链接）。修复后重试，或 --no-verify 跳过。"
        exit 1
    }
fi

# 只在改动涉及 anima/ 时跑测试
if git diff --cached --name-only | grep -q '^anima/'; then
    echo "[pre-commit] 检测到 anima/ 改动，运行快速测试..."
    if [ -n "$PY" ]; then
        $PY -m pytest tests/ -q -x \
            --ignore=tests/test_browser_tool.py \
            -k "not slow and not integration" 2>/dev/null || {
            echo "[pre-commit] 测试未全绿。修复后重试，或 --no-verify 跳过。"
            exit 1
        }
    fi
fi

echo "[pre-commit] OK"
