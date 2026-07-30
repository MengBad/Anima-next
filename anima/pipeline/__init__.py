"""Pipeline —— 5 阶段消息处理流水线。

阶段：
  1. Permission  — 权限校验
  2. Memory      — 注入短期记忆
  3. Cognition   — 注入时间/欲望上下文
  4. Generation  — LLM 生成回复
  5. Guardrail   — 输出护栏

每个阶段故障隔离，单个阶段异常不影响后续阶段（除 Generation 失败外）。
"""

from __future__ import annotations
import logging
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
        """按序执行所有阶段，支持短路。"""
        for i, stage in enumerate(self.stages):
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
                logger.exception("Pipeline 阶段 %d(%s) 异常，跳过", i, stage.__name__)
        return ctx
