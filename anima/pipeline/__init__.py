"""Pipeline —— 5 阶段消息处理流水线。

阶段：
  1. Permission  — 权限校验
  2. Memory      — 注入短期记忆
  3. Cognition   — 注入时间/欲望上下文
  4. Generation  — LLM 生成回复
  5. Guardrail   — 输出护栏

每个阶段故障隔离：非生成阶段异常跳过继续；Generation 失败显式标记 generation_failed 如实降级。
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("anima.pipeline")


class StageResult(Enum):
    CONTINUE = auto()   # 继续下一阶段
    STOP = auto()       # 停止处理（如权限拒绝）
    SKIP_REPLY = auto() # 继续但不回复（如空消息）


@dataclass
class PipelineContext:
    """贯穿 pipeline 的上下文对象。"""
    # 输入
    event: dict
    text: str
    user_id: int
    group_id: Optional[int]
    detail_type: str          # "group" | "private"
    sender_name: str

    # 输出
    reply: str = ""
    should_reply: bool = True
    stop_reason: str = ""

    # 中间数据
    messages: list[dict] = field(default_factory=list)
    system_prompt: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    # 表情包（Sticker 自收集系统）
    sticker_to_send: Optional[str] = None   # 选中要附带的表情图片本地路径
    atmosphere_style: Optional[str] = None  # 当前群氛围（humorous/casual/formal/neutral）

    # 被提及成员：消息里 @ 或点名的人（qq 列表），供认知阶段注入其记忆
    mentioned_uids: list = field(default_factory=list)

    # 全量多模态视觉理解
    inbound_images: list = field(default_factory=list)   # 本回合入站图 [{url,file,sub_type}]
    vision_descs: list = field(default_factory=list)     # 后台生成的图片描述（供记忆/调试）
    generated_image_paths: list[str] = field(default_factory=list)  # 本回合 owner 生图结果
    #: 本回合 write_file 产出的文本文件 [{name, content}]（2026-10-21）。
    #: 用途：把**文件内容贴回会话** —— 此前只回服务器路径，用户在 QQ 里打不开。
    generated_text_files: list = field(default_factory=list)

    # M0.1 Commit 3b：Facade Surface（旧 core → 新边界投影入口）。
    # Stage 仍经 meta["core"] 读取（3c 迁移后移除）；ctx.core 是窄 facade 表面，
    # 不是数据 ABI（PipelineContextProtocol 不含此字段）。
    core: Any = None


class Pipeline:
    """5 阶段流水线编排器。"""

    def __init__(self):
        self.stages: list[Callable] = []

    def use(self, stage: Callable) -> "Pipeline":
        """注册一个阶段处理器。"""
        self.stages.append(stage)
        return self

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """按序执行所有阶段，支持短路。

        故障隔离契约：非生成阶段异常记录后跳过，流水线继续执行后续阶段。
        P0-A4：Generation（generation_stage）失败不再被无条件吞掉——原实现与
        模块 docstring「除 Generation 失败外」自相矛盾，崩溃后走静默空回，
        谁也说不清为什么没回。改为显式如实降级：标记
        ctx.meta["generation_failed"]=True、置 stop_reason、中止后续阶段，
        交给 handler 空回复分支既有的诚实遥测（reply_unavailable 审计 +
        reply_quality 计数）——不伪造成功，也不再无迹可寻。
        """
        # D4 埋点（2026-09-27）：把「入站 → 出站」中 ReAct **之前**的耗时拆到各阶段。
        # 纯观测：只记录阶段名与毫秒数，不改阶段顺序 / 短路语义 / 返回值。
        # `_pipe_t0` 供 generation 在 ReAct 锚点处计算「pipeline 起点 → ReAct」。
        _t_pipe = time.monotonic()
        ctx.meta["_pipe_t0"] = _t_pipe
        _stage_ms: dict[str, float] = {}
        ctx.meta["_stage_ms"] = _stage_ms
        for i, stage in enumerate(self.stages):
            _t_stage = time.monotonic()
            try:
                result = await stage(ctx)
                if result == StageResult.STOP:
                    logger.info("Pipeline 阶段 %d(%s) 返回 STOP: %s",
                                i, stage.__name__, ctx.stop_reason)
                    break
                elif result == StageResult.SKIP_REPLY:
                    ctx.should_reply = False
                    logger.debug("Pipeline 阶段 %d(%s) 跳过回复", i, stage.__name__)
            except Exception:
                if getattr(stage, "__name__", "") == "generation_stage":
                    logger.exception(
                        "Pipeline 生成阶段失败，如实降级（不伪造回复，stop_reason=generation_failed）")
                    ctx.should_reply = False
                    ctx.stop_reason = "generation_failed"
                    ctx.meta["generation_failed"] = True
                    break
                logger.exception("Pipeline 阶段 %d(%s) 异常，跳过", i, stage.__name__)
            finally:
                _stage_ms[getattr(stage, "__name__", f"stage{i}")] = round(
                    (time.monotonic() - _t_stage) * 1000, 1)
        _total_ms = (time.monotonic() - _t_pipe) * 1000
        logger.info("[阶段耗时] total=%.0fms %s",
                    _total_ms,
                    " ".join(f"{k}={v}" for k, v in _stage_ms.items()))
        # ── 阶段耗时进可观测（2026-10-05）───────────────────────────
        # 复用既有 `MetricsCollector` + `HealthHistory` 周期落盘 ⇒ 不新增 timer/文件。
        # 观测失败**绝不能**影响回复路径（observe_turn 内部已 fail-soft，这里再兜一层）。
        try:
            from anima.pipeline.stages.observability import observe_turn
            observe_turn({"total": _total_ms, **_stage_ms})
        except Exception:  # noqa: BLE001
            logger.debug("阶段耗时观测失败，已忽略", exc_info=True)
        return ctx
