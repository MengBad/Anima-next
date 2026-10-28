"""统一模块注册表 —— 把「29 个各自为政的 get_xxx 单例」收敛成一个可遍历的注册面。

## 为什么需要这个文件

`anima/cognitive/` 下有 71 个文件、34 个模块级单例工厂（`get_mood_system` /
`get_desire_system` / `get_relation` …）。它们各自维护缓存、各自 `load()`/`save()`，
**但没有任何统一的生命周期**：

    · 34 个里 **23 个没有 close/shutdown**
    · 方法名不统一（`save` / `persist` / `_save`；`load` / `restore` / `_load`）
    · 签名不统一（8 个收 `core`、25 个收 `data_dir`、8 个无参、4 个其它）

后果（已在生产暴露过）：**加一个新子系统，若忘了在
`AnimaCore.shutdown` 里手写一行 `close_xxx()`，就是资源泄漏**。
`desire_decay_state.json` 损坏那次（37 字节残文件）就是这么来的。

`core/module.py` 里其实定义了 `CognitiveModule` 契约，文档写着
「所有 anima/cognitive/ 下的子系统应继承此类」——
**但实测 0 个类继承它，`register_service()` 全库零调用**。
契约是空壳，所以本文档不重写契约，而是**让现有实现能零改动接进来**。

## 设计取向

1. **不改现有模块**。34 个单例继续按原样工作（它们的内部缓存、锁、TTL 全保留）。
   注册表只包一层 `ModuleAdapter`，把「工厂函数」映射成「Module 协议」。
2. **fail-soft**。任何一个模块的 load/save/close 抛异常，都不得影响其它模块，
   也不得让进程崩 —— 这与项目既有的「降级设计」一致。
3. **可观测**。每个模块的 `get_status()` 默认返回「能否调用、是否已实例化、
   有无脏数据」，`/api/modules/status` 能直接列出全部 34 个。
4. **显式登记**。用 `register_spec()` 声明「这个模块叫什么、怎么造、怎么存」，
   装配点集中在本文件的 `default_specs()`，不再散落在 `anima_core.py` 的
   五处 `close_xxx()` 里。

## 用法

    from anima.core.registry import ModuleRegistry, default_specs

    reg = ModuleRegistry(data_dir="data")
    reg.register_all(default_specs())        # 一次性登记 34 个
    await reg.startup(core)                  # init -> restore（逐个，fail-soft）
    await reg.persist_all()                  # 批量落盘（仅 dirty 的）
    await reg.shutdown()                     # 逐个 close（fail-soft）

    reg.status()                             # WebUI 可观测面
    reg.by_name("mood")                      # 取实例
"""

from __future__ import annotations

import importlib
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger("anima.core.registry")


# ---------------------------------------------------------------------------
# 模块描述：声明「叫什么、怎么造、怎么存、怎么关」
# ---------------------------------------------------------------------------

#: 工厂签名类型 —— 现有 34 个单例混着四种，这里显式标注以便统一调用。
KIND_DATA_DIR = "data_dir"   # f(data_dir) -> obj
KIND_CORE = "core"           # f(core) -> obj
KIND_NOARG = "noarg"         # f() -> obj
KIND_CUSTOM = "custom"       # f(custom_args) -> obj


@dataclass
class ModuleSpec:
    """一个可注册模块的声明式描述。

    刻意做成**数据**而不是类：现有模块不继承任何基类，
    我们只描述「怎么拿到它、怎么让它落盘、怎么让它关掉」。
    """

    #: 唯一标识（用于日志/WebUI/按名取用）。建议与工厂函数去掉 `get_` 后一致。
    name: str

    #: 构造入口：`"anima.cognitive.mood:get_mood_system"`
    factory: str

    #: 工厂签名类型（决定注册表怎么调用它）
    kind: str = KIND_DATA_DIR

    #: 传给工厂的额外 kwargs（kind=custom 时用）
    kwargs: dict[str, Any] = field(default_factory=dict)

    #: 持久化方法名。None ⇒ 该模块没有落盘需求（注册表不调）。
    #: 支持 list 形式按序尝试（兼容 `save`/`persist`/`_save` 三种命名）。
    save: str | list[str] | None = "save"

    #: 关闭方法名。None ⇒ 无关闭需求。同样支持 list。
    close: str | list[str] | None = None

    #: 是否为**进程级单例**（缓存跨 core 实例复用）。
    #: True ⇒ shutdown 时不真正销毁（下次 get 还能拿到同一个）。
    process_singleton: bool = True

    #: 人读的用途说明（进 `/api/modules/status`）
    note: str = ""


class ModuleAdapter:
    """把「工厂 + 可选 save/close 方法」包装成统一对象。

    不继承 `core.module.Module`，因为那会要求被包装对象也是 Module ——
    而现有 34 个都不是。这里用**鸭子类型**：只要有对应方法就当它有。
    """

    __slots__ = ("spec", "_inst", "_dirty", "_error", "_created_at")

    def __init__(self, spec: ModuleSpec):
        self.spec = spec
        self._inst: Any = None
        self._dirty = False
        self._error = ""
        self._created_at = 0.0

    # -- 构造 --------------------------------------------------------------

    def _resolve_factory(self) -> Callable[..., Any]:
        mod_path, _, attr = self.spec.factory.partition(":")
        if not attr:
            raise ValueError(f"factory 格式应为 'module:func'，实得 {self.spec.factory!r}")
        mod = importlib.import_module(mod_path)
        fn = getattr(mod, attr, None)
        if not callable(fn):
            raise AttributeError(f"{self.spec.factory} 不是可调用对象")
        return fn

    def construct(self, *, data_dir: str, core: Any = None) -> Any:
        """按 spec 声明的签名类型调用工厂。失败返回 None（fail-soft）。

        ## 为什么要 core 才能构造的模块

        有 4 个模块的工厂**必须**拿到 core（内部读 `core.config`）或必填
        位置参数，无法只靠 `data_dir` 构造：

            exploration  get_exploration_manager(core)        ← 读 core.config
            stickers     get_sticker_manager(data_dir, ...)   ← data_dir 是必填位置参
            danger       get_danger_behaviors(core, cfg)      ← 读 core.config
            life         get_life(core, data_dir)             ← 两者都要

        它们的 spec 用 `KIND_CORE` / `KIND_CUSTOM`，并在 `kwargs` 里显式声明
        需要哪些参数（见 `default_specs()`）。

        若此时 `core is None`（例如启动早期、或测试里没建 core），
        构造会失败 —— 这是**预期行为**：注册表标记为 `deferred`，
        等 `startup(core)` 传入真实 core 时再构造，而不是塞个假 core 骗过去。
        """
        if self._inst is not None:
            return self._inst
        try:
            fn = self._resolve_factory()
            kind = self.spec.kind
            if kind == KIND_DATA_DIR:
                inst = fn(data_dir)
            elif kind == KIND_CORE:
                if core is None:
                    self._error = "deferred: 需要 core 实例"
                    return None
                inst = fn(core)
            elif kind == KIND_NOARG:
                inst = fn()
            elif kind == KIND_CUSTOM:
                kw = dict(self.spec.kwargs)
                # 允许 spec 声明 `needs_core` / `needs_data_dir`，
                # 由注册表在调用时注入（避免把运行时对象写死进数据表）
                if kw.pop("needs_core", False):
                    if core is None:
                        self._error = "deferred: 需要 core 实例"
                        return None
                    kw["core"] = core
                if kw.pop("needs_data_dir", False):
                    kw["data_dir"] = data_dir
                inst = fn(**kw)
            else:
                raise ValueError(f"未知 kind={kind!r}")
            self._inst = inst
            self._created_at = time.time()
            self._error = ""
            # 🔴 2026-10-28 修正：构造成功即视为 **dirty（需要落盘）**。
            #
            # 原实现 `_dirty` 只在 `mark_dirty()` 里置 True，而全仓
            # **0 个调用点**（`grep mark_dirty` 只有两处定义，无调用）。
            # 后果：`shutdown()` 里 `if ad._dirty: await ad.persist()`
            # 这条「关之前先落盘」的分支**永远不会执行**；
            # `persist_all()` 同理（只落 dirty 的）。
            #
            # 线上之所以看起来正常（`[退出落盘] 候选 22，成功 22`），
            # 是因为 `AnimaCore._persist_cognitive_state()` 这条独立路径
            # 先把 22 个都落了一遍 —— 属于**巧合**，不是本机制在起作用。
            #
            # 改成「构造即为 dirty」的语义：新构造出来的实例状态一定
            # 还没落过盘，退出时理应写一次。落盘成功后 `persist()` 会清掉标记，
            # 所以正常 `persist_all()` + `shutdown()` 组合只会写一次，不会重复。
            self._dirty = True
            return inst
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("[registry] 构造 %s 失败（已隔离）：%s",
                           self.spec.name, self._error)
            return None

    # -- 生命周期 ----------------------------------------------------------

    async def _acall_first(self, names: "str | list[str] | None",
                           *args: Any) -> bool:
        """按序尝试调用第一个存在的方法，**支持同步与协程两种实现**。

        ## 为什么必须处理协程

        生产实测：`XBrowserSession.close` 是 `async def`，
        而 `MoodSystem._save` / `RelationEngine.persist` 是同步的。
        同一张 spec 表里混着两种，注册表必须都能调 ——
        否则协程被同步调用会得到
        `RuntimeWarning: coroutine 'XBrowserSession.close' was never awaited`
        并且**实际什么都没执行**（浏览器会话泄漏）。

        判据用 `inspect.isawaitable` 而不是 `asyncio.iscoroutinefunction`：
        后者认不出被装饰器包过的协程，而前者对「调用结果」判断最准。

        ## 两种解析顺序（实例方法 → 模块级函数）

        生产里两种形态混用：
        - **实例方法**：`MoodSystem._save()` / `ReplyQuality.close()`
        - **模块级函数**：`close_tool_learning()` / `close_sticker_manager()`
          （这些是「销毁单例并清缓存」的工厂配套函数，不是实例方法）

        ⇒ 先找实例方法；找不到再看 `spec.factory` 所在模块有没有同名模块级函数。
        两者都没有 ⇒ 返回 False（该模块确实无此能力，不是错误）。
        """
        if not names:
            return False
        seq = [names] if isinstance(names, str) else list(names)
        for n in seq:
            # ① 实例方法
            fn = getattr(self._inst, n, None) if self._inst is not None else None
            if not callable(fn):
                # ② 模块级函数（如 close_tool_learning）
                fn = self._resolve_module_func(n)
            if not callable(fn):
                continue
            try:
                ret = fn(*args)
                if inspect.isawaitable(ret):
                    await ret
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("[registry] %s.%s() 失败（已隔离）：%s",
                               self.spec.name, n, exc)
                return False
        return False

    def has_save(self) -> bool:
        """实例上是否真的存在可调用的 save 方法（实例方法或模块级函数）。

        用于区分「spec 声明了但该实现没有」与「调用了但失败」——
        前者不是错误（很多模块确实无落盘需求），后者才要报 failed。
        """
        return self._probe(self.spec.save)

    def has_close(self) -> bool:
        """实例上是否真的存在可调用的 close 方法。"""
        return self._probe(self.spec.close)

    def _probe(self, names: "str | list[str] | None") -> bool:
        if not names:
            return False
        seq = [names] if isinstance(names, str) else list(names)
        for n in seq:
            if self._inst is not None and callable(getattr(self._inst, n, None)):
                return True
            if callable(self._resolve_module_func(n)):
                return True
        return False

    def _resolve_module_func(self, func_name: str) -> Any:
        """在 spec.factory 所属模块里找同名模块级函数（找不到返回 None）。"""
        try:
            mod_path = self.spec.factory.partition(":")[0]
            mod = importlib.import_module(mod_path)
            fn = getattr(mod, func_name, None)
            return fn if callable(fn) else None
        except Exception:  # noqa: BLE001
            return None

    async def persist(self) -> bool:
        """落盘。仅当有 save 方法时调用（同步/协程自适应）。"""
        ok = await self._acall_first(self.spec.save)
        if ok:
            self._dirty = False
        return ok

    async def close(self) -> bool:
        """关闭。仅当有 close 方法时调用（同步/协程自适应）。"""
        return await self._acall_first(self.spec.close)

    def mark_dirty(self) -> None:
        self._dirty = True

    # -- 可观测 ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "factory": self.spec.factory,
            "kind": self.spec.kind,
            "instantiated": self._inst is not None,
            "dirty": self._dirty,
            # 区分「spec 声明了」与「实例真有」：两者不一致说明 spec 写错了
            "declared_save": bool(self.spec.save),
            "declared_close": bool(self.spec.close),
            "has_save": self.has_save(),
            "has_close": self.has_close(),
            "error": self._error,
            "age_sec": round(time.time() - self._created_at, 1) if self._created_at else 0.0,
            "note": self.spec.note,
            # 🔴 2026-10-28：**内容摘要**（12 个模块没有 get_status，从这里补）
            "live": self.live_summary(),
        }

    #: 常见容器字段名 → 计数（用于给「没有状态接口」的模块补一个摘要）。
    #:
    #: 33 个模块里 **12 个完全没有** `get_status()` / `status()` / `get_state()`：
    #:     relation scar quirk time_sense desire intent_generator debt
    #:     chat_memory learning tool_learning reply_quality forgetting
    #: ⇒ `/api/modules/status` 只能说「instantiated=true」，
    #:   不能回答「它现在装了多少东西」。
    #:
    #: 与其改 12 个文件（每个都要设计一套返回结构、加测试），
    #: 不如在这里**按属性名惯例**扫一遍容器大小 —— 只读、零侵入。
    #: 这不是「统一接口」，是「在统一接口缺席时给一个最小可观测面」。
    _COUNTABLE = ("_relations", "_scars", "_desires", "_items", "_entries",
                  "_notes", "_store", "_last_interaction", "_interaction_count",
                  "_records", "_pairs", "_groups")

    def live_summary(self) -> dict[str, Any]:
        """实例内容摘要（只读，零侵入，绝不抛）。

        先找模块自己的 `get_status()`；没有就按属性名扫容器大小。
        两者都拿不到就返回 `{}`（不伪造）。
        """
        if self._inst is None:
            return {}
        # ① 模块自带的统一接口优先
        for m in ("get_status", "status", "get_state", "snapshot"):
            fn = getattr(self._inst, m, None)
            if callable(fn):
                try:
                    got = fn()
                    if isinstance(got, dict):
                        return {"source": m, **got}
                    return {"source": m, "value": str(got)[:120]}
                except Exception as exc:  # noqa: BLE001
                    return {"source": m, "error": f"{type(exc).__name__}: {exc}"[:80]}
        # 🔴 2026-10-28 二版：**改成通用扫描**，不再猜属性名。
        #
        # 首版写死了 12 个候选名（`_relations`/`_scars`/...），实测只覆盖 3 个 ——
        # 真实属性名差异极大：
        #     DebtManager._debts        Learning._window_events
        #     SocialGraph._edges        ToolLearning._data
        #     TimeSense.last_interaction  DesireSystem.desires   ← **公有属性**
        # 猜名字的成本高于收益。改成扫所有容器属性（**公有和私有都扫**）。
        #
        # 🔴 2026-10-28 三版：前两版都只扫 `_` 开头，漏掉了
        #    `TimeSense.last_interaction` / `DesireSystem.desires`
        #    这两个**公有**容器 —— 结果 time_sense 和 desire 一直是空摘要。
        #    公开属性恰恰是「这个对象装着什么」最主要的信息来源。
        out: dict[str, Any] = {"source": "attrs"}
        _skip = {"_persist", "_core", "_path", "_data_dir", "_config", "_lock",
                 "persist", "core", "config"}  # 名字像基础设施的不报
        # 名字像「常量表」的不报（全大写或含 BAD/GOOD/KEYWORDS 之类）
        import re as _re
        _const_like = _re.compile(r"^_?[A-Z0-9_]+$|BAD|GOOD|KEYWORD|PHRASE|PATTERN|RE$|_RE$")
        try:
            # 先公有后私有：公有属性更能代表「这个对象的状态」
            _attrs = [a for a in dir(self._inst) if not a.startswith("_")]
            _attrs += [a for a in dir(self._inst)
                       if a.startswith("_") and not a.startswith("__")]
            for attr in _attrs:
                if attr in _skip or _const_like.search(attr):
                    continue
                # 类方法/静态方法跳过（只要数据）
                if callable(getattr(type(self._inst), attr, None)):
                    continue
                try:
                    v = getattr(self._inst, attr)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(v, str) or not hasattr(v, "__len__"):
                    continue
                try:
                    n = len(v)
                except Exception:  # noqa: BLE001
                    continue
                key = attr.lstrip("_")
                out[key] = n
                if len(out) >= 7:      # 够看了，别把整份 __dict__ 倒出来
                    break
        except Exception:  # noqa: BLE001
            pass
        return out if len(out) > 1 else {}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

class ModuleRegistry:
    """全部模块的登记与生命周期管理。

    与 `AnimaCore.register()` 的区别：
    - `AnimaCore.modules` 只装真正继承 `Module` 的（实测 3 个，都是基础设施）
    - 本注册表装**业务模块**（34 个单例），靠 `ModuleAdapter` 适配

    两者互补，不冲突。
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self._adapters: dict[str, ModuleAdapter] = {}
        self._order: list[str] = []
        self._started = False

    # -- 登记 --------------------------------------------------------------

    def register_spec(self, spec: ModuleSpec) -> None:
        """登记一个模块声明。同名重复登记时后者覆盖（便于测试替换）。"""
        if spec.name in self._adapters:
            logger.debug("[registry] 重复登记 %s，已覆盖", spec.name)
        else:
            self._order.append(spec.name)
        self._adapters[spec.name] = ModuleAdapter(spec)

    def register_all(self, specs: Iterable[ModuleSpec]) -> int:
        n = 0
        for s in specs:
            self.register_spec(s)
            n += 1
        logger.info("[registry] 已登记 %d 个模块", n)
        return n

    # -- 生命周期 ----------------------------------------------------------

    async def startup(self, core: Any = None) -> dict[str, int]:
        """逐个构造并加载。**fail-soft**：单个失败不影响其它。

        返回 `{"ok": n, "failed": n}`。
        """
        ok = failed = 0
        for name in self._order:
            ad = self._adapters[name]
            inst = ad.construct(data_dir=self.data_dir, core=core)
            if inst is None:
                failed += 1
                continue
            # 有 restore/load 就调（现有模块里多数在工厂内部已 load，这里幂等）
            await ad._acall_first(["restore", "load", "_load"])
            ok += 1
        self._started = True
        logger.info("[registry] 启动完成：ok=%d failed=%d", ok, failed)
        return {"ok": ok, "failed": failed}

    async def persist_all(self, *, only_dirty: bool = False) -> int:
        """批量落盘（所有声明了 save 且实例存在的模块）。返回成功个数。

        🔴 2026-10-28 修正：模块头注释原写「仅 dirty 的」，但实现从来没
        看过 `_dirty` —— 而 `mark_dirty()` 全仓 **0 个调用点**，
        所以按 dirty 过滤等于**一个都不写**。现在把语义写明：

        - 默认 `only_dirty=False`：把所有能落盘的都写一遍（幂等 JSON 写）；
        - `only_dirty=True`：只写标记为 dirty 的（供高频 tick 用，
          配合「构造即 dirty」，避免每轮把全部 26 个模块重写一遍）。

        注：本方法当前**无生产调用点**（退出路径走
        `AnimaCore._persist_cognitive_state()`），保留给运维/测试显式触发。
        """
        n = 0
        for name in self._order:
            ad = self._adapters[name]
            if ad._inst is None or not ad.has_save():
                continue
            if only_dirty and not ad._dirty:
                continue
            if await ad.persist():
                n += 1
        return n

    async def shutdown(self) -> dict[str, int]:
        """逐个关闭。**fail-soft**：单个失败不影响其它，也不阻断进程退出。

        这是本文件存在的**主要理由** —— 此前 `AnimaCore.shutdown` 手写
        7 段 `close_xxx()`，漏一个就是资源泄漏。

        ## 统计口径（刻意区分「无此能力」与「调用失败」）

        - `ok`：真的调用了 close 且成功
        - `failed`：**声明了** close、实例也**确实有**该方法，但调用抛异常
        - 既没声明、或声明了但实例没有 ⇒ **两边都不计**（不是错误）

        第二类很容易搞混：34 个模块里只有 8 个真有 close，
        其余 26 个「没 close」是**正常状态**（纯计算/无外部资源），
        把它们算进 failed 会让日志出现 26 个假警报。
        """
        ok = failed = 0
        # 逆序关闭（与构造相反，先关后依赖的）
        for name in reversed(self._order):
            ad = self._adapters[name]
            if ad._inst is None:
                continue
            # 关之前先尽力落盘（有 save 的）。
            # ⚠️ 只对**尚未落盘**的做：`persist_all()` 已落过的跳过，
            #    避免同一次 shutdown 里重复写盘（实测 save 会被调两次）。
            if ad._dirty:
                await ad.persist()
            if not ad.has_close():
                # 无关闭能力：不计 ok 也不计 failed
                continue
            if await ad.close():
                ok += 1
            else:
                failed += 1
            # 进程级单例：只关不置空（下次 get 复用同一实例）
            if not ad.spec.process_singleton:
                ad._inst = None
        self._started = False
        logger.info("[registry] 关闭完成：ok=%d failed=%d", ok, failed)
        return {"ok": ok, "failed": failed}

    # -- 取用与观测 --------------------------------------------------------

    def by_name(self, name: str) -> Any:
        """按名取实例（未构造则就地构造）。"""
        ad = self._adapters.get(name)
        if ad is None:
            return None
        return ad._inst if ad._inst is not None else ad.construct(
            data_dir=self.data_dir)

    def names(self) -> list[str]:
        return list(self._order)

    def status(self) -> list[dict[str, Any]]:
        """全量状态快照（供 `/api/modules/status` 与调试）。"""
        return [self._adapters[n].status() for n in self._order]

    def summary(self) -> dict[str, Any]:
        """汇总快照。

        ## 为什么 `declared_*` 与 `with_*` 都给

        实例方法形态的 save/close（如 `XBrowserSession.close`）**未构造时探测不到**，
        所以「未 startup 就查面板」会看到 `with_close=7` 而不是 8。
        那不是缺陷，但会误导 —— 因此同时给出 spec 声明的数量，
        两者不一致时一眼能看出「是没构造，还是 spec 写错了」。
        """
        st = self.status()
        return {
            "total": len(st),
            "instantiated": sum(1 for s in st if s["instantiated"]),
            # 声明数（来自 spec，与是否构造无关）
            "declared_save": sum(1 for s in st if s["declared_save"]),
            "declared_close": sum(1 for s in st if s["declared_close"]),
            # 实例真实能力（未构造的实例方法探测不到）
            "with_save": sum(1 for s in st if s["has_save"]),
            "with_close": sum(1 for s in st if s["has_close"]),
            "errors": [s for s in st if s["error"]],
            "started": self._started,
        }


# ---------------------------------------------------------------------------
# 默认登记表 —— 34 个业务模块集中在此（原来散在 anima_core 的手写 close 里）
# ---------------------------------------------------------------------------

def default_specs() -> list[ModuleSpec]:
    """项目全部业务模块的声明式登记。

    ## 这张表是怎么来的

    对 `anima/` 全量扫描 45 个模块级 `get_xxx` 工厂，剔除 11 个非单例
    （`get_status` / `get_top_k` 这类查询方法），得 34 个候选。

    **每个 `save` / `close` 方法名都是运行时实例探测出来的，不是猜的。**
    探测结果暴露了命名混乱的严重程度：

        save 类：`save` / `persist` / `_save` / `_save_state` / `_save_decay_state`
                 / `flush` / `flush_deferred_if_any`   ← 七种写法
        close 类：`close` / `_persist_daily` / `flush` / 模块级 `close_xxx_manager`

    这也是本注册表存在的意义之一：**把这些差异收敛在数据里**，
    上层代码只调 `registry.shutdown()`，不必知道每个模块叫什么。

    ## 维护约定

    新增业务模块时**只需在这里加一条**（用探测到的真实方法名），
    然后 `registry.startup()` 会自动构造、`shutdown()` 会自动落盘并关闭。
    不再需要改 `anima_core.py` 里那五行手写 `close_xxx()`。
    """
    return [
        # ── 情绪 / 关系 / 伤痕 ───────────────────────────────────────
        ModuleSpec("mood", "anima.cognitive.mood:get_mood_system",
                   kind=KIND_DATA_DIR, save=["_save"], close=None,
                   note="情绪价态/唤醒度，衰减与触发"),
        ModuleSpec("relation", "anima.cognitive.relation:get_relation",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="关系引擎（亲密度/信任/阶段）"),
        ModuleSpec("scar", "anima.cognitive.scar:get_scar_system",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="伤痕系统（被伤害事件累积）"),
        ModuleSpec("soul", "anima.cognitive.soul:get_soul_system",
                   kind=KIND_DATA_DIR, save=["save"], close=None,
                   note="灵魂参数（temperature/max_tokens 调制）"),
        ModuleSpec("quirk", "anima.cognitive.quirk:get_quirk_system",
                   kind=KIND_NOARG, save=None, close=None,
                   note="怪癖/随机小行为（纯计算，无状态）"),
        ModuleSpec("time_sense", "anima.cognitive.time_sense:get_time_sense",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="时间感（久别重逢/时段）"),

        # ── 欲望 / 意图 / 承诺 ───────────────────────────────────────
        ModuleSpec("desire", "anima.cognitive.desire:get_desire_system",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="欲望系统（衰减状态曾损坏过一次）"),
        ModuleSpec("intent_generator", "anima.cognitive.intent_generator:get_intent_generator",
                   kind=KIND_NOARG, save=None, close=None,
                   note="意图生成（无持久状态）"),
        ModuleSpec("debt", "anima.cognitive.debt:get_debt_manager",
                   kind=KIND_DATA_DIR, save=["save"], close=None,
                   note="承诺/欠账（due 触发主动跟进）"),

        # ── 记忆 / 学习 ──────────────────────────────────────────────
        ModuleSpec("chat_memory", "anima.memory.chat_memory:get_chat_memory",
                   kind=KIND_CUSTOM, kwargs={}, save=["save"], close=None,
                   note="短期对话记忆（轮边界双闸门）"),
        ModuleSpec("learning", "anima.cognitive.learning:get_learning",
                   kind=KIND_DATA_DIR, save=["_save_state"], close=None,
                   note="自学习笔记（漂移窗口限流）"),
        ModuleSpec("tool_learning", "anima.cognitive.tool_learning:get_tool_learning",
                   kind=KIND_DATA_DIR, save=["_save"],
                   close=["close_tool_learning"],
                   note="工具使用规律总结"),
        ModuleSpec("reply_learning", "anima.cognitive.reply_learning:get_reply_learning",
                   kind=KIND_DATA_DIR, save=["_save"], close=None,
                   note="被动回复效果自学习"),
        ModuleSpec("reply_quality", "anima.cognitive.reply_quality:get_reply_quality",
                   kind=KIND_DATA_DIR, save=["_save"],
                   close=["close", "close_reply_quality"],
                   note="回复质量统计"),
        ModuleSpec("forgetting", "anima.cognitive.forgetting:get_forgetting",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="遗忘曲线（记忆衰减）"),
        ModuleSpec("world_model", "anima.cognitive.world_model:get_world_model",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="世界观（对世界的稳定认知）"),

        # ── 社交 / 群组 ──────────────────────────────────────────────
        ModuleSpec("group_presence", "anima.cognitive.group_presence:get_group_presence",
                   kind=KIND_DATA_DIR, save=["_save"], close=None,
                   note="群在场状态（谁在说话/氛围）"),
        ModuleSpec("social_graph", "anima.cognitive.social_graph:get_social_graph",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="社交图（群内亲近对）"),
        ModuleSpec("social_reactions", "anima.cognitive.social_reactions:get_social_reactions",
                   kind=KIND_CORE, save=None, close=None,
                   note="社交反应（被动观察，无独立持久）"),
        ModuleSpec("known_bots", "anima.cognitive.bot_social:get_known_bots_store",
                   kind=KIND_DATA_DIR, save=["save"], close=None,
                   note="已知 bot 注册表（评分制自动确认）"),
        ModuleSpec("crossgroup", "anima.crossgroup.manager_singleton:get_crossgroup_manager",
                   kind=KIND_NOARG, save=["flush"],
                   close=["close_crossgroup_manager"],
                   note="跨群记忆抽取"),

        # ── 能力 / 自我改造 ──────────────────────────────────────────
        ModuleSpec("capabilities", "anima.cognitive.capabilities:get_capability_manager",
                   kind=KIND_DATA_DIR, save=["_save"],
                   close=["close_capability_manager"],
                   note="内生能力（动态工具注册）"),
        ModuleSpec("selfcode", "anima.cognitive.selfcode:get_selfcode_manager",
                   kind=KIND_CORE, save=None,
                   close=["close_selfcode_manager"],
                   note="SelfCode（生成/审查/安装工具）"),
        ModuleSpec("controlled_iteration", "anima.cognitive.controlled_iteration:get_controlled_iteration",
                   kind=KIND_DATA_DIR, save=["_save"], close=None,
                   note="受控自我迭代（补丁审批）"),
        ModuleSpec("evolution_loop", "anima.agent.self_evolution:get_evolution_loop",
                   kind=KIND_CUSTOM, kwargs={}, save=None, close=None,
                   note="进化循环（提案/审批）"),

        # ── 探索 / 表达 ──────────────────────────────────────────────
        # ⚠️ exploration 必须收 core（内部要读 core.config），不能用 data_dir
        ModuleSpec("exploration", "anima.cognitive.exploration:get_exploration_manager",
                   kind=KIND_CORE, save=["_save"], close=None,
                   note="外部探索（B站/X/维基 → 群分享）"),
        # ⚠️ stickers 的 data_dir 是**必填位置参数**，且要 config
        ModuleSpec("stickers", "anima.cognitive.stickers:get_sticker_manager",
                   kind=KIND_CUSTOM,
                   kwargs={"needs_data_dir": True}, save=None,
                   close=["close_sticker_manager"],
                   note="表情包库（收集/选用）"),
        ModuleSpec("vision", "anima.cognitive.vision:get_vision_manager",
                   kind=KIND_CUSTOM, kwargs={}, save=None,
                   close=["close_vision_manager"],
                   note="视觉理解（图片描述）"),
        ModuleSpec("x_browser", "anima.cognitive.x_browser:get_x_browser",
                   kind=KIND_CORE, save=["_persist_stats"], close=["close"],
                   note="X/Twitter 浏览器会话（cookie 注入）"),

        # ── 危险行为 / 模拟人生 ──────────────────────────────────────
        ModuleSpec("danger", "anima.cognitive.danger:get_danger_behaviors",
                   kind=KIND_CORE, save=["_save"], close=None,
                   note="危险行为（主动追问/关系推断）"),
        ModuleSpec("life", "anima.cognitive.simulated_life:get_life",
                   kind=KIND_CUSTOM,
                   kwargs={"needs_core": True, "needs_data_dir": True},
                   save=["_save_state"], close=None,
                   note="模拟人生（里程碑/日常）"),
        ModuleSpec("milestone", "anima.cognitive.simulated_life:get_milestone_tracker",
                   kind=KIND_DATA_DIR, save=["persist"], close=None,
                   note="里程碑追踪"),

        # ── 2026-10-28 新增：owner 私聊通道（D-41 扩展）─────────────
        # ⚠️ 这个模块的工厂收 core（不是 data_dir）。
        # 🔴 2026-10-28 修正：原注释写「没有 save 方法」是**错的** ——
        #    `OwnerNotifier._save()` 真实存在（写 `owner_notify_state.json`）。
        #    漏登记它的后果：`normal` 队列里还没投递的消息在重启时**静默丢失**，
        #    而队列本来就是「重启后要接着发」的语义，丢了等于没实现。
        ModuleSpec("owner_notify", "anima.core.owner_notify:get_owner_notifier",
                   kind=KIND_CORE, save=["_save"], close=None,
                   note="owner 私聊通道（urgent 即时 / normal 队列 / digest 归 autonomy）"),
    ]


# ---------------------------------------------------------------------------
# 全局单例（与既有 get_xxx 风格一致，避免再引入新的 DI 框架）
# ---------------------------------------------------------------------------

_registry: ModuleRegistry | None = None


def get_registry(data_dir: str = "data") -> ModuleRegistry:
    """取进程级注册表（首次调用时创建并登记全部模块）。"""
    global _registry
    if _registry is None:
        _registry = ModuleRegistry(data_dir=data_dir)
        _registry.register_all(default_specs())
    return _registry


def reset_registry() -> None:
    """测试用：清空全局注册表。"""
    global _registry
    _registry = None
