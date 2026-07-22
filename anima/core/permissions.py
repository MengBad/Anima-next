"""运行期权限覆盖：仅持久化 WebUI 维护的管理员 QQ 列表。"""
from __future__ import annotations

from anima.core.persistence import PersistenceProvider


class PermissionStore:
    def __init__(self, persistence: PersistenceProvider, config):
        self.persistence = persistence
        self.config = config
        self._apply()

    def _apply(self) -> None:
        raw = self.persistence.read_json("permissions_override.json", {}) or {}
        admins = raw.get("admins")
        if isinstance(admins, list):
            parsed = set()
            for value in admins:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    parsed.add(number)
            self.config.permissions.admins = sorted(parsed)

    def admins(self) -> list[int]:
        return sorted(set(self.config.permissions.admins))

    def add_admin(self, qq: int) -> list[int]:
        if int(qq) <= 0:
            raise ValueError("QQ 号必须为正整数")
        self.config.permissions.admins = sorted({*self.config.permissions.admins, int(qq)})
        self._save()
        return self.admins()

    def remove_admin(self, qq: int) -> list[int]:
        self.config.permissions.admins = [v for v in self.config.permissions.admins if v != int(qq)]
        self._save()
        return self.admins()

    def _save(self) -> None:
        self.persistence.write_json("permissions_override.json", {"admins": self.admins()})
