"""P1-2 记忆压缩：置信度衰减 + 相似聚类合并 + 容量淘汰。

对应设计 ``DESIGN_MEMORY_COMPRESSION.md``。三个动作都作用在
``crossgroup`` 的 ``UserFact`` 上（``data/crossgroup/users.json``）：

1. **daily decay**：非争议事实 ``confidence *= daily_decay``（约 70 天到 0.5）。
2. **merge**：相似事实聚成一簇 → 合成一条（``compressed=True`` /
   ``merge_count`` 累加 / ``source_groups`` 并集），原文保留在 ``merged_from``。
3. **cap**：每人事实数超上限时，按 ``(confidence, timestamp)`` 升序淘汰。

两条硬约束（设计的 §4 回滚 + P1-2「勿污染」）：

- **disputed 免疫**：争议事实不衰减、不合并、不淘汰。
- **全有或全无**：落盘前先 ``.bak-<ts>`` 快照；任一用户压缩抛错只跳过该用户。

与设计文档的实现偏差（见文件末尾 ``DEVIATIONS``）：
- 相似度默认走 **字符级 difflib** 而非 embedding——零成本、零外部依赖、可确定性
  测试；``sim_fn`` 可注入以替换为向量相似度。
- LLM 摘要仅在 ``len(facts) >= min_facts_to_llm``（默认 40）时触发，且失败静默
  回退到「取簇内最新一条为代表」，**不阻塞、不产生额外成本**。
"""
from __future__ import annotations

import copy
import difflib
import json
import logging
import os
import time
from typing import Any, Callable, Optional

from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.crossgroup.compression")

STATE_FILE = "compression_state.json"
DEFAULT_BACKUP_KEEP = 3

DEVIATIONS = (
    "1) 相似度默认用 difflib 字符比（可注入 sim_fn 换向量），非 embedding；"
    "2) LLM 摘要仅在事实数 >= min_facts_to_llm(40) 时触发，失败静默回退；"
    "3) summary()/recall() 排序未改（仍按时间），降权由夜间 cap 淘汰完成。"
)


# ---------------------------------------------------------------------------
# 配置（无 config 对象时全部走默认值，便于单测）
# ---------------------------------------------------------------------------
def _cfg(cfg: Any, name: str, default: Any) -> Any:
    v = getattr(cfg, name, None)
    return default if v is None else v


# ---------------------------------------------------------------------------
# 相似度
# ---------------------------------------------------------------------------
_PUNCT = "，。！？、；：\"'“”‘’（）()[]【】,.!?;:-_~ \t\r\n"


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch not in _PUNCT)


def default_similarity(a: str, b: str) -> float:
    """字符级相似度（0-1）。确定性、零依赖，避免为压缩引入 embedding 成本。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# ---------------------------------------------------------------------------
# 纯函数：衰减 / 聚类 / 合并 / 淘汰（同步，便于单测）
# ---------------------------------------------------------------------------
def decay_user_facts(facts: list, daily_decay: float = 0.99) -> int:
    """对所有非争议事实做置信度衰减。返回被衰减条数。"""
    n = 0
    for f in facts:
        if getattr(f, "disputed", False):
            continue
        f.confidence = max(0.0, float(getattr(f, "confidence", 1.0)) * daily_decay)
        n += 1
    return n


def cluster_facts(facts: list, sim_fn: Callable[[str, str], float] = default_similarity,
                  threshold: float = 0.85, min_len: int = 4) -> list[list]:
    """贪心单链聚类。返回 [[fact, ...], ...]。

    - 争议事实（``disputed``）单独成簇，永不与他人合并。
    - 归一化后长度 < ``min_len`` 的碎片不参与合并（防"喜欢猫"与"不喜欢猫"被揉一起）。
    """
    clusters: list[list] = []
    for f in sorted(facts, key=lambda x: float(getattr(x, "timestamp", 0) or 0)):
        if getattr(f, "disputed", False) or len(_norm(f.content)) < min_len:
            clusters.append([f])
            continue
        placed = False
        for c in clusters:
            if len(c) == 1 and (getattr(c[0], "disputed", False)
                                or len(_norm(c[0].content)) < min_len):
                continue
            if any(sim_fn(f.content, g.content) >= threshold for g in c):
                c.append(f)
                placed = True
                break
        if not placed:
            clusters.append([f])
    return clusters


def _merge_cluster(cluster: list, llm_summary: Optional[str] = None) -> Any:
    """把一簇事实合成一条。簇内最新一条为代表的基准（信息最新）。"""
    base = max(cluster, key=lambda f: float(getattr(f, "timestamp", 0) or 0))
    others = [f for f in cluster if f is not base]

    groups: list[str] = []
    for f in cluster:
        for g in (getattr(f, "source_groups", None) or [str(f.source_group)]):
            g = str(g)
            if g not in groups:
                groups.append(g)

    tags: list[str] = []
    for f in cluster:
        for t in (getattr(f, "tags", None) or []):
            if t not in tags:
                tags.append(t)

    merged_from: list[str] = list(getattr(base, "merged_from", None) or [])
    for f in others:
        if f.content not in merged_from:
            merged_from.append(f.content)
        for old in (getattr(f, "merged_from", None) or []):
            if old not in merged_from:
                merged_from.append(old)

    conf_sum = sum(float(getattr(f, "confidence", 1.0)) for f in cluster)
    base.content = (llm_summary or base.content).strip() or base.content
    base.source_group = str(base.source_group)
    base.source_groups = groups if len(groups) > 1 else None
    base.timestamp = max(float(getattr(f, "timestamp", 0) or 0) for f in cluster)
    base.tags = tags
    base.confidence = round(conf_sum / len(cluster), 4)
    base.access_count = sum(int(getattr(f, "access_count", 0) or 0) for f in cluster)
    base.merged_from = merged_from[-6:]
    if len(cluster) > 1 or llm_summary:
        base.compressed = True
    base.merge_count = sum(int(getattr(f, "merge_count", 1) or 1) for f in cluster)
    return base


def enforce_cap(facts: list, max_facts: int) -> int:
    """按 (confidence, timestamp) 升序淘汰，保护 disputed / merge_count>=3 / private。

    直接原地修改传入列表，返回被淘汰条数。
    """
    if max_facts <= 0 or len(facts) <= max_facts:
        return 0
    evicted = 0
    while len(facts) > max_facts:
        pool = [f for f in facts if not getattr(f, "is_protected", False)]
        if not pool:
            break  # 全是保护名单，宁可不淘汰也不丢关键记忆
        victim = min(pool, key=lambda f: (float(getattr(f, "confidence", 1.0)),
                                          float(getattr(f, "timestamp", 0) or 0)))
        facts.remove(victim)
        evicted += 1
    return evicted


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class CompressionEngine:
    """夜间记忆压缩。默认只在低峰窗口（``run_hour``）每日跑一次。"""

    def __init__(self, data_dir: str = "data/crossgroup", cfg: Any = None):
        self.data_dir = data_dir
        self.cfg = cfg
        self._state_path = os.path.join(data_dir, STATE_FILE)
        self._last_run = 0.0
        self._run_count = 0
        self._restore()

    # ---- 状态 ----
    def _restore(self) -> None:
        try:
            if os.path.exists(self._state_path):
                with open(self._state_path, encoding="utf-8") as fh:
                    st = json.load(fh)
                self._last_run = float(st.get("last_run", 0.0))
                self._run_count = int(st.get("run_count", 0))
        except Exception:
            logger.exception("读取压缩状态失败（已忽略，按首次运行处理）")

    def _persist_state(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = f"{self._state_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"last_run": self._last_run,
                           "run_count": self._run_count}, fh)
            os.replace(tmp, self._state_path)
        except Exception:
            logger.exception("写入压缩状态失败（已忽略）")

    # ---- 备份 / 回滚 ----
    def _users_path(self, manager: Any) -> str:
        getter = getattr(manager, "_users_path", None)
        if callable(getter):
            try:
                return str(getter())
            except Exception:
                pass
        return os.path.join(self.data_dir, "users.json")

    def _backup(self, manager: Any) -> Optional[str]:
        src = self._users_path(manager)
        if not os.path.exists(src):
            return None
        try:
            dst = f"{src}.bak-{int(time.time())}"
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
            self._prune_backups(src)
            return dst
        except Exception:
            logger.exception("压缩前快照失败（已忽略）")
            return None

    @staticmethod
    def _prune_backups(users_path: str, keep: int = DEFAULT_BACKUP_KEEP) -> None:
        try:
            base = os.path.dirname(users_path) or "."
            name = os.path.basename(users_path)
            baks = sorted(
                (f for f in os.listdir(base) if f.startswith(f"{name}.bak-")),
                reverse=True,
            )
            for old in baks[keep:]:
                try:
                    os.remove(os.path.join(base, old))
                except OSError:
                    pass
        except Exception:
            pass

    def rollback(self, manager: Any = None) -> bool:
        """把最近一份 ``users.json.bak-*`` 还原回 ``users.json``。"""
        path = self._users_path(manager) if manager is not None else \
            os.path.join(self.data_dir, "users.json")
        base = os.path.dirname(path) or "."
        name = os.path.basename(path)
        try:
            baks = sorted(
                (f for f in os.listdir(base) if f.startswith(f"{name}.bak-")),
                reverse=True,
            )
        except OSError:
            return False
        if not baks:
            return False
        try:
            with open(os.path.join(base, baks[0]), "rb") as a, open(path, "wb") as b:
                b.write(a.read())
            logger.warning("记忆压缩回滚：%s -> %s", baks[0], name)
            return True
        except Exception:
            logger.exception("记忆压缩回滚失败")
            return False

    # ---- 调度 ----
    async def maybe_run(self, crossgroup_manager: Any, vector_memory: Any = None,
                        llm: Any = None, now: Optional[float] = None,
                        force: bool = False) -> dict:
        """每日 gate + 低峰窗口 gate 后调用 :meth:`run`。"""
        if not _cfg(self.cfg, "enabled", True):
            return {"skipped": "disabled"}
        now = time.time() if now is None else now
        if not force and now - self._last_run < float(_cfg(self.cfg, "interval_sec", 86400)):
            return {"skipped": "daily_gate", "last_run": self._last_run}
        hour = int(_cfg(self.cfg, "run_hour", 3))
        if not force and hour >= 0 and time.localtime(now).tm_hour != hour:
            return {"skipped": "not_run_hour", "hour": time.localtime(now).tm_hour}
        report = await self.run(crossgroup_manager, vector_memory=vector_memory, llm=llm)
        if not report.get("error"):
            self._last_run = now
            self._run_count += 1
            self._persist_state()
        return report

    # ---- 主流程 ----
    async def run(self, crossgroup_manager: Any, vector_memory: Any = None,
                  llm: Any = None) -> dict:
        if crossgroup_manager is None:
            return {"error": "no_manager"}
        users = getattr(crossgroup_manager, "_users", None)
        if not isinstance(users, dict) or not users:
            return {"users": 0, "merged": 0, "evicted": 0, "errors": []}

        sim_fn = self._make_sim_fn(vector_memory)
        threshold = float(_cfg(self.cfg, "sim_threshold", 0.85))
        decay_rate = float(_cfg(self.cfg, "daily_decay", 0.99))
        max_facts = int(_cfg(self.cfg, "max_facts_per_user", 30))
        min_to_llm = int(_cfg(self.cfg, "min_facts_to_llm", 40))

        # 先算后写：任一用户抛错只跳过该用户，不污染已算好的结果
        planned: dict[str, list] = {}
        stats = {"merged": 0, "evicted": 0, "decayed": 0}
        errors: list[str] = []
        for uid, user in users.items():
            try:
                # 关键：在**副本**上做压缩。合并是原地改写 base fact，若直接改原对象，
                # 一旦后面落盘失败就无法还原（内存已压缩、磁盘没压缩的半状态）。
                facts = [copy.copy(f)
                         for f in (getattr(user, "facts", None) or [])]
                if not facts:
                    continue
                new_facts, s = await self._plan_user(
                    facts, sim_fn, threshold, decay_rate, max_facts,
                    min_to_llm, llm,
                )
                planned[uid] = new_facts
                for k in ("merged", "evicted", "decayed"):
                    stats[k] += s.get(k, 0)
            except Exception as e:  # per-user 隔离
                errors.append(f"{uid}: {e}")
                logger.warning("用户 %s 记忆压缩失败（已跳过）: %s", uid, e)

        if not planned:
            return {"users": 0, **stats, "errors": errors}

        backup = self._backup(crossgroup_manager)
        # 全有或全无：先记住旧引用，落盘失败时整体还原（磁盘因原子写没变，
        # 但内存里的 UserMemory 已被改写，不还原就会出现「内存与磁盘不一致」）。
        old_refs = {uid: users[uid].facts for uid in planned}
        try:
            for uid, new_facts in planned.items():
                users[uid].facts = new_facts
            flush = getattr(crossgroup_manager, "flush", None)
            if callable(flush):
                flush()
            else:
                crossgroup_manager._save_users()  # noqa: SLF001 - 兜底
        except Exception as e:
            for uid, old in old_refs.items():
                users[uid].facts = old
            errors.append(f"save: {e}")
            logger.exception("记忆压缩落盘失败（数据未改动）")
            return {"error": str(e), "users": 0, **stats, "errors": errors,
                    "backup": backup}

        return {"users": len(planned), **stats, "errors": errors, "backup": backup}

    async def _plan_user(self, facts, sim_fn, threshold, decay_rate, max_facts,
                         min_to_llm, llm) -> tuple[list, dict]:
        stats = {"merged": 0, "evicted": 0, "decayed": 0}
        stats["decayed"] = decay_user_facts(facts, decay_rate)

        clusters = cluster_facts(facts, sim_fn=sim_fn, threshold=threshold)
        merged: list = []
        for c in clusters:
            if len(c) == 1:
                merged.append(c[0])
                continue
            summary = None
            if llm is not None and len(facts) >= min_to_llm and len(c) >= 2:
                summary = await self._llm_summary(llm, c)
            merged.append(_merge_cluster(c, llm_summary=summary))
            stats["merged"] += len(c) - 1

        stats["evicted"] = enforce_cap(merged, max_facts)
        return merged, stats

    @scoped_llm("crossgroup", "facts_compact")
    async def _llm_summary(self, llm: Any, cluster: list) -> Optional[str]:
        """LLM 摘要（仅大用户触发）。任何失败都静默回退为代表句。

        归因（2026-10-03）：用 `scoped_llm` 在**方法边界**打标，而不是给 `llm.chat`
        加 `module=` 参数 —— 后者会改掉调用签名，逼着测试/集成里的假客户端跟着改
        （`scoped_llm` 的 docstring 已明确要避免这一点）。
        """
        try:
            joined = "\n".join(f"- {f.content}（置信:{getattr(f, 'confidence', 1.0)}）"
                               for f in cluster)
            resp = await llm.chat(
                messages=[
                    {"role": "system", "content": "你是记忆整理员。把下面同一语义的多条"
                                                 "事实合并为一句客观陈述，保留关键细节，"
                                                 "不要新增信息。只输出这一句。"},
                    {"role": "user", "content": joined},
                ],
                temperature=0.2, max_tokens=120,
            )
            text = resp if isinstance(resp, str) else \
                (getattr(resp, "content", None) or "")
            text = str(text).strip()
            return text or None
        except Exception:
            logger.debug("LLM 记忆摘要失败（回退代表句）", exc_info=True)
            return None

    @staticmethod
    def _make_sim_fn(vector_memory: Any) -> Callable[[str, str], float]:
        """``vector_memory`` 提供 ``similarity(a,b)`` 时优先用向量相似度。"""
        if vector_memory is None:
            return default_similarity
        sim = getattr(vector_memory, "similarity", None)
        if callable(sim):
            def _sim(a: str, b: str) -> float:
                try:
                    return float(sim(a, b))
                except Exception:
                    return default_similarity(a, b)
            return _sim
        return default_similarity


_engine: Optional[CompressionEngine] = None


def get_compression(data_dir: str = "data/crossgroup",
                    cfg: Any = None) -> CompressionEngine:
    """进程内单例。首次调用后 cfg 固定；测试请直接 ``CompressionEngine(...)``。"""
    global _engine
    if _engine is None:
        _engine = CompressionEngine(data_dir, cfg)
    return _engine
