# Anima Makefile —— 常用开发/运维任务
# 使用：`make test` / `make install` / `make run`

PYTHON ?= .venv/Scripts/python
PIP ?= .venv/Scripts/pip

.PHONY: help install test test-verbose run doctor clean

help:
	@echo "可用任务:"
	@echo "  make install   安装项目与可选依赖(qq,vector,dev)"
	@echo "  make test      运行 pytest 测试套件"
	@echo "  make run       启动 Anima (anima start)"
	@echo "  make doctor    环境自检 (anima doctor)"
	@echo "  make clean     清理缓存与字节码"

install:
	$(PIP) install -e ".[qq,vector,dev]"

test:
	$(PYTHON) -m pytest tests/ -q

test-verbose:
	$(PYTHON) -m pytest tests/ -v

run:
	$(PYTHON) -m anima.cli start

doctor:
	$(PYTHON) -m anima.cli doctor

clean:
	rm -rf .pytest_cache __pycache__ .venv/Lib/site-packages/anima.egg-link
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
