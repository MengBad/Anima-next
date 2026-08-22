"""R17 -- GitHub 真实信号源（Real-Environment Observation 的「真实世界」一翼）。

collect_from_github（environment.py）已定义「可注入 GitHub 源」接口：实现
recent_activity(limit) -> list[dict]，每条含 kind（commit/ci/issue）、
status、ts、detail。本模块提供生产可用的注入实现，复用已认证的
gh CLI（token 存于系统 keyring，本进程不接触任何明文凭据），把仓库的
commits / CI runs / issues 归一化为真实环境观测事件。

设计纪律（延续 R6-R15 / R16）：
- 只观测、绝不执行：零写入、零权限提升、零自动部署。
- fail-closed / 诚实：gh 不可用 / 网络失败 / 解析错误 -> 返回空列表，
  绝不以「环境正常」掩盖信号缺失。
- 资源克制：每次聚合只取最近 N 条（默认 20），不翻页刷 GitHub 配额。
"""
from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any, Optional

# 生产仓库（deployment 来源）。prod 为源码归档、不携带 .git，无法从 remote
# 反推；此处以项目规范仓库为准。若未来多仓，可在构造时显式传入 repo。
_DEFAULT_REPO = "MengBad/Anima-next"
_GH_BIN = "gh"
_MAX_PER_KIND = 20


def _resolve_repo(explicit: Optional[str] = None) -> str:
    """优先用显式仓库；否则尝试从当前 git remote 反推 owner/repo；失败回退默认。"""
    if explicit:
        return explicit
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            root = out.stdout.strip()
            url = subprocess.run(
                ["git", "-C", root, "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
            )
            if url.returncode == 0:
                u = url.stdout.strip()
                if "github.com" in u:
                    tail = u.split("github.com")[-1].lstrip(":/").rstrip("/.git")
                    if tail.count("/") == 1:
                        return tail
    except Exception:  # noqa: BLE001 - 反推失败不阻断观测
        pass
    return _DEFAULT_REPO


def _iso_to_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _gh_api(repo: str, endpoint: str) -> Optional[list[dict[str, Any]]]:
    """调用 gh api 取 JSON 数组；任何失败返回 None（调用方据此 fail-closed）。"""
    try:
        out = subprocess.run(
            [_GH_BIN, "api", f"repos/{repo}/{endpoint}",
             "--paginate", "--jq", "."],
            capture_output=True, text=True, encoding="utf-8", timeout=25,
        )
    except Exception:  # noqa: BLE001 - CLI 不可用
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


class GitHubSignalSource:
    """基于已认证 gh CLI 的真实活动源；实现 recent_activity(limit) 接口。"""

    def __init__(self, repo: Optional[str] = None):
        self.repo = _resolve_repo(repo)

    def available(self) -> bool:
        try:
            out = subprocess.run(
                [_GH_BIN, "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            return out.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def recent_activity(self, limit: int = _MAX_PER_KIND) -> list[dict[str, Any]]:
        if not self.available():
            return []
        n = max(1, min(int(limit), 50))
        rows: list[dict[str, Any]] = []

        commits = _gh_api(self.repo, f"commits?per_page={n}")
        if commits:
            for c in commits[:n]:
                sha = str(c.get("sha", ""))[:10]
                msg = str((c.get("commit") or {}).get("message", "")).splitlines()
                msg = msg[0] if msg else ""
                rows.append({
                    "kind": "commit", "status": "ok",
                    "ts": _iso_to_ts(((c.get("commit") or {}).get("author") or {}).get("date")),
                    "detail": f"{sha} {msg}"[:80],
                })

        runs = _gh_api(self.repo, f"actions/runs?per_page={n}")
        if runs:
            for r in runs[:n]:
                concl = str(r.get("conclusion") or "").lower()
                status = "failed" if concl == "failure" else (
                    "error" if concl in ("timed_out", "cancelled") else "ok")
                rows.append({
                    "kind": "ci", "status": status,
                    "ts": _iso_to_ts(r.get("created_at")),
                    "detail": f"{r.get('name', 'CI')} ({r.get('head_branch', '')})"[:60],
                })

        issues = _gh_api(self.repo, "issues?state=open&per_page=%d&filter=all" % n)
        if issues:
            for it in issues[:n]:
                if "pull_request" in it:
                    continue
                rows.append({
                    "kind": "issue", "status": str(it.get("state", "opened")),
                    "ts": _iso_to_ts(it.get("created_at")),
                    "detail": str(it.get("title", ""))[:60],
                })
        return rows

