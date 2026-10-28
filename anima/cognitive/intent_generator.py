"""意图生成器（B1 阶段新增）。

注意：历史实现曾在模块顶层 `from anima.core.gateway import get_gateway`，
但该符号在 `anima/core/gateway.py` 中从未定义，导致模块一被 import 即
ImportError（潜在损坏懒加载）。现改为依赖注入 + 降级：不依赖不存在的全局
网关入口，LLM 由调用方注入，缺失/失败时优雅返回 silence 意图。
"""

from __future__ import annotations
import json
import logging
from typing import Any, Dict, Optional

from anima.cognitive.soul import get_soul_system

logger = logging.getLogger("anima.cognitive.intent_generator")

class IntentGenerator:
    """意图生成器（模型驱动）"""

    def __init__(self, llm: Optional[Any] = None):
        # 不依赖不存在的 get_gateway()；LLM 由调用方注入（或运行期赋值）。
        self.llm = llm

    def generate_intent(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """根据状态生成意图（JSON 输出）；任何失败/未配 LLM 均降级为 silence。"""
        try:
            _soul = get_soul_system()
            _mood = _soul.describe_state() if _soul and hasattr(_soul, "describe_state") else ""
        except Exception as e:  # 人格系统缺失不应阻断意图生成
            logger.warning("获取人格状态失败: %s", e)
            _mood = ""
        prompt = f"""你叫哈基米，是一个独立自主的QQ机器人。

当前状态：
{json.dumps(state, ensure_ascii=False, indent=2)}

人格：{_mood}

请根据以上状态，生成一个意图（JSON格式），包含：
- intent_type (speak/silence/care/apologize/share/joke/learn)
- target (private/group + user_id)
- content_direction (简短描述想做什么)
- priority (0.0-1.0)
- reasoning (为什么这个意图)

如果觉得应该沉默，就返回 silence 意图。"""

        try:
            if self.llm is None:
                raise RuntimeError("LLM 未注入，意图生成不可用")
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

    def get_status(self) -> dict:
        """可观测快照（2026-10-28 补，供 `/api/modules/status`）。

        ## 这个模块为什么几乎没有状态

        `IntentGenerator` 是**无状态**的：每次 `generate()` 都现场拼 prompt
        调 LLM，不缓存、不落盘、不计数。

        所以 `live_summary()` 的属性扫描扫不到任何容器 —— 这是事实，不是缺陷。

        ## 那还剩什么值得看

        唯一有意义的是 **`llm` 有没有被注入**：

            未注入 ⇒ 每次 `generate()` 都抛 `RuntimeError("LLM 未注入")`
                    ⇒ 它被构造出来了，但实际不能用
            （`get_intent_generator()` 无参构造时 `self.llm = None`，
              生产里由调用方注入 —— 若某处忘了注入，这里就能看出来）

        这正是「instantiated=true 但功能不可用」的典型形态，
        面板上必须能区分，所以显式报 `llm_ready`。
        """
        llm = getattr(self, "llm", None)
        return {
            "stateless": True,          # 明确告知：没有内容可数
            "llm_ready": llm is not None,
            "llm_type": type(llm).__name__ if llm is not None else None,
        }


# 全局单例
_intent_generator = None

def get_intent_generator() -> IntentGenerator:
    global _intent_generator
    if _intent_generator is None:
        _intent_generator = IntentGenerator()
    return _intent_generator
