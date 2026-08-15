"""R6.2-04 — 运行时漂移检测（生产一致性检查）。

比对运行 commit SHA 与批准的 SHA，并可选择性比对运行 config 哈希与批准的
config 哈希。输出 PASS 或 DRIFT DETECTED。fail-closed：任何不匹配 / 缺失批准
参考 → DRIFT。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class DriftResult:
    status: str                       # "PASS" | "DRIFT DETECTED"
    checks: dict                     # {name: (ok: bool, detail: str)}

    @property
    def ok(self) -> bool:
        return self.status == "PASS"

    def __repr__(self) -> str:
        return f"DriftResult({self.status}, {len(self.checks)} checks)"


def attest(
    running_sha: Optional[str],
    approved_sha: Optional[str],
    running_config_hash: Optional[str] = None,
    approved_config_hash: Optional[str] = None,
) -> DriftResult:
    """比对 running 与 approved。fail-closed：缺 approved 参考或任一不匹配 → DRIFT。"""
    checks: dict = {}

    # commit SHA 比对
    if not approved_sha:
        checks["commit_sha"] = (False, "approved_sha not provided")
    elif not running_sha:
        checks["commit_sha"] = (False, "running_sha not available")
    else:
        checks["commit_sha"] = (
            running_sha == approved_sha,
            f"running={running_sha} approved={approved_sha}",
        )

    # config 哈希比对（可选；提供则必须一致）
    if running_config_hash is not None or approved_config_hash is not None:
        if not approved_config_hash:
            checks["config_hash"] = (False, "approved_config_hash not provided")
        elif not running_config_hash:
            checks["config_hash"] = (False, "running_config_hash not available")
        else:
            checks["config_hash"] = (
                running_config_hash == approved_config_hash,
                f"running={running_config_hash} approved={approved_config_hash}",
            )

    ok = all(ok_ for ok_, _ in checks.values())
    status = "PASS" if ok else "DRIFT DETECTED"
    return DriftResult(status, checks)


def config_hash(config_text: str) -> str:
    """对配置文本生成稳定哈希（去空白、行排序），用于漂移比对。"""
    norm = "\n".join(
        sorted(line.strip() for line in config_text.splitlines() if line.strip())
    )
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def read_running_sha(version_path: str) -> Optional[str]:
    """从 version.json 读取运行 commit SHA（生产态）。"""
    try:
        with open(version_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("commit")
    except (OSError, ValueError):
        return None


def attest_runtime(
    approved_sha: Optional[str],
    version_path: str,
    config_text: Optional[str] = None,
    approved_config_hash: Optional[str] = None,
) -> DriftResult:
    """生产便捷封装：读运行 SHA（version.json）+ 可选 config 哈希，执行 attest。"""
    running_sha = read_running_sha(version_path)
    running_config_hash = config_hash(config_text) if config_text is not None else None
    return attest(
        running_sha,
        approved_sha,
        running_config_hash=running_config_hash,
        approved_config_hash=approved_config_hash,
    )
