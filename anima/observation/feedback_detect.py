# -*- coding: utf-8 -*-
"""P3 —— 工程反馈实时识别（Feedback Capture，只读、只建议）。

把「用户/群友指出 Bot 出错」的话，从普通聊天里识别出来，并归类为工程反馈信号。

设计纪律（延续 R10-01 / R19 提案闭环）：
- 确定性规则（阈值/关键词驱动，无 LLM），可单测、可复现、可审计。
- 只**识别 + 归集**：产出 FeedbackSignal（含 evidence），绝不自动改生产、
  绝不自我授权。真正修复走既有 R8 开发闭环 + Owner 审批门（Self-improvement
  ≠ Self-authorization）。
- fail-closed：拿不准就当普通聊天（返回 None），绝不把闲聊误判成 bug。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Goal §14 反馈类别：bug / behavior / memory / persona / tool / image / system
_FEEDBACK_KINDS = ("bug", "behavior", "memory", "persona", "tool", "image", "system")

# 纠错动词 + 明确对象：命中即进入工程反馈候选。
_CORRECTION_RE = re.compile(
    r"(你(?:刚才|刚刚|之前|上次|又)?(?:说错|答错|回答错|搞错|弄错|记错|认错|识别错|"
    r"判断错|讲错|写错|搞混|弄混|记混|认混|弄反|说反|弄岔|搞岔)了?)|"
    r"(你[^，。！？]{0,6}成[^，。！？]{0,6})|"  # 你说成X / 你识别成X
    r"(有问题|错了|不对|错了啊|不对啊|不是这样的|别乱说|别瞎说|你胡说|乱讲|瞎讲|"
    r"崩溃了?|又崩溃|卡死了?|死机了?)",
    re.I,
)

# B5：纯情绪抱怨词（无具体故障对象时不构成工程反馈）。
_EMOTION_COMPLAINT_RE = re.compile(
    r"(让人?失望|嘴硬|上纲上线|态度[^。，]{0,4}(?:差|不好)|生气|讨厌你|烦人|不理我|骄傲|自大)"
)
# 具体故障对象/证据词：命中其一即视为可行动反馈。
_CONCRETE_EVIDENCE_RE = re.compile(
    r"(报错|崩溃|异常|失败|乱码|复读|链接|网页|图[片]?|看图|搜索|搜到|代码|运行|"
    r"记错|说错[^。，]{0,16}(?:内容|话|事实|名字)|答错[^。，]{0,16}(?:题|数字|事实))"
)

# 类别关键词：优先级从高到低（image > tool > memory > persona > behavior > bug > system）
_CATEGORY_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("image", "image",
     ("图", "照片", "图片", "识别", "视觉", "看图", "这图", "图里", "猫", "狗",
      "颜色", "画面", "表情", "贴图")),
    ("tool", "tool",
     ("工具", "搜索", "搜到", "查", "代码", "运行", "执行", "函数", "结果", "调用",
      "链接", "网页", "天气", "股价", "比分")),
    ("memory", "memory",
     ("记", "记忆", "忘", "历史", "之前说", "上次说", "昨天说", "档案", "资料",
      "说过", "回忆")),
    ("persona", "persona",
     ("人格", "人设", "语气", "口吻", "性格", "角色", "猫娘", "哈基米", "身份",
      "是谁", "不是这样说话")),
    ("behavior", "behavior",
     ("回复", "响应", "态度", "做法", "行为", "不该", "应该", "反应", "应对")),
    ("bug", "bug",
     ("bug", "崩溃", "卡", "死机", "报错", "异常", "失败", "乱码", "重复", "复读",
      "抽风")),
    ("system", "system",
     ("系统", "服务", "掉线", "不在线", "没反应", "收不到", "发不出", "延迟", "慢")),
)


@dataclass
class FeedbackSignal:
    """一条被识别的工程反馈（Goal §14 Issue 的只读前身）。"""

    kind: str                    # bug | behavior | memory | persona | tool | image | system
    source: str                  # qq_group | qq_private
    sender: str = ""
    message_id: Any = None
    conversation_id: Any = None
    problem: str = ""
    evidence: str = ""
    severity: str = "medium"     # low | medium | high
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "source": self.source,
            "user": self.sender,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "problem": self.problem,
            "evidence": self.evidence,
            "severity": self.severity,
            "target": self.target,
        }

    def to_observation_event(self):
        """桥接为 ObservationEvent，进入既有 R10/R19 观测闭环。"""
        from .event import ObservationEvent, EVENT_USER_FEEDBACK, SOURCE_FEEDBACK
        sev = {"low": "low", "medium": "normal", "high": "high"}.get(self.severity, "normal")
        return ObservationEvent(
            type=EVENT_USER_FEEDBACK,
            source=SOURCE_FEEDBACK,
            severity=sev,
            title=f"[{self.kind}] {self.problem[:60]}",
            detail=self.problem,
            evidence=self.evidence,
            target=self.target,
        )


def _classify(text: str) -> str:
    """按类别规则给纠错文本归类；无命中回退 bug。"""
    for kind, _key, keywords in _CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return kind
    return "bug"


def detect_engineering_feedback(
    text: str,
    *,
    sender_name: str = "",
    message_id: Any = None,
    conversation_id: Any = None,
    source: str = "qq_group",
) -> Optional[FeedbackSignal]:
    """识别一条用户消息是否为工程反馈（指出 Bot 出错）。

    只识别**明确纠错 Bot** 的表述，不识别普通聊天/提问。返回 None 表示非反馈。
    不触发任何修复动作：本函数纯只读，产出信号交由既有 Owner 审批闭环。
    """
    t = (text or "").strip()
    if not t or len(t) > 200:
        # 超长当普通聊天（工程反馈通常是短句直指问题）
        return None
    if not _CORRECTION_RE.search(t):
        return None
    # B5（2026-09-06 第三轮体验测试）：纯情绪抱怨（失望/嘴硬/态度差）没有
    # 具体故障对象 → 当普通聊天。fail-closed 纪律：拿不准就不当反馈。
    if _EMOTION_COMPLAINT_RE.search(t) and not _CONCRETE_EVIDENCE_RE.search(t):
        return None
    # 带 @ 本喵/「哈基米」/「你」更可能是纠错；但纯「错了」也可能指向第三方。
    # 保守策略：命中纠错动词即可，避免漏掉「你刚才回答错了」这类高价值反馈。
    kind = _classify(t)
    severity = "high" if any(k in t for k in ("崩溃", "错", "不对", "bug", "Bug")) else "medium"
    return FeedbackSignal(
        kind=kind,
        source=source,
        sender=sender_name,
        message_id=message_id,
        conversation_id=conversation_id,
        problem=t,
        evidence=f"用户原话：{t}",
        severity=severity,
        target="",
    )
