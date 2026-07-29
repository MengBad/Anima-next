"""秘密存储（灵魂补丁 B1）—— Bot 的隐私记忆，影响行为但不被直接查询。

设计：
- Secret dataclass 承载单条秘密（content / 来源 / 影响类型 / 针对谁 / 强度 / 是否泄露）。
- SecretsStore 以 data/secrets.json 落盘（list 形式，便于按 id 去重/追加）。
- 秘密默认不进 chat_memory（不污染人类对话历史）；仅经 SecretInjector（B3）隐式影响生成参数。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("anima.memory.secrets")

_VALID_INFLUENCES = ("jealousy", "resentment", "attachment", "fear", "gratitude")


@dataclass
class Secret:
    id: str
    content: str
    created_at: float
    source_event: str
    influence: str              # jealousy / resentment / attachment / fear / gratitude
    target_uid: Optional[str] = None
    intensity: float = 0.5      # 0-1
    exposed: bool = False       # 是否已被意外泄露

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Secret":
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex),
            content=str(d.get("content") or ""),
            created_at=float(d.get("created_at") or time.time()),
            source_event=str(d.get("source_event") or ""),
            influence=str(d.get("influence") or "gratitude"),
            target_uid=(str(d["target_uid"]) if d.get("target_uid") not in (None, "") else None),
            intensity=float(d.get("intensity", 0.5)),
            exposed=bool(d.get("exposed", False)),
        )


class SecretsStore:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "secrets.json"
        self._secrets: dict[str, Secret] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("secrets", [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    s = Secret.from_dict(it)
                    self._secrets[s.id] = s
                except Exception:
                    logger.debug("秘密记录解析失败，已跳过 id=%s", it.get("id"))
        except Exception:
            logger.exception("读取 secrets.json 失败，使用空集合")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [s.to_dict() for s in self._secrets.values()]
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def add(self, secret: Secret) -> Secret:
        self._secrets[secret.id] = secret
        self._save()
        return secret

    def add_secret(
        self,
        content: str,
        source_event: str,
        influence: str,
        target_uid: Optional[str] = None,
        intensity: float = 0.5,
        secret_id: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> Secret:
        if influence not in _VALID_INFLUENCES:
            logger.warning("未知秘密影响类型 %s，回退 gratitude", influence)
            influence = "gratitude"
        sec = Secret(
            id=secret_id or uuid.uuid4().hex,
            content=content,
            created_at=created_at if created_at is not None else time.time(),
            source_event=source_event,
            influence=influence,
            target_uid=(str(target_uid) if target_uid not in (None, "") else None),
            intensity=max(0.0, min(1.0, float(intensity))),
        )
        return self.add(sec)

    # ------------------------------------------------------------------
    def all(self) -> list[Secret]:
        return list(self._secrets.values())

    def by_influence(self, influence: str) -> list[Secret]:
        return [s for s in self._secrets.values() if s.influence == influence]

    def for_target(self, target_uid: str) -> list[Secret]:
        t = str(target_uid)
        return [s for s in self._secrets.values() if s.target_uid == t]

    def active_secrets(self) -> list[Secret]:
        """未泄露的秘密（B3 注入时只用这些；泄露后不再影响行为）。"""
        return [s for s in self._secrets.values() if not s.exposed]

    def get(self, secret_id: str) -> Optional[Secret]:
        return self._secrets.get(secret_id)

    def mark_exposed(self, secret_id: str, exposed: bool = True) -> bool:
        """标记泄露（B5）。返回是否真的发生状态变化。"""
        s = self._secrets.get(secret_id)
        if s is None or s.exposed == exposed:
            return False
        s.exposed = exposed
        self._save()
        return True

    def count(self) -> int:
        return len(self._secrets)
