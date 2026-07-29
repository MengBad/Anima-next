"""意图生成器（B1 阶段新增）"""

from __future__ import annotations
import json
import logging
import time
from typing import Any, Dict

from anima.core.gateway import get_gateway
from anima.cognitive.soul import get_soul_system

logger = logging.getLogger("anima.cognitive.intent_generator")

class IntentGenerator:
    """意图生成器（模型驱动）"""

    def __init__(self):
        self.llm = get_gateway().llm  # 复用现有 gateway

    def generate_intent(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """根据状态生成意图（JSON 输出）"""
        prompt = f"""你叫哈基米，是一个独立自主的QQ机器人。

当前状态：
{json.dumps(state, ensure_ascii=False, indent=2)}

人格：{get_soul_system().get_mood()}

请根据以上状态，生成一个意图（JSON格式），包含：
- intent_type (speak/silence/care/apologize/share/joke/learn)
- target (private/group + user_id)
- content_direction (简短描述想做什么)
- priority (0.0-1.0)
- reasoning (为什么这个意图)

如果觉得应该沉默，就返回 silence 意图。"""

        try:
            response = self.llm.chat_structured(
                [{"role": "system", "content": prompt}],
                [],
                temperature=0.7,
                max_tokens=300
            )
            intent = json.loads(response.get("content", "{}"))
            # 社交礼仪检查（后续 gate 会做最终校验）
            return intent
        except Exception as e:
            logger.warning("意图生成失败: %s", e)
            return {"intent_type": "silence", "priority": 0.0}

# 全局单例
_intent_generator = None

def get_intent_generator() -> IntentGenerator:
    global _intent_generator
    if _intent_generator is None:
        _intent_generator = IntentGenerator()
    return _intent_generator
