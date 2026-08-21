"""P7.4 — SandboxWriteProvider: the first F5-domain SIDE-EFFECT provider.

Writes files INSIDE a controlled sandbox root (``sandbox_root``) only.  This is
a real side effect (``side_effect=True``) but fully controlled: every target
must resolve inside the sandbox root (traversal-blocked), and the ``execution_key``
is propagated as the EXTERNAL idempotency token (F5.7 C2: F5 identity == external
mutation identity) — the marker file is the external system's authoritative dedupe,
so a repeated invocation with the same key returns ``written=False`` and never
re-writes (F5.7 C4).

Scope limits (P7.4 authorization):

  * F5 domain ONLY — no System B reuse, no anima.tools import, no governance
    layer import, no web UI, no production wiring.
  * No capability_mode / gateway activation.
  * No uncontrolled external mutation: writes are confined to the sandbox root
    and gated by the external idempotency token.
"""
from __future__ import annotations

import hashlib
import threading as _threading
import time
from pathlib import Path
from typing import Any

from anima.runtime.providers.base import Provider, ProviderResult

__all__ = ["SandboxWriteProvider"]

_DEFAULT_SCOPE = "sandbox:write"


class SandboxWriteProvider(Provider):
    """Writes a file inside the sandbox root under an execution_key token."""

    def __init__(
        self,
        sandbox_root: str | None = None,
        *,
        data_dir: str | None = None,
        provider_id: str = "sandbox.write.local",
        max_content_bytes: int = 1_048_576,   # G1: 1 MiB default (fail-closed)
        max_files: int = 1024,                # G2: file-count quota (per sandbox root)
        max_total_bytes: int = 16 * 1024 * 1024,  # G2: 16 MiB aggregate quota
    ) -> None:
        # P7.8 G2: caller-injected sandbox_root wins; otherwise derive a
        # controlled default under data_dir (no cwd / user-home / VPS paths).
        if sandbox_root is not None:
            self._root = Path(sandbox_root)
        elif data_dir is not None:
            self._root = Path(data_dir) / "sandbox_write"
        else:
            raise ValueError(
                "sandbox.write requires either sandbox_root or data_dir "
                "(no uncontrolled default root)"
            )
        # P7.17-A Phase 4: create the sandbox root AT CONSTRUCTION time.  This
        # removes a Windows-specific race where concurrent first invocations
        # resolved the root before another thread's mkdir, yielding an
        # inconsistent (case/normalization) resolved root and a false
        # "path escapes" rejection for a legitimate relative path.
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(
                f"sandbox.write: cannot create sandbox root {self._root}: {exc}"
            ) from exc
        # P7.17-A Phase 4: resolve the root ONCE at construction and reuse the
        # same Path for every traversal check — a stable baseline prevents
        # Windows case/normalization drift across concurrent resolves.
        self._resolved_root = self._root.resolve()
        self._provider_id = provider_id
        # G1/G2: explicit, fail-closed resource boundaries (R3.1). Defaults are
        # conservative; limits are scoped to THIS sandbox root (shared across
        # all execution keys), so distinct keys cannot accumulate unbounded files.
        self._max_content_bytes = max_content_bytes
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        # P7.8 G1: auditable lifecycle state (no governance wiring — "recorded,
        # not driven"). The provider never activates/suspends/revokes itself.
        self._lifecycle = "active"
        # P7.17-A Phase 4: the marker check + write must be atomic per key —
        # concurrent dispatches of the same key must not both pass the marker
        # check and both write (no double side effect, no torn marker).
        self._lock = _threading.RLock()

    @property
    def sandbox_root(self) -> Path:
        """The resolved sandbox root (audit surface)."""
        return self._root

    @property
    def lifecycle(self) -> str:
        """Auditable lifecycle state (currently: active)."""
        return self._lifecycle

    @property
    def capability_id(self) -> str:
        return "sandbox.write"

    @property
    def granted_scopes(self) -> set[str]:
        return {"capability:invoke", _DEFAULT_SCOPE}

    @property
    def side_effect(self) -> bool:
        return True

    @property
    def idempotent(self) -> bool:
        return True

    def _token_path(self, execution_key: tuple) -> Path:
        digest = hashlib.sha256(repr(execution_key).encode("utf-8")).hexdigest()[:32]
        return self._root / ".tokens" / f"{digest}.token"

    def _resolve_within_root(self, path_arg: str) -> Path | None:
        """Resolve a relative path inside the sandbox root, or None if it escapes."""
        target = (self._resolved_root / path_arg).resolve()
        root = self._resolved_root
        if target != root and root not in target.parents:
            return None
        return target

    # -- G2 (R3.1): sandbox quota, shared across all execution keys -----------
    def _within_quota(self, new_content_bytes: int) -> bool:
        """True if one more file of ``new_content_bytes`` fits within quota.

        Counts primary files only (the ``.tokens/`` idempotency dir is excluded),
        so the quota is shared across ALL execution keys — a different key writing
        a distinct path still counts against the same root and cannot accumulate
        unbounded files/bytes.
        """
        count = 0
        total = 0
        for p in self._resolved_root.rglob("*"):
            if p.is_file() and ".tokens" not in p.parts:
                count += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return (
            (count + 1) <= self._max_files
            and (total + new_content_bytes) <= self._max_total_bytes
        )

    # -- P7.8 G3: explicit retention cleanup (.tokens only, primary never deleted)
    def cleanup(self, retention_seconds: float = 0.0) -> int:
        """Delete expired idempotency markers (explicit call; never automatic).

        ``retention_seconds`` is a freshness window on marker mtime; ``<=0``
        removes every marker.  Primary data files are never touched.
        """
        tokens_dir = self._root / ".tokens"
        if not tokens_dir.exists():
            return 0
        cutoff = time.time() - retention_seconds if retention_seconds > 0 else None
        removed = 0
        for marker in tokens_dir.glob("*.token"):
            try:
                if cutoff is None or marker.stat().st_mtime < cutoff:
                    marker.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def invoke(
        self, capability: str, arguments: dict, execution_key: tuple
    ) -> ProviderResult:
        path_arg = arguments.get("path")
        content = arguments.get("content")
        meta = {"provider_id": self._provider_id, "side_effect_recorded": False}

        if not isinstance(path_arg, str) or not path_arg:
            return ProviderResult(
                success=False,
                error="sandbox.write: missing 'path' argument",
                metadata=meta,
            )
        if not isinstance(content, str):
            return ProviderResult(
                success=False,
                error="sandbox.write: missing 'content' argument",
                metadata=meta,
            )

        # G1 (R3.1): fail-closed single-file size cap.
        content_b = content.encode("utf-8")
        if len(content_b) > self._max_content_bytes:
            meta["denied_reason"] = "oversized"
            return ProviderResult(
                success=False,
                error="sandbox.write: content exceeds max_content_bytes "
                      f"({len(content_b)} > {self._max_content_bytes})",
                metadata=meta,
            )

        target = self._resolve_within_root(path_arg)
        if target is None:
            return ProviderResult(
                success=False,
                error=f"sandbox.write: path escapes the sandbox root: {path_arg!r}",
                metadata=meta,
            )

        # G2 (R3.1): fail-closed sandbox quota, shared across all execution keys.
        if not self._within_quota(len(content_b)):
            meta["denied_reason"] = "quota_exceeded"
            return ProviderResult(
                success=False,
                error="sandbox.write: sandbox quota exceeded "
                      f"(max_files={self._max_files}, "
                      f"max_total_bytes={self._max_total_bytes})",
                metadata=meta,
            )

        # G3 (R3.1): safe audit fields only — no plaintext qq / secret / content
        # ever leaves the provider. These land in the durable ExecutionLedger via
        # the upstream executor; the gate audit records subject_hash + decision.
        path_hash = hashlib.sha256(path_arg.encode("utf-8")).hexdigest()[:32]
        content_sha = hashlib.sha256(content_b).hexdigest()
        byte_count = len(content_b)

        # F5.7 C2/C4: the execution_key IS the external idempotency token.  A
        # marker hit means the external system already committed this key, so
        # this invocation is a no-op (authoritative dedupe).
        # P7.17-A Phase 4: the marker check + (possibly) the write are ONE
        # atomic section — concurrent dispatches of the same key serialize
        # here, so exactly one invocation performs the write; the others see
        # the marker and report written=False.
        marker = self._token_path(execution_key)
        with self._lock:
            if marker.exists():
                return ProviderResult(
                    success=True,
                    data={
                        "written": False,
                        "reason": "external idempotency hit",
                        "path_hash": path_hash,
                        "sha256": content_sha,
                        "bytes": byte_count,
                    },
                    metadata=meta,
                )

            # P7.8 G4: close the commit-then-crash window.  If the marker is
            # missing but the target file exists, decide from content:
            #   - content matches -> the write WAS committed, only the marker
            #     was lost -> restore the marker and report written=False
            #     (no re-write);
            #   - content differs  -> an uncommitted conflicting write exists
            #     -> refuse (never overwrite an unknown external state).
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8")
                except OSError as exc:
                    return ProviderResult(
                        success=False,
                        error=f"sandbox.write: cannot read existing target: {exc}",
                        metadata=meta,
                    )
                if existing == content:
                    try:
                        marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.write_text(repr(execution_key), encoding="utf-8")
                    except OSError as exc:
                        return ProviderResult(
                            success=False,
                            error=f"sandbox.write: marker restore failed: {exc}",
                            metadata=meta,
                        )
                    return ProviderResult(
                        success=True,
                        data={
                            "written": False,
                            "reason": "committed marker restored (crash window)",
                            "path_hash": path_hash,
                            "sha256": content_sha,
                            "bytes": byte_count,
                        },
                        metadata=meta,
                    )
                return ProviderResult(
                    success=False,
                    error="sandbox.write: conflict: target exists with different "
                    "content and no idempotency marker (never overwritten)",
                    metadata=meta,
                )

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(repr(execution_key), encoding="utf-8")
            except OSError as exc:
                return ProviderResult(
                    success=False,
                    error=f"sandbox.write: write failed: {exc}",
                    metadata=meta,
                )

            meta["side_effect_recorded"] = True
            return ProviderResult(
                success=True,
                data={
                    "written": True,
                    "path": path_arg,
                    "path_hash": path_hash,
                    "sha256": content_sha,
                    "bytes": byte_count,
                },
                metadata=meta,
            )
