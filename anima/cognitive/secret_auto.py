"""秘密自动生成（灵魂补丁 B2）—— 每日心跳根据群观察生成秘密。

仅用本地规则扫描 chat_memory.json / bot_relations.json / relation 数据，不调 LLM。
触发规则（轻量启发式）：
- jealousy：某其他用户被夸次数 >> 本喵被夸次数 → 嫉妒
- resentment：bot_relations 中 rivalry ≥ 0.3 → 记仇
- attachment：relation 中熟悉度 ≥ 0.7 → 依恋
- fear：用户伤痕严重度 > 0.6 → 畏惧（复用 scar 数据）
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional

from anima.memory.secrets import Secret, SecretsStore

logger = logging.getLogger("anima.cognitive.secret_auto")

_PRAISE_RE = re.compile(r"(夸|厉害|牛|棒|强|神|绝了|牛逼|优秀|崇拜|喜欢你|可爱)", re.IGNORECASE)
_SELF_NAME_HINTS = ("哈基米", "本喵", "anima", "哈基米酱")

# jealousy 阈值
_OTHER_PRAISED_MIN = 3
_OTHER_VS_SELF_RATIO = 3.0


def _parse_round_uid(key: str) -> Optional[str]:
    m = re.search(r"group_(\d+)_(\d+)$", key)
    return m.group(2) if m else None


def _mentioned_name(content: str, names: set[str]) -> Optional[str]:
    for n in names:
        if n and n in content:
            return n
    return None


def detect_daily_secrets(
    data_dir: str,
    *,
    self_name: str = "哈基米",
    name_to_uid: Optional[dict[str, str]] = None,
    now: Optional[float] = None,
) -> list[Secret]:
    """扫描当日群观察，返回应生成的秘密列表（未落盘）。"""
    data_dir = Path(data_dir)
    now = now or time.time()
    secrets: list[Secret] = []

    # —— 收集已知用户昵称（用于「被夸的是谁」识别）——
    known_names: set[str] = set()
    cm_path = data_dir / "chat_memory.json"
    raw_cm = {}
    if cm_path.exists():
        try:
            raw_cm = json.loads(cm_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("读取 chat_memory.json 失败", exc_info=True)
    for key, rounds in raw_cm.items():
        if not isinstance(rounds, dict):
            continue
        for rk, msgs in rounds.items():
            if isinstance(msgs, list):
                for e in msgs:
                    if isinstance(e, dict):
                        n = e.get("name")
                        if n:
                            known_names.add(str(n))

    # —— 统计赞美（本喵 vs 其他）——
    self_praised = 0
    other_praised: dict[str, int] = {}
    other_uid: dict[str, str] = {}
    if name_to_uid:
        for n, u in name_to_uid.items():
            other_uid[n] = str(u)
    for key, rounds in raw_cm.items():
        uid = _parse_round_uid(key)
        if not isinstance(rounds, dict):
            continue
        for rk, msgs in rounds.items():
            if not isinstance(msgs, list):
                continue
            for e in msgs:
                if not isinstance(e, dict):
                    continue
                content = str(e.get("content") or "")
                if not _PRAISE_RE.search(content):
                    continue
                # 提到本喵 → 本喵被夸
                if any(h in content for h in _SELF_NAME_HINTS) or (self_name and self_name in content):
                    self_praised += 1
                    continue
                mentioned = _mentioned_name(content, known_names)
                if mentioned:
                    other_praised[mentioned] = other_praised.get(mentioned, 0) + 1
                    if uid:
                        other_uid.setdefault(mentioned, uid)

    # —— jealousy ——
    if other_praised:
        top_name = max(other_praised, key=lambda n: other_praised[n])
        top_cnt = other_praised[top_name]
        if top_cnt >= _OTHER_PRAISED_MIN and top_cnt > self_praised * _OTHER_VS_SELF_RATIO:
            intensity = min(1.0, 0.3 + top_cnt / 30.0)
            secrets.append(Secret(
                id=_new_id(),
                content=f"其实有点嫉妒{top_name}，TA总是被夸",
                created_at=now,
                source_event=f"{top_name}被夸{top_cnt}次，本喵只被夸{self_praised}次",
                influence="jealousy",
                target_uid=other_uid.get(top_name),
                intensity=intensity,
            ))

    # —— resentment（来自 bot_relations）——
    rel_path = data_dir / "bot_relations.json"
    if rel_path.exists():
        try:
            rels = json.loads(rel_path.read_text(encoding="utf-8")).get("relations", {})
            for uid, d in rels.items():
                if not isinstance(d, dict):
                    continue
                if float(d.get("rivalry") or 0.0) >= 0.3:
                    secrets.append(Secret(
                        id=_new_id(),
                        content=f"{d.get('bot_name') or uid}对我不友好的时候，本喵其实偷偷记仇了",
                        created_at=now,
                        source_event=f"{d.get('bot_name') or uid} rivalry={d.get('rivalry')}",
                        influence="resentment",
                        target_uid=str(uid),
                        intensity=min(1.0, float(d.get("rivalry") or 0.3)),
                    ))
        except Exception:
            logger.debug("读取 bot_relations.json 失败", exc_info=True)

    # —— attachment（来自 relation 熟悉度）——
    try:
        from anima.cognitive.relation import get_relation
        rel_sys = get_relation()
        for uid, info in rel_sys.all().items():
            fam = float(getattr(info, "familiarity", 0.0) or 0.0)
            if fam >= 0.7:
                secrets.append(Secret(
                    id=_new_id(),
                    content=f"其实很喜欢和{getattr(info, 'name', uid)}聊天，但不好意思说",
                    created_at=now,
                    source_event=f"熟悉度={fam:.2f}",
                    influence="attachment",
                    target_uid=str(uid),
                    intensity=min(1.0, 0.4 + fam / 3.0),
                ))
    except Exception:
        logger.debug("读取 relation 失败（跳过 attachment）", exc_info=True)

    # —— fear（来自 scar 严重度）——
    try:
        from anima.cognitive.scar import get_scar_system
        scar = get_scar_system(str(data_dir))
        for s in scar.all():
            if s.severity > 0.6:
                secrets.append(Secret(
                    id=_new_id(),
                    content=f"其实有点怕{s.target_uid}，但不想承认",
                    created_at=now,
                    source_event=f"伤痕严重度={s.severity:.2f}",
                    influence="fear",
                    target_uid=str(s.target_uid),
                    intensity=min(1.0, 0.3 + s.severity / 2.0),
                ))
    except Exception:
        logger.debug("读取 scar 失败（跳过 fear）", exc_info=True)

    return secrets


def maybe_generate_daily_secrets(
    data_dir: str,
    *,
    self_name: str = "哈基米",
    name_to_uid: Optional[dict[str, str]] = None,
    now: Optional[float] = None,
) -> list[Secret]:
    """检测并去重落盘；返回本次新增的秘密。"""
    store = SecretsStore(data_dir)
    existing = {(s.influence, s.target_uid) for s in store.all()}
    new_secrets = detect_daily_secrets(
        data_dir, self_name=self_name, name_to_uid=name_to_uid, now=now,
    )
    added: list[Secret] = []
    for s in new_secrets:
        key = (s.influence, s.target_uid)
        if key in existing:
            continue
        store.add(s)
        added.append(s)
        existing.add(key)
    return added


def _new_id() -> str:
    return "sec_auto_" + str(int(time.time() * 1000)) + "_" + str(random.randint(0, 9999))
