"""受控仓库只读访问（P3-2）。

**为什么要它**：在此之前 bot 只能读 ``data_dir``（``tools/builtin/file.py`` 的
``read_file``），**读不到自己的源码**。于是任何「自我改进提案」都是无依据的猜——
既不知道真实函数签名，也不知道某处是否已有护栏，只能凭语言模型记忆编。P3-2 让
提案可以引用真实代码，同时把「读代码」这件事做成**受控、可审计、有边界**的能力。

设计纪律（与 R9 / Fail Closed / Production Is Sacred 一致）：

1. **纯只读**：只用 ``open(..., "r")`` / ``Path.iterdir()``，本模块不含任何写路径，
   不 import 任何写接口。没有 ``write`` / ``unlink`` / ``rename``。
2. **根目录约束**：所有路径先 ``resolve()``，必须落在仓库根内——覆盖 ``..``、
   绝对路径混入、软链跳出等（软链用 ``resolve()`` 后的真实路径判定）。
3. **拒绝名单**：密钥/凭据/生产数据/依赖与缓存一律不可读。**这是硬约束**，
   不是「默认值」——``config.yaml``（含 WebUI token）、``.secrets``、``.env*``、
   ``*.pem/key``、``data/**``、``.git/**``、``.workbuddy/**`` 全部在内。
   生产 ``data/`` 只能经既有 ``read_file``（其自身有 data_dir 边界）访问，
   本模块不得成为绕过它的通道。
4. **有界**：单文件字节/行数上限、单次列表条数上限、grep 命中上限——防上下文爆炸。
5. **可审计**：每次读/列/搜都发 ``repo_read`` 审计事件（含相对路径与字节数）。
6. **不猜**：拒绝 → 明确说「被拒绝名单拦下」；不存在 → 明确说「不存在」；
   二进制 → 明确说「二进制，拒读」。绝不返回空串让上层以为是「没有内容」。
"""
from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("anima.agent.repo_read")

# 默认拒绝名单（按仓库相对路径做 fnmatch；同时按「任一路径段」做精确名匹配）。
# 顺序无关，命中最先到的一个即拒。
DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    # ---- 凭据 / 密钥 ----
    ".env", ".env.*", "*.env", ".secrets", ".secrets.*", "*.secret", "*.secrets",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.ppk",
    "credentials*", "*credential*.json", "*token*.json",
    # ---- 生产配置 / 运行时数据 ----
    "config.yaml", "config.local.yaml", "config.prod.yaml",
    "data/**", "sylann/**", "logs/**", "*.log",
    # ---- Agent 自身元数据（含本地会话与记忆）----
    ".workbuddy/**", ".claude/**", ".vscode/**", ".idea/**",
    # ---- VCS / 依赖 / 构建产物 / 缓存 ----
    ".git/**", ".git", ".github/**", "node_modules/**", "**/node_modules/**",
    ".venv/**", "venv/**", "env/**", "site-packages/**",
    "__pycache__/**", "**/__pycache__/**", "*.pyc", "*.pyo", "*.so", "*.dll", "*.exe",
    "*.egg-info/**", "dist/**", "build/**", ".pytest_cache/**", ".mypy_cache/**",
    ".ruff_cache/**", "htmlcov/**", ".coverage*", "*.zip", "*.tar*", "*.gz", "*.db",
    "*.sqlite", "*.sqlite3",
)

# 即使名字不在拒绝名单里，这些**路径段**出现即拒（防「换个目录名再放一份密钥」）。
DEFAULT_DENY_SEGMENTS: frozenset[str] = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "data", ".workbuddy",
    ".secrets", "secrets", ".ssh",
})

_BINARY_SNIFF_BYTES = 4096
_MAX_GREP_FILE_BYTES = 2_000_000


class RepoReadError(RuntimeError):
    """受控读失败的基类。"""


class RepoPathDenied(RepoReadError):
    """路径被边界/拒绝名单拦下（不是「文件不存在」）。"""


class RepoReader:
    """仓库只读访问器（根目录约束 + 拒绝名单 + 上限 + 审计）。"""

    def __init__(self, root: str | Path | None = None, *,
                 deny_patterns: tuple[str, ...] | None = None,
                 deny_segments: frozenset[str] | None = None,
                 max_bytes: int = 20_000,
                 max_lines: int = 400,
                 max_files: int = 40,
                 max_hits: int = 40,
                 audit: Any = None) -> None:
        self.root = Path(root).resolve() if root is not None else self._default_root()
        self.deny_patterns = tuple(deny_patterns) if deny_patterns is not None else DEFAULT_DENY_PATTERNS
        self.deny_segments = deny_segments if deny_segments is not None else DEFAULT_DENY_SEGMENTS
        self.max_bytes = max(200, int(max_bytes))
        self.max_lines = max(1, int(max_lines))
        self.max_files = max(1, int(max_files))
        self.max_hits = max(1, int(max_hits))
        self._audit = audit

    # ---------------- 边界 ----------------
    @staticmethod
    def _default_root() -> Path:
        """默认仓库根 = 本文件上溯到仓库根（anima/agent/repo_read.py → 上两级）。"""
        return Path(__file__).resolve().parents[2]

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return ""

    def _denied_reason(self, rel: str) -> str:
        if not rel:
            return "路径在仓库根之外"
        for seg in rel.split("/"):
            if seg in self.deny_segments:
                return f"路径段「{seg}」在拒绝名单内"
        for pat in self.deny_patterns:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel.split("/")[-1], pat):
                return f"命中拒绝名单「{pat}」"
            if pat.endswith("/**") and rel.startswith(pat[:-3] + "/"):
                return f"命中拒绝名单「{pat}」"
        return ""

    def resolve(self, rel: str = "") -> Path:
        """把仓库相对路径解析成绝对路径，越界/被拒即抛 ``RepoPathDenied``。"""
        raw = str(rel or "").strip()
        if raw.startswith("~"):
            raise RepoPathDenied("不接受 ~ 展开的路径")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", raw):
            # 绝对路径一律拒绝，而不是"悄悄当成相对路径"——后者会把
            # `/etc/passwd` 静默改写成 `<repo>/etc/passwd`，既掩盖调用方意图，
            # 也可能意外命中仓库内的同名文件。
            raise RepoPathDenied(f"不接受绝对路径（本能力只在仓库内相对寻址）：{rel}")
        candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise RepoPathDenied(f"路径越出仓库根：{rel}") from None
        rel_posix = self._rel(candidate)
        reason = self._denied_reason(rel_posix)
        if reason:
            raise RepoPathDenied(f"{reason}：{rel_posix}")
        return candidate

    def _emit(self, action: str, rel: str, *, detail: str = "", size: int = 0) -> None:
        if self._audit is None:
            return
        try:
            self._audit.emit("repo_read", action=action, path=rel,
                             detail=detail[:200], bytes=int(size))
        except Exception:  # noqa: BLE001 - 审计失败绝不阻断只读能力
            logger.debug("repo_read 审计事件发送失败", exc_info=True)

    # ---------------- 列目录 ----------------
    def list_dir(self, rel: str = "", *, depth: int = 1) -> dict[str, Any]:
        """列出目录（有界、只读）。被拒/不存在都如实报告。"""
        depth = max(1, min(int(depth or 1), 3))
        try:
            base = self.resolve(rel)
        except RepoPathDenied as exc:
            return {"ok": False, "path": rel, "reason": str(exc), "entries": []}
        if not base.exists():
            return {"ok": False, "path": rel, "reason": "路径不存在", "entries": []}
        if base.is_file():
            return {"ok": True, "path": self._rel(base), "entries": [self._rel(base)],
                    "truncated": False}
        entries: list[str] = []
        truncated = False
        stack = [(base, 0)]
        while stack:
            cur, d = stack.pop(0)
            try:
                children = sorted(cur.iterdir(), key=lambda p: (p.is_file(), p.name))
            except OSError:
                continue
            for ch in children:
                relch = self._rel(ch)
                if self._denied_reason(relch):
                    continue
                entries.append(relch + ("/" if ch.is_dir() else ""))
                if len(entries) >= self.max_files:
                    truncated = True
                    stack.clear()
                    break
                if ch.is_dir() and d + 1 < depth:
                    stack.append((ch, d + 1))
        self._emit("list", rel, detail=f"{len(entries)} entries")
        return {"ok": True, "path": rel or ".", "entries": entries, "truncated": truncated,
                "root": str(self.root)}

    # ---------------- 读文件 ----------------
    def read_text(self, rel: str, *, start_line: int = 1,
                  end_line: int | None = None) -> dict[str, Any]:
        """读仓库内文本文件（有界、只读）。"""
        try:
            path = self.resolve(rel)
        except RepoPathDenied as exc:
            return {"ok": False, "path": rel, "reason": str(exc), "content": ""}
        if not path.is_file():
            return {"ok": False, "path": rel, "reason": "文件不存在或不是普通文件", "content": ""}
        size = path.stat().st_size
        try:
            head = path.open("rb").read(_BINARY_SNIFF_BYTES)
        except OSError as exc:
            return {"ok": False, "path": rel, "reason": f"读取失败：{type(exc).__name__}", "content": ""}
        if b"\x00" in head:
            return {"ok": False, "path": rel, "reason": "二进制文件，拒读（只支持文本）", "content": ""}

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        lo = max(1, int(start_line or 1))
        hi = total if end_line in (None, 0) else min(total, int(end_line))
        if hi < lo:
            hi = lo
        window = lines[lo - 1:hi]
        notes: list[str] = []
        window_truncated = False
        if len(window) > self.max_lines:
            window = window[:self.max_lines]
            window_truncated = True
            notes.append(f"行数超过上限，仅返回前 {self.max_lines} 行")
        text = "\n".join(window)
        byte_truncated = False
        if len(text.encode("utf-8")) > self.max_bytes:
            text = text.encode("utf-8")[:self.max_bytes].decode("utf-8", errors="ignore")
            byte_truncated = True
            notes.append(f"字节数超过上限（{self.max_bytes}），已截断")
        self._emit("read", self._rel(path), detail=f"lines {lo}-{hi}/{total}", size=size)
        return {
            "ok": True,
            "path": self._rel(path),
            "total_lines": total,
            "size_bytes": size,
            "range": [lo, hi],
            "truncated": bool(window_truncated or byte_truncated),
            "notes": notes,
            "content": text,
        }

    # ---------------- grep ----------------
    def grep(self, pattern: str, *, glob: str = "**/*.py",
             max_hits: int | None = None) -> dict[str, Any]:
        """在仓库文本文件里做正则搜索（有界、只读）。"""
        pat = str(pattern or "")
        if not pat:
            return {"ok": False, "reason": "空 pattern", "hits": []}
        try:
            rx = re.compile(pat)
        except re.error as exc:
            return {"ok": False, "reason": f"正则非法：{exc}", "hits": []}
        cap = max(1, int(max_hits or self.max_hits))
        hits: list[dict[str, Any]] = []
        scanned = 0
        skipped = 0
        truncated = False
        for path in sorted(self.root.glob(glob)):
            if len(hits) >= cap:
                truncated = True
                break
            if not path.is_file():
                continue
            rel = self._rel(path)
            if self._denied_reason(rel):
                skipped += 1
                continue
            try:
                if path.stat().st_size > _MAX_GREP_FILE_BYTES:
                    skipped += 1
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped += 1
                continue
            scanned += 1
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append({"path": rel, "line": i, "text": line.strip()[:240]})
                    if len(hits) >= cap:
                        truncated = True
                        break
        self._emit("grep", glob, detail=f"{len(hits)} hits / {scanned} files")
        return {"ok": True, "pattern": pat, "glob": glob, "hits": hits,
                "files_scanned": scanned, "files_skipped": skipped,
                "truncated": truncated}

    # ---------------- 元信息 ----------------
    def stats(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "max_bytes": self.max_bytes,
            "max_lines": self.max_lines,
            "max_files": self.max_files,
            "max_hits": self.max_hits,
            "deny_patterns": len(self.deny_patterns),
            "deny_segments": sorted(self.deny_segments),
        }


def build_repo_reader(core: Any = None) -> RepoReader:
    """按配置构造受控读取器（配置缺失时用安全默认值）。

    刻意**不**把根目录做成可配项：仓库根就是这份代码自己所在的位置，
    允许配置等于给了一条「让它去读别的地方」的通道。
    """
    cfg = getattr(getattr(core, "config", None), "repo_read", None)
    kwargs: dict[str, Any] = {}
    if cfg is not None:
        kwargs = {
            "max_bytes": getattr(cfg, "max_bytes", 20_000),
            "max_lines": getattr(cfg, "max_lines", 400),
            "max_files": getattr(cfg, "max_files", 40),
            "max_hits": getattr(cfg, "max_hits", 40),
        }
        extra = list(getattr(cfg, "deny_extra", None) or [])
        if extra:
            kwargs["deny_patterns"] = tuple(DEFAULT_DENY_PATTERNS) + tuple(str(x) for x in extra)
    return RepoReader(audit=getattr(core, "audit", None), **kwargs)


def repo_read_enabled(core: Any) -> bool:
    """是否启用受控仓库读取（配置缺失 → fail-closed 关闭）。"""
    cfg = getattr(getattr(core, "config", None), "repo_read", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "enabled", False))
