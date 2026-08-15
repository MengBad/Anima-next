"""R6.2-01/02 — 能力撤销 + 全局熔断（execution-boundary 强制）。

本模块是两种治理态的唯一真相源，二者都 **必须在 execution boundary 拒绝**
（而非靠 UI 隐藏）：

  * 按能力的撤销（Revocation）：能力可从 ACTIVE → REVOKED。一旦 REVOKED，
    任何调用（含 owner）在 provider / registry 边界被 DENY。
  * 全局 capability 熔断（Kill Switch）：开启时所有 governed capability
    一律 DENY（fail-closed），含 python.exec。

刻意保持极小、零依赖（不 import anima.runtime / anima.tools），使任意 governed
provider 都能读取而不引入新的 import 边。治理态为进程内单例，provider / registry
在每次 invoke 时实时读取，故治理控制器可热切换而无需重启。
"""
from __future__ import annotations

import threading


class GovernanceState:
    REVOKED = "REVOKED"
    ACTIVE = "ACTIVE"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revoked: set[str] = set()
        self._global_kill: bool = False

    # --- 能力撤销（Feature 1）---
    def revoke(self, capability_id: str) -> None:
        """将能力置为 REVOKED：任何后续调用在边界被 DENY。"""
        with self._lock:
            self._revoked.add(capability_id)

    def restore(self, capability_id: str) -> None:
        """撤销恢复为 ACTIVE。"""
        with self._lock:
            self._revoked.discard(capability_id)

    def is_revoked(self, capability_id: str) -> bool:
        with self._lock:
            return capability_id in self._revoked

    def status(self, capability_id: str) -> str:
        with self._lock:
            return self.REVOKED if capability_id in self._revoked else self.ACTIVE

    # --- 全局熔断（Feature 2）---
    def set_global_kill_switch(self, on: bool) -> None:
        with self._lock:
            self._global_kill = bool(on)

    def global_kill_switch(self) -> bool:
        with self._lock:
            return self._global_kill

    def reset(self) -> None:
        """测试用：清空全部治理态。"""
        with self._lock:
            self._revoked.clear()
            self._global_kill = False


# 进程级单例：governed provider / registry 在每次 invoke 时实时读取。
_GOVERNANCE_STATE = GovernanceState()


def get_governance_state() -> GovernanceState:
    return _GOVERNANCE_STATE
