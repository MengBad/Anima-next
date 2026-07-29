"""群/私聊作用域内的情节记忆与明确承诺，不替代原始聊天档案。"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass

from anima.core.persistence import PersistenceProvider


# 用户侧约定 / bot 侧承诺（口语常见面）
_COMMITMENT_HINT = re.compile(
    r"(?:"
    r"我会|我帮你|我来|答应|约好|说好|"
    r"别忘了|记得帮|记得提醒|提醒我|记得叫|"
    r"明天|明日|今晚|稍后|待会|回头|下次|到时候|"
    r"本喵会|本喵帮|本喵来|本喵记得|哈基米会"
    r")"
)
_BOT_PROMISE_HINT = re.compile(
    r"(?:本喵会|本喵帮|本喵来|本喵记得|我会帮|我会记|帮你查|帮你看|帮你盯|记下了|到时候告诉)"
)
# 威胁 / 角色扮演攻击语境——即使含时间词也不算承诺
_THREAT_ROLEPLAY_RE = re.compile(
    r"(?:死定了|气死|弄死|打死|踩死|掐死|咬死|"
    r"把你.*踩|把你.*抠|把你.*拆|把你.*砸|"
    r"今晚死|明天死|等着瞧|你完了|你死定|"
    r"炸毛|尾巴竖|龇牙|嘶吼|狂踩|踩奶|"
    r"踩成筛子|扣下来当|抠下来当)"
)


@dataclass
class Episode:
    scope: str
    user_id: int
    role: str
    summary: str
    salience: float
    id: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        self.id = self.id or f"ep_{uuid.uuid4().hex[:12]}"
        self.scope, self.user_id, self.role = str(self.scope)[:80], int(self.user_id), str(self.role)[:16]
        self.summary = str(self.summary).strip()[:600]
        self.salience = max(0.0, min(1.0, float(self.salience)))
        self.created_at = float(self.created_at or time.time())


@dataclass
class Commitment:
    scope: str
    user_id: int
    text: str
    source_episode_id: str
    id: str = ""
    status: str = "open"
    created_at: float = 0.0
    resolved_at: float = 0.0
    agenda_id: str = ""
    due_at: float = 0.0
    resolution: str = ""  # 新增：完成/取消原因

    def __post_init__(self) -> None:
        self.id = self.id or f"com_{uuid.uuid4().hex[:12]}"
        self.scope, self.user_id = str(self.scope)[:80], int(self.user_id)
        self.text, self.source_episode_id = str(self.text).strip()[:500], str(self.source_episode_id)[:64]
        self.created_at = float(self.created_at or time.time())
        self.agenda_id = str(self.agenda_id or "")[:64]
        self.due_at = max(0.0, float(self.due_at or 0.0))
        self.resolution = str(self.resolution)[:200]
        if self.status not in {"open", "completed", "cancelled"}:
            self.status = "open"


class EpisodicMemory:
    def __init__(self, persistence: PersistenceProvider):
        self.persistence = persistence
        self.episodes: list[Episode] = []
        self.commitments: list[Commitment] = []
        self.restore()

    @staticmethod
    def scope_for(detail_type: str, group_id: int | None, user_id: int) -> str:
        return f"group:{int(group_id)}" if detail_type == "group" and group_id else f"private:{int(user_id)}"

    def restore(self) -> None:
        self.episodes, self.commitments = [], []
        for raw in self.persistence.read_json("episodes.json", []) or []:
            try: self.episodes.append(Episode(**raw))
            except (TypeError, ValueError): continue
        for raw in self.persistence.read_json("commitments.json", []) or []:
            try: self.commitments.append(Commitment(**raw))
            except (TypeError, ValueError): continue

    def save(self) -> None:
        self.persistence.write_json("episodes.json", [asdict(item) for item in self.episodes[-1000:]])
        self.persistence.write_json("commitments.json", [asdict(item) for item in self.commitments[-500:]])

    def observe(self, detail_type: str, group_id: int | None, user_id: int, role: str, text: str,
                *, has_image: bool = False, agenda_factory=None) -> Episode | None:
        text = str(text or "").strip()
        if not text:
            return None
        has_commit = bool(_COMMITMENT_HINT.search(text) or (role == "bot" and _BOT_PROMISE_HINT.search(text)))
        # 威胁 / 角色扮演攻击语境不算承诺（“你今晚死定了”≠ 承诺）
        if has_commit and _THREAT_ROLEPLAY_RE.search(text):
            has_commit = False
        # 约定类降低长度门槛；一般闲聊仍要求更明显的情节信号。
        salient = (
            has_image
            or has_commit
            or len(text) >= 48
            or ("?" in text or "？" in text) and len(text) >= 12
        )
        if not salient:
            return None
        salience = 0.88 if has_commit else (.65 if has_image else .5)
        if role == "bot" and has_commit:
            salience = 0.9
        episode = Episode(
            self.scope_for(detail_type, group_id, user_id), user_id, role, text, salience,
        )
        self.episodes.append(episode)
        # role 兼容 assistant（出站记录）与 bot（内部存储）。
        role_norm = "bot" if role in {"bot", "assistant"} else role
        if has_commit and role_norm in {"user", "bot"}:
            self._capture_commitment(
                episode, group_id=group_id, agenda_factory=agenda_factory, role=role_norm,
            )
        self._prune()
        self.save()
        return episode

    def _capture_commitment(self, episode: Episode, *, group_id: int | None = None,
                            agenda_factory=None, role: str = "user") -> Commitment | None:
        for item in self.commitments:
            if item.status == "open" and item.scope == episode.scope and item.user_id == episode.user_id and item.text == episode.summary:
                return item
        due_at = time.time() + 6 * 3600
        if any(token in episode.summary for token in ("明天", "明日")):
            due_at = time.time() + 20 * 3600
        elif "今晚" in episode.summary:
            due_at = time.time() + 4 * 3600
        elif any(token in episode.summary for token in ("稍后", "待会", "回头")):
            due_at = time.time() + 2 * 3600
        elif "下次" in episode.summary:
            due_at = time.time() + 24 * 3600
        item = Commitment(episode.scope, episode.user_id, episode.summary, episode.id, due_at=due_at)
        # 仅群聊可自动生成跟进议程；私聊只保留约定，避免误发到群。
        if agenda_factory is not None and group_id:
            try:
                # 议程文本须剥离 CQ 码与角色扮演动作，避免污染 LLM 意图
                clean_text = re.sub(r"\[CQ:[^\]]*\]?", "", item.text).strip()
                clean_text = re.sub(r"（[^）]*）", "", clean_text).strip()
                if not clean_text:
                    clean_text = item.text[:80]
                # 不带系统前缀，直接用自然描述作为议程意图
                agenda_text = clean_text[:120]
                agenda = agenda_factory(
                    int(group_id),
                    agenda_text,
                    priority=0.62 if role == "bot" else 0.55,
                    not_before_at=due_at,
                    expires_at=due_at + 12 * 3600,
                    source="commitment",
                )
                item.agenda_id = str(getattr(agenda, "id", "") or "")
            except Exception:
                item.agenda_id = ""
        self.commitments.append(item)
        return item

    def resolve(self, commitment_id: str, *, status: str = "completed", resolution: str = "") -> bool:
        for item in self.commitments:
            if item.id == commitment_id and item.status == "open":
                item.status, item.resolved_at = status, time.time()
                item.resolution = resolution if resolution else status
                self.save()
                return True
        return False

    def open_commitments(self, *, limit: int = 20) -> list[Commitment]:
        items = [item for item in self.commitments if item.status == "open"]
        items.sort(key=lambda item: item.due_at or item.created_at)
        return items[:max(1, min(int(limit), 100))]

    def _prune(self) -> None:
        """限制情节规模并衰减过旧低显著性记忆，避免无限增长。"""
        now = time.time()
        kept: list[Episode] = []
        for item in self.episodes[-1200:]:
            age_hours = max(0.0, (now - item.created_at) / 3600.0)
            if age_hours > 72 and item.salience < 0.55:
                continue
            if age_hours > 24 and item.salience < 0.45:
                item.salience = max(0.1, item.salience * 0.85)
            kept.append(item)
        self.episodes = kept[-1000:]
        self.commitments = self.commitments[-500:]

    def context(self, detail_type: str, group_id: int | None, user_id: int, limit: int = 3) -> str:
        scope = self.scope_for(detail_type, group_id, user_id)
        episodes = [item for item in self.episodes if item.scope == scope and item.user_id == int(user_id)][-limit:]
        commitments = [item for item in self.commitments if item.scope == scope and item.user_id == int(user_id) and item.status == "open"][-3:]
        if not episodes and not commitments:
            return ""
        lines = ["## 正在延续的经历（仅作自然回应依据，不要逐条复述）"]
        lines.extend(f"- {item.summary}" for item in episodes)
        if commitments:
            lines.append("## 尚未完成的明确约定")
            lines.extend(f"- {item.text}" for item in commitments)
        return "\n".join(lines)

    def summary(self) -> dict[str, int]:
        return {"episodes": len(self.episodes), "commitments_open": sum(item.status == "open" for item in self.commitments), "commitments_total": len(self.commitments)}
