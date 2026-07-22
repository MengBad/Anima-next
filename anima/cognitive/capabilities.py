"""个人能力系统（P1 自生长工具）—— 让 bot 从经历里长出可复用方法论。

设计哲学对齐 next-gen CapabilitiesMixin，但去 AstrBot 依赖，复用生产基建：
- 能力 = 角色自创的方法论（name/description/how_to_use/parameters_schema），
  执行时重新 prompt LLM（execute_capability），**不 eval 任意代码**（安全核心）。
- 置信度脱钩 LLM 自评：新建一律 initial_confidence(0.3) 起步，只有真实使用反馈
  能提上去（next-gen v0.9.4 修正：忽略 LLM 自报高分，防 103/0 膨胀）。
- 语义去重（capability_dedup）+ 健康维护（未用淘汰/硬上限）防膨胀。
- 注册采用 value_score Top-K 晋升（trial-slot，next-gen v0.9.10），打破
  「严格 confidence≥0.65 才注册」的死锁：新建能力必须先被注册成工具才能被使用、
  才能拿到反馈。
- 数据独立 JSON personal_capabilities.json，不挤占人格漂移预算。
"""

from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Optional

from anima.core.persistence import PersistenceProvider
from anima.cognitive.capability_dedup import find_similar_capability
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.capabilities")

# 论文体/工程黑话过滤：所有入口（rumination/owner/selfcode）创建能力都过此闸。
_ACADEMIC_MARKERS = (
    "噪音过滤", "核心概念", "溯源", "重构", "融合", "聚类", "诊断", "评估",
    "迭代", "结构化", "系统化", "模块化", "知识图谱", "语义网络", "表征",
    "特征工程", "维度", "指标", "链路", "闭环", "治理", "管控", "策略性",
    "元认知", "自我解剖", "方法论", "框架", "机制", "范式", "抓手", "赋能",
    "对齐", "落地", "沉淀", "复盘", "组合拳", "颗粒度", "水位", "天花板",
    "漏斗", "中台", "数字化转型", "断点融合", "结构化降温", "视觉检索",
    "偏见溯源诊断", "降解重构",
)


class CapabilityManager:
    """个人能力管理器。"""

    def __init__(self, data_dir: str = "data", **cfg):
        self._persist = PersistenceProvider(data_dir)
        self._path = "personal_capabilities.json"
        self.enabled = cfg.get("enabled", True)
        self.initial_confidence = cfg.get("initial_confidence", 0.3)
        self.auto_register_confidence = cfg.get("auto_register_confidence", 0.65)
        self.dynamic_daily_quota = cfg.get("dynamic_daily_quota", 3)
        self.max_total = cfg.get("max_total", 40)
        self.unused_drop_days = cfg.get("unused_drop_days", 30)
        self.unused_decay_days = cfg.get("unused_decay_days", 14)
        self.dedup_text_threshold = cfg.get("dedup_text_threshold", 0.6)
        self.min_create_text_len = cfg.get("min_create_text_len", 6)
        self.max_zero_usage = cfg.get("max_zero_usage", 12)
        self._daily_register = {"date": "", "count": 0}
        self._data = self._load()

    def _load(self) -> dict:
        return self._persist.read_json(
            self._path, {"version": 1, "capabilities": [], "last_research_ts": ""}
        )

    def _save(self) -> None:
        self._persist.write_json(self._path, self._data)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_or_update(self, cap: dict) -> Optional[str]:
        """创建或更新一个能力。受语义去重约束，合并时累计 usage + corrections。

        Returns:
            最终能力名；未创建（开关关闭 / 名称非法）返回 None。
        """
        if not self.enabled:
            return None
        name = (cap.get("name") or "").strip()
        if not name or len(name) < 2:
            return None
        # 防碎碎念：名+描述+用法总内容至少 min_create_text_len
        total = len(name) + len(cap.get("description", "") or "") + len(cap.get("how_to_use", "") or "")
        if total < self.min_create_text_len:
            return None
        # 论文体/工程黑话过滤：防止能力库被腌成分析报告库
        combined = name + (cap.get("description", "") or "") + (cap.get("how_to_use", "") or "")
        if any(marker in combined for marker in _ACADEMIC_MARKERS):
            logger.info("CapabilityManager: 丢弃论文体/工程黑话能力（name=%s）", name)
            return None

        caps = self._data.setdefault("capabilities", [])

        # 第一道：名字精确匹配
        existing = None
        for i, c in enumerate(caps):
            if c.get("name") == name:
                existing = i
                break
        # 第二道：语义近似合并
        if existing is None:
            similar = find_similar_capability(
                name, cap.get("description", ""), caps, self.dedup_text_threshold
            )
            if similar >= 0:
                existing = similar
                logger.info("能力语义去重：合并到「%s」", caps[similar].get("name", ""))
                cap.pop("name", None)  # 保留主名，不覆盖

        cap["last_updated"] = datetime.now().isoformat()

        if existing is not None:
            old = caps[existing]
            for k, v in cap.items():
                if k in ("corrections", "usage_count", "id", "created_at", "name"):
                    continue
                old[k] = v
            if "corrections" in cap:
                old.setdefault("corrections", []).extend(cap["corrections"])
            final_name = old.get("name", "")
        else:
            cap.setdefault("id", f"cap_{int(time.time())}")
            cap.setdefault("created_at", datetime.now().isoformat())
            cap.setdefault("usage_count", 0)
            # 置信度脱钩 LLM 自评：新建一律基线起步
            cap["confidence"] = self.initial_confidence
            cap.setdefault("corrections", [])
            cap.setdefault("register_as_independent_tool", False)
            caps.append(cap)
            final_name = cap.get("name", "")

        self._save()
        return final_name

    # ------------------------------------------------------------------
    # 反馈闭环
    # ------------------------------------------------------------------

    def apply_feedback(self, capability_name: str, success: bool, reflection: str = "") -> bool:
        """对某个能力使用后的自我修正。成功 +0.08，失败 -0.15 并记 correction。"""
        caps = self._data.get("capabilities", [])
        for cap in caps:
            if cap.get("name") == capability_name:
                cap["usage_count"] = cap.get("usage_count", 0) + 1
                old_conf = cap.get("confidence", self.initial_confidence)
                if success:
                    cap["confidence"] = min(0.98, old_conf + 0.08)
                else:
                    cap["confidence"] = max(0.1, old_conf - 0.15)
                    cap.setdefault("corrections", []).append({
                        "ts": datetime.now().isoformat(),
                        "what_was_wrong": reflection or "使用后发现效果不佳",
                        "new_confidence": cap["confidence"],
                    })
                self._save()
                return True
        return False

    def feedback_for_tool(self, tool_name: str, success: bool, reflection: str = "") -> bool:
        """工具名 → 能力名 映射后应用反馈（生成阶段调用）。"""
        caps = self._data.get("capabilities", [])
        low = tool_name.lower()
        for cap in caps:
            cn = (cap.get("name", "") or "").lower()
            if cn and (low in cn or cn in low):
                return self.apply_feedback(cap["name"], success, reflection)
        return False

    # ------------------------------------------------------------------
    # 语义去重（LLM 判断「解决问题/场景/方法」是否重复，攻破表面文本盲区）
    # ------------------------------------------------------------------

    @scoped_llm("capabilities", "deduplicate")
    async def find_semantic_duplicate(self, llm, name: str, description: str) -> Optional[str]:
        """用 LLM 判断新能力是否与某个已有能力语义重复（即使名字不同）。

        表面文本相似度（text_similarity/Jaccard）对中文长名「对话话题净化与记忆纠偏」
        vs「角色对话记忆粘性纠偏」这类近义变体无能为力（共享字少、Jaccard 低），
        于是 11 个冗余能力漏网。这里让 LLM 看已有清单 + 候选，做语义级判定。

        命中返回已有能力名（调用方据此把新能力 merge 进它，而非新建）；
        不重复返回 None。LLM 失败安全退化为 None（不阻断创建）。
        """
        caps = self._data.get("capabilities", [])
        if not caps:
            return None
        existing = "\n".join(
            f"- {c.get('name', '')}：{(c.get('description', '') or '')[:80]}"
            for c in caps
        )
        prompt = (
            "下面是你已有的「个人能力」清单（名字 + 简述）：\n"
            f"{existing}\n\n"
            f"现在想新增一个能力：名称「{name}」，描述「{description}」。\n"
            "请严格判断：它是否与某个已有能力在「要解决的核心问题 / 使用场景 / 方法」上"
            "语义重复（哪怕名字不同、表述不同）。\n"
            "若重复，只回复那个已有能力的【确切名称】（不要加任何其它字、不要引号）；\n"
            "若不重复，只回复 MERGE_NONE。"
        )
        try:
            out = (await llm.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=60
            ) or "").strip().strip('`"').strip()
            if out and out != "MERGE_NONE":
                for c in caps:
                    if c.get("name") == out:
                        return out
        except Exception:
            logger.exception("能力语义去重 LLM 调用失败")
        return None

    # ------------------------------------------------------------------
    # 价值分 / 排序
    # ------------------------------------------------------------------

    def value_score(self, cap: dict, now=None) -> float:
        """能力价值分（不含自封 confidence）= 使用次数*2 + 修正数*0.5 + 新近度(90d)。"""
        now = now or datetime.now()
        usage = cap.get("usage_count", 0) or 0
        corr = len(cap.get("corrections", []) or [])
        try:
            days = (now - datetime.fromisoformat(cap.get("last_updated", ""))).days
        except Exception:
            days = 999
        recency = max(0.0, 1.0 - days / 90.0)
        return usage * 2.0 + corr * 0.5 + recency

    # ------------------------------------------------------------------
    # 健康维护
    # ------------------------------------------------------------------

    def maintain_health(self) -> None:
        """能力库健康管理：未用超期淘汰 / 降权 / 去重合并 / 硬上限截断。"""
        caps = self._data.get("capabilities", [])
        if not caps:
            return
        now = datetime.now()
        decay_days = self.unused_decay_days
        drop_days = self.unused_drop_days
        survivors: list = []
        dropped = 0
        decayed = False

        for cap in caps:
            conf = cap.get("confidence", self.initial_confidence)
            usage = cap.get("usage_count", 0) or 0
            try:
                days = (now - datetime.fromisoformat(cap.get("last_updated", ""))).days
            except Exception:
                days = 999

            # 未使用且超淘汰天数 → 淘汰（无视置信度）
            if usage == 0 and days > drop_days:
                dropped += 1
                continue
            # 低价值 → 淘汰
            if conf < 0.2 and usage <= 1 and days > 25:
                dropped += 1
                continue
            # 未使用且超降权天数 → 降权
            if usage == 0 and days > decay_days:
                new_conf = max(0.05, conf * 0.9)
                if new_conf != conf:
                    cap["confidence"] = new_conf
                    decayed = True

            # 去重合并
            idx = find_similar_capability(
                cap.get("name", ""), cap.get("description", ""), survivors, self.dedup_text_threshold
            )
            if idx >= 0:
                winner = survivors[idx]
                loser = cap
                if self.value_score(loser, now) > self.value_score(winner, now):
                    winner, loser = loser, winner
                winner["usage_count"] = winner.get("usage_count", 0) + loser.get("usage_count", 0)
                merged = (winner.get("corrections", []) or []) + (loser.get("corrections", []) or [])
                if merged:
                    winner["corrections"] = merged
                survivors[idx] = winner
                continue

            survivors.append(cap)

        # 硬上限
        capped = 0
        if len(survivors) > self.max_total:
            survivors.sort(key=lambda c: self.value_score(c, now), reverse=True)
            capped = len(survivors) - self.max_total
            survivors = survivors[:self.max_total]

        # 零调用能力软上限：未使用能力长期堆积会稀释价值池、挤占配额。
        # 超出 max_zero_usage 时，按价值分升序淘汰最低价值的零调用能力
        # （仍保留有真实使用/修正的，以及最近刚创建、价值分靠新近度撑着的）。
        zero_pruned = 0
        if self.max_zero_usage and self.max_zero_usage > 0:
            zero = [c for c in survivors if (c.get("usage_count", 0) or 0) == 0]
            if len(zero) > self.max_zero_usage:
                # 升序：先淘汰价值最低的零调用能力
                zero.sort(key=lambda c: self.value_score(c, now))
                drop_names = {id(c) for c in zero[: len(zero) - self.max_zero_usage]}
                before = len(survivors)
                survivors = [c for c in survivors if id(c) not in drop_names]
                zero_pruned = before - len(survivors)

        if len(survivors) != len(caps) or decayed or zero_pruned:
            self._data["capabilities"] = survivors
            self._save()
            logger.info("能力健康维护：%d → %d（丢弃 %d / 超限 %d / 零调用淘汰 %d）",
                        len(caps), len(survivors), dropped, capped, zero_pruned)

    # ------------------------------------------------------------------
    # 注入片段
    # ------------------------------------------------------------------

    def build_fragment(self, top_k: int = 6) -> str:
        """生成第一人称「我的个人技能与方法论」片段（按价值分 Top-K）。"""
        caps = self._data.get("capabilities", [])
        if not caps:
            return ""
        lines = [
            "【我的个人技能与方法论】",
            "以下是我通过自己的经历、研究和思考，一点一点学会并创造出来的方法。",
            "遇到合适的场景，我会主动调用其中最贴合的那一个。",
        ]
        for cap in sorted(caps, key=self.value_score, reverse=True)[:top_k]:
            name = cap.get("name", "未命名能力")
            desc = (cap.get("description", "") or "")[:140]
            how = (cap.get("how_to_use", "") or "")[:110]
            conf = cap.get("confidence", self.initial_confidence)
            lines.append(f"\n• {name}（我的置信度 {conf:.0%}）")
            lines.append(f"  {desc}")
            if how:
                lines.append(f"  我通常这样用：{how}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 执行（安全：LLM 提示词方法，无代码 eval）
    # ------------------------------------------------------------------

    @scoped_llm("capabilities", "execute")
    async def execute_capability(self, llm, cap: dict, query: str) -> str:
        """以能力「how_to_use」重新 prompt LLM 执行任务。绝不执行外部代码。"""
        schema = cap.get("parameters_schema")
        schema_note = f"\n参数结构要求：{schema}" if schema else ""
        prompt = (
            f"你正在作为自己创造的个人能力「{cap.get('name', '')}」忠实执行任务。\n\n"
            f"能力描述：{cap.get('description', '')}\n\n"
            f"你自己定义的精确使用方法：\n{cap.get('how_to_use', '')}{schema_note}\n\n"
            f"当前任务输入：{query}\n\n"
            "严格按照你自己写的使用方法给出高质量结构化结果。直接输出结果即可。"
        )
        try:
            result = await llm.chat([{"role": "user", "content": prompt}], temperature=0.6, max_tokens=512)
            return result or "能力执行未返回结果"
        except Exception:
            logger.exception("能力执行失败")
            return "能力执行失败"

    # ------------------------------------------------------------------
    # 晋升注册（打破死锁的核心）
    # ------------------------------------------------------------------

    def promote_top_k(self, core, k: int = 3) -> int:
        """按价值分 Top-K，把「register_as_independent_tool=True」的能力注册成 LLM 工具。

        受每日配额 + 同名跳过约束。新能力（0 使用）通过 trial-slot 获得被使用机会，
        从而赚到真实反馈、提升置信度——打破「严格 0.65 才注册」的死锁。
        返回本次真实新注册数。
        """
        from anima.tools import register_capability_as_tool
        caps = self._data.get("capabilities", [])
        ranked = sorted(caps, key=lambda c: self.value_score(c), reverse=True)
        registered = 0
        for cap in ranked[:k]:
            if register_capability_as_tool(core, cap, self):
                registered += 1
        return registered


# 全局单例
_mgr: Optional["CapabilityManager"] = None


def get_capability_manager(data_dir: str = "data", **cfg) -> "CapabilityManager":
    global _mgr
    if _mgr is None:
        _mgr = CapabilityManager(data_dir, **cfg)
    return _mgr


def close_capability_manager() -> None:
    global _mgr
    _mgr = None


def capability_cfg_dict(config) -> dict:
    """从 AnimaConfig 取出 capabilities 段为 dict（供单例初始化）。"""
    c = getattr(config, "capabilities", None)
    if c is None:
        return {}
    return c.model_dump()
