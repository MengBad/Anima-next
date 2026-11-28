"""Reflection → Memory 的**生产 producer**（工程债 D-13 验收口径 (a)）。

RFC-0108 把 ``ExecutionResult → Reflection → Memory`` 定义为一个"闭环"，而
``memory_authority.py`` 的模块注释也自称 "This is the **closing of the loop**"。
2026-09-18 审查实测的事实是：``MemoryAuthority.ingest`` **全库没有任何生产调用者**
（唯一导入者是 ``invariants.py``，它只做自检）—— 也就是说闭环此前只存在于测试里。
本模块补上那个 producer。

它**只复用**已存在的宪法引擎，不新增存储、不新增持久化路径：

    ExecutionResult  →  reflect()  →  ReflectionResult  →  MemoryAuthority.ingest()
       (RFC-0107)       (RFC-0109)        (RFC-0108)

四条纪律（对齐 D-13 / D-14 的边界）：

1. **不伪造经验**（这是 B1 的教训）。输入必须是**真实发生过**的承载结果 —— 调用方
   传入真实的 success/failure 与 outcome。本模块不得现场合成常量替身 Memory：
   ``_constitution_drive`` 里被摘掉的正是那种东西（2 个 mood 标量 + 常量
   ``relation_depth``）。合成出来的"经验"不是经验，只是把激活门顶开的道具。
2. **不落盘**。只写进程内的 ``MemoryEngine``（``MemoryAuthority`` 持有的那一个）。
   不新建 ``data/`` 文件、不驱动 F5 ``runtime/``（D-14 已裁定休眠）。
3. **有界**。``MemoryEngine.ingest_episode`` 是无上限 append 且不去重
   （``memory_engine.py:36``），所以有界性必须由本层负责：指纹去重 + 进程内条数上限。
   ⚠️ 去重粒度由 **RFC-0109 的 ``_fingerprint`` 决定，且比直觉粗**：它只哈希
   ``(plan_ref, status, outcome, lessons)``，**不含 timestamp**（``reflection.py:81-91``）。
   因此「同一群里同样的结果」永远算同一次经验，只会沉淀一条 —— 这是引擎的既定形状
   （"引用 THIS ExecutionResult 的内容指纹"），本层**不去对抗它**。
   实际增长因此远低于"每次承载一条"：每个 (plan_ref, outcome) 组合至多一条。
4. **可审计**。每次沉淀返回结构化摘要（status / records / evidence_ref），调用方把
   它写进 audit —— 让"闭环是否真的跑过"可被外部核查，而不是只能靠读代码相信。

⚠️ 诚实边界（**D-13 第二层**，本模块刻意不掩盖、也不擅自修补）：

    ``reflect()`` 产出的 semantic 记录只有**文本** ``{"lesson": <str>}``
    （``reflection.py:67``），而 ``derive_identity`` 只把 semantic 的**数值**字段
    聚成 traits（``identity.py:35-36``）⇒ 本闭环接通后 ``identity_traits`` **仍然是 0**，
    ``need.urgency`` 仍然是 0。要让 ``autonomy.drive_gate`` 能被诚实地打开，还需要
    一条**数值 trait** 的生产路径：定义 trait 词汇表属于人格/宪法边界决策
    （RFC-0101 Identity vs RFC-0103 Affect 的分界），需 owner 裁定，本模块不引入。

    同理，``reflect()`` 对 ``FAILED`` 承载**不产任何记录**（``reflection.py:52``），
    因此失败的承载不会留下记忆 —— 这是引擎既有形状，本模块如实返回 ``filtered``。
"""
from __future__ import annotations

import logging

from anima.constitution.identity import derive_identity
from anima.constitution.memory_authority import MemoryAuthority
from anima.constitution.reflection import reflect
from anima.constitution.state import (
    ExecutionResult,
    ExecutionStatus,
    ReflectionResult,
)

logger = logging.getLogger("anima.constitution.reflection_loop")

# 进程内沉淀上限（安全阀）。防"每次承载都 append 一条"在长跑进程里无界增长：
# MemoryEngine 没有淘汰 API，且 M1.1 的引擎不应被本层改写（RFC-0108 明确不重写它）。
DEFAULT_MAX_RECORDS = 200


def carrying_to_execution_result(
    *,
    plan_ref: str,
    succeeded: bool,
    outcome: str,
    lessons: "list | tuple" = (),
    timestamp: "float | None" = None,
) -> ExecutionResult:
    """把一次**真实承载**的观察结果包成 RFC-0107 的 ``ExecutionResult``。

    纯构造，无副作用、不落盘。``lessons`` 默认空 —— 真实承载不会自动产出"教训"，
    只有真正的反思（未来接 LLM 蒸馏）才会有；这里不编造。

    ``timestamp`` 可注入，使同一承载可复现同一 ``result_ref``（指纹去重依赖它）。
    """
    kwargs: dict = {
        "plan_ref": str(plan_ref),
        "status": ExecutionStatus.SUCCESS if succeeded else ExecutionStatus.FAILED,
        "outcome": str(outcome),
        "lessons": list(lessons),
    }
    if timestamp is not None:
        kwargs["timestamp"] = float(timestamp)
    return ExecutionResult(**kwargs)


class ReflectionSedimenter:
    """D-13 生产 producer：把真实承载结果沉淀进宪法记忆（有界 / 不落盘 / 可审计）。

    状态是**实例级**的（``_seen`` / ``_ingested``），不是模块级 —— 否则测试之间会
    互相污染，且多个 core 实例会共用去重集合（本项目已有"模块级单例跨测试污染"
    的前例，conftest 专门为此加了 autouse 复位）。

    Args:
        authority: 目标 ``MemoryAuthority``（进程内唯一 Memory 的写门）。
        max_records: 进程内最多沉淀的记录条数；到达后返回 ``capped`` 而不再写入。
    """

    def __init__(self, authority: MemoryAuthority, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        self._authority = authority
        self._max_records = max(0, int(max_records))
        self._seen: set[str] = set()
        self._ingested = 0
        # 2026-11-29：D-13 层二——交付可靠性计数（owner 授权白名单 trait）。
        # 来源是**真实承载**，不是 prompt 自评。失败承载也必须计入，
        # 否则 reliability 恒为 1.0（详见 design/D13-layer2-numeric-traits.md §5）。
        self._carried_ok = 0
        self._carried_fail = 0
        # trait 证据条数（独立于 _ingested，供观测/审计用）
        self._trait_evidence = 0
        # 2026-11-29（阶段 2）：**韧性**计数 —— 失败后仍继续尝试的次数。
        # 与 reliability **正交**：可以很不可靠但很坚持，也可以很可靠但一败就停。
        # 注意：不能把 caution 定义为 fail/total —— 那与 reliability 恒互补（和为 1），
        # 会制造伪多样性，让 derive_need 的 CURIOSITY(>0.5) 与 AVERSION(<0.1) 对称冲突。
        self._continued_after_fail = 0
        self._last_failed = False

    # --- read surface (for audit / tests) -----------------------------------
    @property
    def authority(self) -> MemoryAuthority:
        return self._authority

    @property
    def ingested(self) -> int:
        """已沉淀的记录条数（不是调用次数）。"""
        return self._ingested

    def identity_traits(self) -> int:
        """当前 Memory 经 ``derive_identity`` 可达的 trait 数 —— D-13 第二层的探针。

        闭环接通后该值仍为 0（``reflect()`` 只产文本 lesson），这是**诚实的 0**，
        不是"没接线"的 0。两种 0 的区别正是本债存在的意义。
        """
        try:
            return len(derive_identity(self._authority.snapshot_memory()).traits)
        except Exception:  # noqa: BLE001 - 观测辅助，绝不因它失败而影响沉淀
            logger.debug("identity_traits 探针失败", exc_info=True)
            return 0

    # --- D-13 层二：数值 trait 证据 -------------------------------------------

    def _ingest_trait_evidence(self, succeeded: bool, ref: str,
                               timestamp: "float | None" = None) -> None:
        """写入一条**数值 trait 证据**（2026-11-29，D-13 层二）。

        这条记录**不属于 `reflect()` 的输出** —— reflect 属 RFC-0109 的 M1.9
        纯度契约，改它要走 Foundation Evolution F1 流程。本方法是 sedimenter
        层新增的证据通道，已获 owner 授权的白名单 trait（见 design/
        `D13-layer2-numeric-traits.md` §8 决策 1/2）。

        三个实现要点（三轮验算确认，改动时勿踩）：
          1. `records` 必须是 **dict** 而非 MemoryRecord ——
             `MemoryAuthority.ingest` 内部按 `rec.get("kind")` 读，传对象会
             AttributeError 并被 except 吞掉（已踩过一次）；
          2. `salience` 必须 1.0 —— 累加公式是 `v*salience`，
             用 0.5 会让计数减半、破坏 Laplace 平滑先验；
          3. 写**计数**而非比率 —— 比率由 `derive_identity` 转换（见 identity.py）。

        fail-closed：任何异常只记 debug，绝不影响沉淀主流程。
        """
        try:
            from anima.constitution.state import ReflectionResult
            _key = "delivery_ok" if succeeded else "delivery_fail"
            _content: dict = {_key: 1.0}
            # 阶段 2（2026-11-29）：失败后继续尝试 —— 独立的韧性证据。
            # 由调用方在 sediment 里判定并置位 _pending_resilience。
            if getattr(self, "_pending_resilience", False):
                _content["continued_after_fail"] = 1.0
                self._pending_resilience = False
            _refl = ReflectionResult(
                result_ref=f"{ref}#trait",
                records=[{"kind": "semantic", "content": _content, "salience": 1.0}],
                timestamp=float(timestamp or 0.0),
            )
            self._authority.ingest(_refl)
            # 注意：**不增加 `_ingested`**。该属性的语义是「reflect() 沉淀的记录数」，
            # trait 证据是独立通道，混进来会让 `ingested == out["records"]` 的
            # 契约失效（既有测试正是这么断言的）。
            self._trait_evidence += 1
        except Exception:  # noqa: BLE001 - 证据失败不影响沉淀本身
            logger.debug("trait 证据写入失败（已隔离）", exc_info=True)

    # --- the producer act ---------------------------------------------------
    def sediment(
        self,
        *,
        plan_ref: str,
        succeeded: bool,
        outcome: str,
        lessons: "list | tuple" = (),
        timestamp: "float | None" = None,
    ) -> dict:
        """真实承载 → ``reflect()`` → ``MemoryAuthority.ingest()``。

        Returns:
            可审计摘要 dict：

            - ``status``：``ingested`` / ``duplicate`` / ``capped`` / ``filtered``
            - ``records``：本次真正写入的记录条数（0 表示未写）
            - ``evidence_ref``：被消费的 ``ExecutionResult`` 的确定性指纹
            - ``identity_traits``：沉淀后 Memory 可达的 trait 数（D-13 第二层探针）
            - ``lesson_count``：本次承载携带的 lesson 数（真实承载默认为 0）

        ``filtered`` 的语义：``reflect()`` 判定无可沉淀内容（``FAILED`` 承载即如此），
        这是引擎既有形状，不是错误 —— 但必须如实返回，不能静默当成"已沉淀"。
        """
        er = carrying_to_execution_result(
            plan_ref=plan_ref, succeeded=succeeded, outcome=outcome,
            lessons=lessons, timestamp=timestamp,
        )
        refl: ReflectionResult = reflect(er)
        ref = str(refl.result_ref)
        base = {
            "evidence_ref": ref,
            "lesson_count": len(list(lessons)),
        }

        # 2026-11-29：**先计数，再分流**。
        # 必须放在 filtered/duplicate/capped 之前 —— 因为：
        #   1. FAILED 承载走 filtered 分支（reflect() 不产记录），
        #      但它**确实发生过**，必须计入分母，否则 reliability 恒 1.0；
        #   2. duplicate 表示"同一承载重复观测"，不是"没发生"。
        # 计数是**观测事实**，沉淀是**存储行为**，两者不能混。
        if succeeded:
            self._carried_ok += 1
            # 上一次失败了、这次继续尝试 => 韧性 +1（正交于可靠率）
            if self._last_failed:
                self._continued_after_fail += 1
                # 传给 _ingest_trait_evidence：本条记录额外携带韧性证据
                self._pending_resilience = True
            self._last_failed = False
        else:
            self._carried_fail += 1
            self._last_failed = True

        if not refl.records:
            # reflect() 对 FAILED 不产记录 —— 如实上报，不伪造一条"失败记忆"。
            # 但**失败承载的真实性必须被记录**：trait 证据独立于 reflect 输出，
            # 否则 reliability 永远只统计成功（实测 7 胜 3 负会算成 8/9 而非 7/10）。
            self._ingest_trait_evidence(succeeded, ref, refl.timestamp)
            return {**base, "status": "filtered", "records": 0,
                    "identity_traits": self.identity_traits()}

        if ref in self._seen:
            # 有界纪律 (3)：同一承载重复观测不得重复沉淀（无上限 append 的唯一防线）。
            return {**base, "status": "duplicate", "records": 0,
                    "identity_traits": self.identity_traits()}

        if self._ingested + len(refl.records) > self._max_records:
            logger.warning(
                "宪法反思沉淀已达进程内上限 %d，停止写入（MemoryEngine 无淘汰 API）",
                self._max_records,
            )
            return {**base, "status": "capped", "records": 0,
                    "identity_traits": self.identity_traits()}

        self._authority.ingest(refl)
        self._seen.add(ref)
        self._ingested += len(refl.records)

        # 2026-11-29：D-13 层二 —— 追加一条**数值 trait 证据**。
        #
        # 为什么不放在 reflect() 里：reflect() 属 RFC-0109（M1.9 纯度契约），
        # 改它要走 Foundation Evolution F1 流程。本条是**本层新增**的证据记录，
        # 不是 reflect 的输出 —— 这一点必须在 docstring 与本注释中如实写明。
        #
        # 为什么写「计数」而不是「比率」：
        # `derive_identity` 的累加语义是为「多条证据叠加」设计的，
        # 写比率会被反复叠加顶爆 peak_intensity（验算实测 4.676），
        # 由 identity 层负责把计数转成比率才是语义正确的分工。
        #
        # salience 必须 = 1.0：累加公式是 v*salience，
        # 用 0.5 会让计数减半，破坏 Laplace 平滑的先验（验算实测）。
        self._ingest_trait_evidence(succeeded, ref, refl.timestamp)

        return {**base, "status": "ingested", "records": len(refl.records),
                "identity_traits": self.identity_traits()}
