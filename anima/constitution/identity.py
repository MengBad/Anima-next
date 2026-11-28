"""Identity Engine (M1.2) — f(Memory): the first derivation.

RFC-0101: ``Identity = f(Memory)``, a pure function with no setter. The life
re-derives who it has become every session from Memory; there is no separate
Identity store (canonical Hybrid — persistence is *inherited* via Memory).

This is the first link of the chain; everything downstream (Need, Affect, Drive,
Goal) depends on it. It is implemented as a pure function: same Memory ⇒ same
Identity.
"""
from __future__ import annotations

from anima.constitution.state import Identity, Memory


def derive_identity(memory: Memory) -> Identity:
    """``f(Memory)`` — pure derivation of Identity from sedimented experience.

    Heuristic (M1): aggregate traits/values from salient MemoryRecords. A later
    RFC or Reflection loop may refine the *policy*; the contract (pure function,
    no setter) is fixed by RFC-0101.

    The function reads ``memory`` but must not mutate it — callers pass a snapshot.
    """
    traits: dict[str, float] = {}
    values: list[str] = []
    self_model: dict = {}

    for rec in (*memory.episodes, *memory.semantic, *memory.relations):
        if rec.kind == "semantic":
            for k, v in rec.content.items():
                if k == "value":
                    if v not in values:
                        values.append(v)
                elif isinstance(v, (int, float)):
                    traits[str(k)] = traits.get(str(k), 0.0) + float(v) * rec.salience
        elif rec.kind == "episode":
            # episodes shape the self-model narrative, not scalar traits
            self_model.setdefault("episodes", []).append(rec.content)
        elif rec.kind == "relation":
            self_model.setdefault("relations", []).append(rec.content)

    # 2026-11-29：D-13 层二 —— 把「交付计数」还原成「可靠性比率」。
    #
    # 为什么在 Identity 层做而不是 sedimenter 层：
    # `derive_identity` 的累加语义是为「多条证据叠加」设计的，
    # 若让 sedimenter 直接写比率，比率会被反复叠加顶爆 peak_intensity
    # （三轮验算实测：peak=4.676、urgency 恒 1.0）。
    # 正确分工是——sedimenter 只贡献**计数事实**，Identity 负责**语义转换**。
    #
    # 两个实现级陷阱（验算确认，改动时勿踩）：
    #   ① trait 记录必须 salience=1.0，否则计数被缩小、平滑先验失真；
    #   ② 转换必须插在归一化**之前**，否则拿到的是已归一化的 1.0。
    #
    # Laplace 平滑 (ok+1)/(total+2)：样本少时向 0.5 收缩，避免 1/1 就判定 1.0。
    # 下限 0.15：跳出 `derive_need` 的 AVERSION 阈值(<0.1)，
    # 否则全失败时会产出 `avoid losing: reliability` 这种荒谬目标。
    # 先取三个计数值（都 pop 掉，避免它们作为原始计数进入 traits）。
    # `_fail` 要留到 resilience 计算完再用，故用局部变量而非立即丢弃。
    _ok = traits.pop("delivery_ok", 0.0)
    _fail = traits.pop("delivery_fail", 0.0)
    _cont = traits.pop("continued_after_fail", 0.0)

    if _ok + _fail > 0:
        # Laplace 平滑 (ok+1)/(total+2)：样本少时向 0.5 收缩，
        # 避免 1/1 就判定 1.0（验算：1/1 -> 0.667 而非 1.0）。
        # 下限 0.15：跳出 derive_need 的 AVERSION 阈值(<0.1)，
        # 否则全失败会产出 `avoid losing: reliability` 这种荒谬目标。
        _ratio = (_ok + 1.0) / (_ok + _fail + 2.0)
        traits["reliability"] = round(max(0.15, _ratio), 3)

    # 2026-11-29 阶段 2：**韧性**（resilience）—— 失败后仍继续尝试的比例。
    #
    # 为什么不用 caution（fail/total）：它与 reliability **恒为互补**（和为 1），
    # 等于同一信息的两种写法（验算 7/10 => rel=0.667 cau=0.333，和恒 1.000）。
    # 后果是 derive_need 的 CURIOSITY(>0.5) 与 AVERSION(<0.1) 对称冲突，
    # 且无法表达「很不可靠但很坚持」这种真实组合。
    #
    # resilience 与 reliability **正交**：验算确认能区分
    #   低可靠+高韧性 (0.333 / 0.889) vs 高可靠+低韧性 (0.833 / 0.333)。
    #
    # 无失败样本时不产此 trait —— 「没失败过」推不出韧性，不编造。
    if _cont > 0 and _fail > 0:
        traits["resilience"] = round((_cont + 1.0) / (_fail + 2.0), 3)

    # normalize trait weights to a 0..1 peak so Identity is comparable across sessions
    # 🔴 2026-10-23：归一化便于跨会话比较，但会**抹掉绝对强度**。故额外保留
    #   `peak_intensity`（归一化前的真实峰值）供 `derive_need` 计算 urgency ——
    #   否则「只要有欲望 traits 就是 1.0」⇒ urgency 恒 1.0 ⇒ Drive 永真激活。
    _peak_raw = 0.0
    if traits:
        _peak_raw = round(max(traits.values()), 3)
        peak = max(traits.values()) or 1.0
        # 🔴 2026-11-29：`reliability` / `resilience` **豁免归一化**。
        # 它们已是 0–1 的状态量（比率），再除以峰值会被抬到 1.0，
        # 语义被破坏且 urgency 恒饱和（三轮验算实测确认）。
        _RATIO_TRAITS = frozenset({"reliability", "resilience"})
        traits = {
            k: (round(v, 3) if k in _RATIO_TRAITS else round(v / peak, 3))
            for k, v in traits.items()
        }

    return Identity(traits=traits, values=values, self_model=self_model,
                    peak_intensity=_peak_raw)
