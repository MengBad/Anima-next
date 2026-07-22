"""反刍引擎 —— 从聊天记忆中提取近期上下文，触发 SelfLearning.ruminate。

触发策略：
- 每 N 秒检查一次（默认 ~15 分钟，见 interval_sec）
- 只在没有最近反思过且记忆库有足够新内容时触发
- 避免在对话高峰期触发（由 Heartbeat 调度）
- 限频目的：防止反思笔记过度膨胀、把元认知黑话灌进生成上下文导致破角色
  （见 reports/2026-07-11_agent_health_report.md）
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from anima.core.persistence import PersistenceProvider
from anima.cognitive.learning import get_learning
from anima.cognitive.world_model import get_world_model
from anima.llm.usage import scoped_llm

logger = logging.getLogger("anima.rumination")


def _extract_json(text: str) -> Optional[dict]:
    """从可能含噪声（前后文字、```json 围栏、Markdown）的文本里截取首个 JSON 对象并解析。

    用在反刍/能力研究场景：LLM 偶尔返回非纯 JSON（带解释、缺闭合括号、混 Markdown），
    直接 json.loads 会抛 JSONDecodeError。这里先整体试，再截取首个 `{` 到末个 `}` 之间
    的内容重试；都失败返回 None，由调用方记 warning 早退，不抛异常污染日志。
    """
    if not text:
        return None
    import json as _json
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return _json.loads(text[start : end + 1])
        except _json.JSONDecodeError:
            return None
    return None


class RuminationEngine:
    def __init__(self, core, data_dir: str = "data", interval_sec: float = 900.0):
        self.core = core
        self.interval_sec = interval_sec
        self._last_run = 0.0
        self._cap_research_last = 0.0
        self._compact_count = 0
        self._persist = PersistenceProvider(data_dir)
        self._state_path = "rumination_state.json"
        self._restore_state()

    def _restore_state(self) -> None:
        state = self._persist.read_json(self._state_path, {})
        self._last_run = state.get("last_run", 0.0)

    def _save_state(self) -> None:
        self._persist.write_json(self._state_path, {"last_run": self._last_run})

    async def maybe_ruminate(self) -> Optional[str]:
        """条件触发反刍。

        关键修正：interval 冷却在「每次尝试」时都推进，而不是仅在成功产
        出反思时推进。否则冷启动（_last_run=0）或预算耗尽导致 ruminate 返回
        None 时，_last_run 永远不更新，5 分钟间隔门控失效，会每个心跳 tick
        都触发 check_drift，瞬间烧光日预算并持续刷「漂移拒绝」日志。
        """
        now = time.time()
        if now - self._last_run < self.interval_sec:
            return None

        # 无论本次是否真正产出反思，都推进冷却，确保 ~interval_sec 才评估一次
        self._last_run = now
        self._save_state()

        if self.core.llm is None:
            return None

        from anima.memory.chat_memory import get_chat_memory
        memory = get_chat_memory()

        # 收集最近所有对话片段
        recent_text = ""
        for key in list(memory._store.keys()):
            od = memory._store[key]
            for _, entries in list(od.items())[-2:]:
                for e in entries:
                    role = e.get("role", "?")
                    name = e.get("name", "?")
                    content = e.get("content", "")
                    recent_text += f"{name}({role}): {content[:100]}\n"

        recent_text = recent_text[-1500:]
        if len(recent_text) < 100:
            return None

        learning = get_learning(self.core.config.persistence.data_dir)
        if learning.is_rumination_paused():
            logger.info(
                "Owner 暂停反刍中 until=%.0f，跳过反思与笔记写入",
                learning.rumination_paused_until,
            )
            # 自生长 / 压缩 / 表情打标仍可跑（不依赖人格漂移笔记）
            await self._self_growth_dispatch(recent_text)
            return None
        world_model = get_world_model(self.core.config.persistence.data_dir)
        result = await learning.ruminate(self.core.llm, recent_text)
        if result:
            # 冷却已在方法开头推进，这里只需沉淀世界观
            world_model.add(result, category="belief", weight=0.4, source="rumination")

        # P0/P1 自生长调度（与反刍同频，每 ~5min 一次，失败不拖垮反刍）
        await self._self_growth_dispatch(recent_text)

        return result

    # ------------------------------------------------------------------
    # 自生长调度（P0 工具规律 + P1 能力研究/健康/晋升）
    # ------------------------------------------------------------------

    async def _self_growth_dispatch(self, recent_text: str) -> None:
        """P0 工具规律总结 + P1 能力研究/健康维护/晋升注册。"""
        # P0：工具使用规律归纳
        try:
            from anima.cognitive.tool_learning import get_tool_learning
            tl = get_tool_learning(self.core.config.persistence.data_dir)
            for tool in tl.summarize_due_tools():
                await tl.summarize_rules(self.core.llm, tool)
        except Exception:
            logger.exception("工具规律总结调度失败")

        # P1：能力系统
        try:
            from anima.cognitive.capabilities import (
                get_capability_manager, capability_cfg_dict,
            )
            cap_cfg = getattr(self.core.config, "capabilities", None)
            if cap_cfg and getattr(cap_cfg, "enabled", True):
                mgr = get_capability_manager(
                    self.core.config.persistence.data_dir,
                    **capability_cfg_dict(self.core.config),
                )
                await self._research_capability(mgr, recent_text)
                mgr.maintain_health()
                mgr.promote_top_k(self.core, k=3)
        except Exception:
            logger.exception("能力系统调度失败")

        # 笔记压缩（低频，避免频繁 LLM 调用）：每 N 次反刍触发一次
        try:
            nc_cfg = getattr(self.core.config, "notes_compact", None)
            if nc_cfg and getattr(nc_cfg, "enabled", True):
                self._compact_count += 1
                if self._compact_count >= nc_cfg.compact_every_ruminations:
                    self._compact_count = 0
                    from anima.cognitive.learning import get_learning
                    learning = get_learning(self.core.config.persistence.data_dir)
                    await learning.compact_notes(self.core.llm, nc_cfg.notes_max_keep)
        except Exception:
            logger.exception("笔记压缩调度失败")

        # 表情包打标：对未打标新图用发图上下文文本生成中文标签（节流，零多模态依赖）
        try:
            st_cfg = getattr(self.core.config, "sticker", None)
            if st_cfg and getattr(st_cfg, "enabled", True) and getattr(st_cfg, "auto_collect", True):
                from anima.cognitive.stickers import get_sticker_manager
                mgr = get_sticker_manager(
                    self.core.config.persistence.data_dir,
                    label_by_vision=getattr(st_cfg, "label_by_vision", True),
                    label_model=getattr(st_cfg, "label_model", "") or "",
                )
                if mgr.count() > 0:
                    await mgr.label_pending(
                        self.core.llm,
                        getattr(st_cfg, "label_model", "") or "",
                    )
        except Exception:
            logger.exception("表情打标调度失败")

    @scoped_llm("capabilities", "research")
    async def _research_capability(self, mgr, recent_text: str) -> None:
        """从近期对话里找反复出现、可用方法论解决的需求，生成长为能力。

        低频门控（每 900s 至多研究一次），避免反刍风暴灌满能力库。
        """
        now = time.time()
        if now - self._cap_research_last < 900:
            return
        self._cap_research_last = now

        prompt = (
            "回顾下面这段最近的对话，判断是否存在「反复出现、可以用一个固定方法或流程解决、"
            "但目前没有现成工具」的需求。\n\n"
            f"{recent_text[:1200]}\n\n"
            "如果找到，请提炼成一个可复用的方法论能力；否则只回复 {{\"need\": false}}。\n"
            '如果找到，回复 JSON：{"need": true, "name": "能力名(简洁中文)", '
            '"description": "这个能力做什么", '
            '"how_to_use": "我通常如何运用它(具体步骤)", '
            '"parameters_schema": {"type":"object","properties":{"query_or_args":{"type":"string","description":"任务描述"}},"required":["query_or_args"]}}\n'
            "命名与描述必须用哈基米这只猫的日常口吻，像随口给自己起的小名/小技能——"
            "绝不用「断点融合 / 结构化降温 / 视觉检索 / 偏见溯源诊断 / 降解重构」"
            "这类论文/工程术语，也不写分析报告腔。例如「帮人认群里头像」「聊天跑题了拽回来」之类。\n"
            "只输出 JSON。"
        )
        try:
            text = await self.core.llm.chat(
                [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=600
            )
            if not text:
                return
            text = text.strip().lstrip("`").replace("```json", "").replace("```", "").strip("`")
            data = _extract_json(text)
            if not isinstance(data, dict):
                logger.warning("能力研究：LLM 未返回可解析 JSON 对象，跳过本次研究")
                return
            if not data.get("need"):
                return
            name = (data.get("name") or "").strip()
            if len(name) < 2:
                return
            cap = {
                "name": name,
                "description": data.get("description", ""),
                "how_to_use": data.get("how_to_use", ""),
                "parameters_schema": data.get("parameters_schema"),
                "register_as_independent_tool": True,
            }
            # 语义去重（攻破表面文本盲区）：命中已有近义能力则合并，而非新建冗余
            dup_of = await mgr.find_semantic_duplicate(
                self.core.llm, name, data.get("description", "")
            )
            if dup_of:
                logger.info("能力语义去重：发现已有能力（name_len=%d）", len(name))
                cap["name"] = dup_of  # create_or_update 按名合并
            final = mgr.create_or_update(cap)
            if final:
                logger.info("自生长能力创建（name_len=%d）", len(final))
        except Exception:
            logger.exception("能力研究失败")
