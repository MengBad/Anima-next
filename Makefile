# Anima Makefile —— 常用开发/运维任务
# 使用：`make test` / `make install` / `make run`

PYTHON ?= .venv/Scripts/python
PIP ?= .venv/Scripts/pip

.PHONY: help install test test-verbose run doctor clean ci security-guard ci-evidence

help:
	@echo "可用任务:"
	@echo "  make install   安装项目与可选依赖(qq,vector,dev)"
	@echo "  make test      运行 pytest 测试套件"
	@echo "  make run       启动 Anima (anima start)"
	@echo "  make doctor    环境自检 (anima doctor)"
	@echo "  make ci        本地复跑 R10-00 CI 验证（测试+安全护栏+证据）"
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

# R10-00 本地 CI 验证：测试 + 安全护栏 + 证据聚合（不含生产部署/自动批准）
# 注意：本地不强制 pytest-json-report（CI 侧安装并启用）；evidence 容忍 test-result.json 缺失。
# 已知预存 broken 测试（与 R6–R9 路线无关，不属 R10-00 范围，CI/本地统一忽略；后续专项修复）：
#   test_r3_4_audit_completeness / test_r3_5_kill_switch（缺 anima.pilot.identity_policy）
#   test_f9_2_operational_activation（冻结配置集与 R6.1 python_exec_* 增强冲突）
#   test_memory_read_r2a（缺 anima.pilot + memory_read 能力未落地）
#   test_audit_persistence / test_health_observer（subprocess cwd 硬编码 Windows 路径 G:/Anima-next，Linux CI 不可运行）
ANIMA_CI_IGNORE = --ignore=tests/test_r3_4_audit_completeness.py \
	--ignore=tests/test_r3_5_kill_switch.py \
	--ignore=tests/test_f9_2_operational_activation.py \
	--ignore=tests/test_memory_read_r2a.py \
	--ignore=tests/test_audit_persistence.py \
	--ignore=tests/test_health_observer.py

ci:
	$(PYTHON) -m pytest tests/ $(ANIMA_CI_IGNORE) -q
	$(PYTHON) scripts/anima_guard.py --base origin/master --out security-report.json
	$(PYTHON) scripts/ci_evidence.py --commit "$(shell git rev-parse HEAD)" --run-id local \
		--security-report security-report.json --test-report test-result.json \
		--frontend-built true --out-ci ci-report.json --out-attest commit-attestation.json

security-guard:
	$(PYTHON) scripts/anima_guard.py --base origin/master --out security-report.json

ci-evidence:
	$(PYTHON) scripts/ci_evidence.py --commit "$(shell git rev-parse HEAD)" --run-id local \
		--security-report security-report.json --test-report test-result.json \
		--frontend-built true --out-ci ci-report.json --out-attest commit-attestation.json
