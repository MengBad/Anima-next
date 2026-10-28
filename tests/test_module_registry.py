"""统一模块注册表（2026-10-28）。

## 问题：34 个单例，23 个没有生命周期

`anima/cognitive/` 下 71 个文件、34 个模块级 `get_xxx` 工厂。实测：

    · 34 个里 **23 个没有 close/shutdown**
    · save 有七种写法：`save` / `persist` / `_save` / `_save_state`
      / `_save_decay_state` / `flush` / `flush_deferred_if_any`
    · close 有四种：`close` / `_persist_daily` / 模块级 `close_xxx_manager` / 无
    · 签名四种：8 个收 `core`、25 个收 `data_dir`、8 个无参、4 个其它

后果已在生产暴露：**加新子系统若忘了在 `AnimaCore.shutdown` 手写一行
`close_xxx()`，就是资源泄漏** —— `desire_decay_state.json` 那次损坏
（37 字节残文件）就是这么来的。

`core/module.py` 的 `CognitiveModule` 契约写得很好，文档说
「所有 anima/cognitive/ 下的子系统应继承此类」——
**但实测 0 个类继承它，`register_service()` 全库零调用**。契约是空壳。

## 解法：不改现有模块，只加一层适配

`ModuleRegistry` + `ModuleSpec` 把「工厂函数」映射成「Module 协议」。
34 个单例继续按原样工作（内部缓存/锁/TTL 全保留），
注册表负责：统一构造、统一落盘、统一关闭、统一观测。
"""
from __future__ import annotations

import asyncio
import warnings

import pytest

from anima.core.config import load_config
from anima.core.registry import (
    KIND_CORE,
    KIND_CUSTOM,
    KIND_DATA_DIR,
    KIND_NOARG,
    ModuleAdapter,
    ModuleRegistry,
    ModuleSpec,
    default_specs,
    get_registry,
    reset_registry,
)


# ── 测试替身 ────────────────────────────────────────────────────────

class _Obj:
    """可控的假模块（验证 save/close 调用）。"""

    def __init__(self):
        self.saved = 0
        self.closed = 0

    def save(self):
        self.saved += 1

    def close(self):
        self.closed += 1


class _AsyncObj:
    """协程版：验证 async save/close 能被正确 await。"""

    def __init__(self):
        self.saved = 0
        self.closed = 0

    async def save(self):
        self.saved += 1

    async def close(self):
        self.closed += 1


class _FakeCore:
    def __init__(self, data_dir):
        self.config = load_config("config/default.yaml")
        self.config.persistence.data_dir = data_dir


@pytest.fixture()
def tmp_core(tmp_path):
    return _FakeCore(str(tmp_path))


# ── 1. 基本生命周期 ─────────────────────────────────────────────────

def test_register_and_lifecycle(tmp_path):
    reg = ModuleRegistry(data_dir=str(tmp_path))
    inst = _Obj()
    reg.register_spec(ModuleSpec("x", "__main__:noop", save=["save"], close=["close"]))
    # 手工塞实例，绕过 factory 解析
    reg._adapters["x"]._inst = inst

    assert asyncio.run(reg.persist_all()) == 1
    assert inst.saved == 1
    assert asyncio.run(reg.shutdown())["ok"] == 1
    assert inst.closed == 1


def test_async_save_and_close_are_awaited(tmp_path):
    """协程版 save/close 必须被 await，不能变成「未 await 的协程」。

    生产实测：`XBrowserSession.close` 是 `async def`，
    同步调用只得到 RuntimeWarning 且**实际什么都没执行**（会话泄漏）。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    inst = _AsyncObj()
    reg.register_spec(ModuleSpec("x", "__main__:noop", save=["save"], close=["close"]))
    reg._adapters["x"]._inst = inst

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert asyncio.run(reg.persist_all()) == 1
        asyncio.run(reg.shutdown())

    assert inst.saved == 1, f"async save 没被执行或重复执行（{inst.saved}）"
    assert inst.closed == 1, "async close 没被执行"


def test_missing_method_is_not_an_error(tmp_path):
    """spec 声明了 save 但实例没有 ⇒ 不算失败，只是没做。"""
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec("x", "__main__:noop", save=["save"], close=["close"]))
    reg._adapters["x"]._inst = object()      # 没有任何方法
    assert asyncio.run(reg.persist_all()) == 0
    r = asyncio.run(reg.shutdown())
    assert r["failed"] == 0, "「无此能力」不该算失败"
    assert r["ok"] == 0


def test_construct_failure_is_isolated(tmp_path):
    """单个模块构造失败不影响其它（fail-soft）。"""
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec("bad", "no_such_module_xyz:fn", kind=KIND_DATA_DIR))
    reg.register_spec(ModuleSpec("good", "anima.cognitive.mood:get_mood_system",
                                 kind=KIND_DATA_DIR, save=["_save"]))
    r = asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    assert r["failed"] == 1 and r["ok"] == 1
    assert reg.by_name("bad") is None
    assert reg.by_name("good") is not None


def test_shutdown_reverse_order(tmp_path):
    """关闭顺序必须与构造相反（先关后依赖的）。"""
    order = []

    class A:
        def close(self):
            order.append("A")

    class B:
        def close(self):
            order.append("B")

    reg = ModuleRegistry(data_dir=str(tmp_path))
    for name, inst in (("a", A()), ("b", B())):
        reg.register_spec(ModuleSpec(name, "__main__:noop", close=["close"]))
        reg._adapters[name]._inst = inst
    asyncio.run(reg.shutdown())
    assert order == ["B", "A"], f"关闭顺序错了：{order}"


# ── 2. 四种工厂签名 ─────────────────────────────────────────────────

def test_kind_data_dir(tmp_path):
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec("mood", "anima.cognitive.mood:get_mood_system",
                                 kind=KIND_DATA_DIR))
    assert reg.by_name("mood") is not None


def test_kind_core_requires_core(tmp_path):
    """KIND_CORE 在 core=None 时**延迟**而不是塞假 core。"""
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec("expl", "anima.cognitive.exploration:get_exploration_manager",
                                 kind=KIND_CORE))
    ad = reg._adapters["expl"]
    assert ad.construct(data_dir=str(tmp_path), core=None) is None
    assert "deferred" in ad._error, f"应标记 deferred，实得 {ad._error!r}"
    # 传真实 core 后可构造
    assert ad.construct(data_dir=str(tmp_path), core=_FakeCore(str(tmp_path))) is not None


def test_kind_noarg(tmp_path):
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec("q", "anima.cognitive.quirk:get_quirk_system",
                                 kind=KIND_NOARG))
    assert reg.by_name("q") is not None


def test_kind_custom_injects_runtime_args(tmp_path):
    """KIND_CUSTOM 的 needs_core / needs_data_dir 由注册表注入。"""
    seen = {}

    def factory(core=None, data_dir=None):
        seen["core"] = core
        seen["data_dir"] = data_dir
        return _Obj()

    import anima.cognitive.quirk as _probe_mod
    _probe_mod._registry_probe = factory
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec(
        "p", "anima.cognitive.quirk:_registry_probe", kind=KIND_CUSTOM,
        kwargs={"needs_core": True, "needs_data_dir": True}))
    core = _FakeCore(str(tmp_path))
    ad = reg._adapters["p"]
    assert ad.construct(data_dir=str(tmp_path), core=core) is not None, ad._error
    assert seen["core"] is core and seen["data_dir"] == str(tmp_path)


# ── 3. 真实 34 个模块全量 ───────────────────────────────────────────

def test_all_default_specs_construct(tmp_path):
    """全部默认模块必须能构造成功（用真实 config）。

    这条是回归守卫：任何 factory 路径写错、或某个模块新增了必填参数，
    都会在这里炸。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    core = _FakeCore(str(tmp_path))
    r = asyncio.run(reg.startup(core))
    assert r["failed"] == 0, f"有模块构造失败：{[s for s in reg.status() if s['error']]}"
    assert r["ok"] == len(default_specs())


def test_all_default_specs_persist_and_shutdown(tmp_path):
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    core = _FakeCore(str(tmp_path))
    asyncio.run(reg.startup(core))
    assert asyncio.run(reg.persist_all()) > 0
    r = asyncio.run(reg.shutdown())
    assert r["failed"] == 0


def test_no_duplicate_module_names():
    specs = default_specs()
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), f"重名：{[n for n in names if names.count(n)>1]}"


def test_every_spec_has_note_and_factory():
    for s in default_specs():
        assert s.factory.count(":") == 1, f"{s.name} 的 factory 格式错：{s.factory}"
        assert s.note, f"{s.name} 缺 note（WebUI 要显示用途）"
        assert s.kind in (KIND_DATA_DIR, KIND_CORE, KIND_NOARG, KIND_CUSTOM)


# ── 4. 可观测 ───────────────────────────────────────────────────────

def test_status_reports_capabilities(tmp_path):
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    st = reg.status()
    assert len(st) == len(default_specs())
    for row in st:
        assert set(row) >= {"name", "instantiated", "has_save", "has_close",
                            "declared_save", "declared_close", "error"}
    s = reg.summary()
    assert s["total"] == len(default_specs())
    assert s["instantiated"] == s["total"]
    assert s["with_save"] > 0 and s["with_close"] > 0


def test_by_name_constructs_lazily(tmp_path):
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec("mood", "anima.cognitive.mood:get_mood_system",
                                 kind=KIND_DATA_DIR))
    assert reg._adapters["mood"]._inst is None      # 尚未构造
    assert reg.by_name("mood") is not None
    assert reg._adapters["mood"]._inst is not None  # 惰性构造了


def test_global_registry_singleton():
    reset_registry()
    a = get_registry("data")
    b = get_registry("data")
    assert a is b, "全局注册表应为进程级单例"
    reset_registry()
    c = get_registry("data")
    assert c is not a, "reset 后应重建"


# ── 5. 反向守卫 ─────────────────────────────────────────────────────

def test_save_and_close_lists_try_in_order(tmp_path):
    """spec 给多个候选方法名时，按序取第一个存在的。"""

    class OnlyPersist:
        def __init__(self):
            self.n = 0

        def persist(self):
            self.n += 1

    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_spec(ModuleSpec("x", "__main__:noop",
                                 save=["save", "persist"], close=None))
    inst = OnlyPersist()
    reg._adapters["x"]._inst = inst
    assert asyncio.run(reg.persist_all()) == 1
    assert inst.n == 1, "没回退到 persist"


def test_method_exception_does_not_break_others(tmp_path):
    """一个模块 save 抛异常，不得影响其它模块落盘。"""

    class Boom:
        def save(self):
            raise RuntimeError("boom")

    class Fine:
        def __init__(self):
            self.n = 0

        def save(self):
            self.n += 1

    reg = ModuleRegistry(data_dir=str(tmp_path))
    for name, inst in (("boom", Boom()), ("fine", Fine())):
        reg.register_spec(ModuleSpec(name, "__main__:noop", save=["save"]))
        reg._adapters[name]._inst = inst
    n = asyncio.run(reg.persist_all())
    assert n == 1, f"应只有 fine 成功，实得 {n}"


# ── 6. 工具注册表统一可观测（与 ModuleRegistry.status 对齐）──────────

def test_tool_registry_observability():
    """ToolRegistry 必须能枚举工具（此前只有 register/unregister）。

    与模块层同源的问题：**能加，但看不见**。
    以前想知道「注册了哪些工具、各自什么权限」只能 grep 源码。
    """
    from anima.tools import ToolRegistry
    from anima.tools.builtin import register_all_builtin

    r = ToolRegistry()
    register_all_builtin(r, core=None)

    names = r.names()
    assert names, "没有任何工具"
    assert names == sorted(names), "names() 应排序（便于快照对比）"

    st = r.status()
    assert st["total"] == len(names)
    assert sum(st["by_level"].values()) == st["total"]
    assert isinstance(st["private_only"], int)

    for t in r.list_tools():
        assert set(t) >= {"name", "min_level", "priority", "params",
                          "private_only", "desc"}
        assert isinstance(t["params"], list)


def test_tool_registry_list_is_serializable():
    """`list_tools()` 结果必须可 JSON 序列化（WebUI 要用），不得含 handler。"""
    import json
    from anima.tools import ToolRegistry
    from anima.tools.builtin import register_all_builtin

    r = ToolRegistry()
    register_all_builtin(r, core=None)
    json.dumps(r.list_tools())      # 不可序列化会直接抛
    json.dumps(r.status())


def test_summary_distinguishes_declared_from_actual():
    """`summary()` 必须同时给「spec 声明数」与「实例真实能力数」。

    实例方法形态的 save/close（如 `XBrowserSession.close`）**未构造时探测不到**，
    所以「未 startup 就查面板」会看到 with_close 比 declared_close 少一个。
    那不是缺陷，但只给一个数会误导 —— 两个都给，一眼能区分
    「是没构造」还是「spec 写错了」。
    """
    reg = ModuleRegistry(data_dir="data")
    reg.register_all(default_specs())
    s = reg.summary()
    assert "declared_save" in s and "declared_close" in s
    assert s["declared_close"] >= s["with_close"], \
        "声明数不应少于已探测到的实例能力数"
    # 未构造时，实例方法形态探测不到 ⇒ with_* 可能小于 declared_*
    assert s["instantiated"] == 0


# ── 8. live_summary：给「没有状态接口」的模块补最小可观测面 ──────────

def test_live_summary_covers_majority(tmp_path):
    """33 个模块里至少 20 个要有活体摘要。

    背景：12 个模块**完全没有** `get_status()` / `status()` / `get_state()`：
        relation scar quirk time_sense desire intent_generator debt
        chat_memory learning tool_learning reply_quality forgetting
    ⇒ `/api/modules/status` 只能说「instantiated=true」，
      不能回答「它现在装了多少东西」。

    修法不是改 12 个文件，而是在 registry 层**扫所有容器属性**。
    首版写死了 12 个候选名（`_relations`/`_scars`/...），只覆盖 3 个 ——
    真实属性名差异极大（`_debts`/`_edges`/`_data`/`_window_events`）。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    st = reg.status()
    covered = [s for s in st if (s.get("live") or {}).get("source")]
    assert len(covered) >= 20, \
        f"只有 {len(covered)}/{len(st)} 有活体摘要：{[s['name'] for s in st if not (s.get('live') or {}).get('source')]}"


def test_live_summary_prefers_module_status_method(tmp_path):
    """模块自带 `get_status()` 时优先用它，不要用属性扫描糊弄。"""
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    by_name = {s["name"]: s for s in reg.status()}
    # reply_learning 有 snapshot()
    assert (by_name["reply_learning"].get("live") or {}).get("source") == "snapshot"
    # owner_notify 有 status()
    assert (by_name["owner_notify"].get("live") or {}).get("source") == "status"


def test_live_summary_never_fabricates(tmp_path):
    """拿不到就返回 `{}`，绝不编造数字。

    `quirk` / `forgetting` 这类模块确实没有容器属性 ——
    返回空比返回假数字好。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    for s in reg.status():
        live = s.get("live")
        assert isinstance(live, dict)
        # 有 source 就一定有内容（不能 source=attrs 但只有一个键）
        if live.get("source") == "attrs":
            assert len(live) > 1, f"{s['name']} 的 attrs 摘要为空"


# ── 9. 33/33 全覆盖（2026-10-28 补齐 6 个纯标量模块）──────────────

def test_all_modules_have_live_summary(tmp_path):
    """**33 个模块全都要有活体摘要**，不能有空白。

    补齐前有 6 个为空：
        quirk            只有 _today_count/_daily_max（标量，非容器）
        intent_generator 无状态（每次现场拼 prompt）
        forgetting       只有 _run_count/_threshold（标量）
        danger           只有时间戳标量 + 配置对象
        life             只有时间戳 + 计数（标量）
        evolution_loop   只有指针（_dev/_proposer/_gate）

    这些模块的属性扫描结果都是空的 ⇒ 面板上只有一个「instantiated=true」。
    修法：给它们各写一个 `get_status()`，报**有意义的标量**而非容器大小。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    st = reg.status()
    empty = [s["name"] for s in st if not (s.get("live") or {}).get("source")]
    assert not empty, f"以下模块没有活体摘要：{empty}"
    assert len(st) == 33


@pytest.mark.parametrize("name,expect_keys", [
    ("quirk", {"today_count", "daily_max", "remaining"}),
    ("forgetting", {"run_count", "threshold", "stale"}),
    ("intent_generator", {"stateless", "llm_ready"}),
    ("danger", {"enabled_switches", "identity_stability"}),
    ("life", {"flow_count", "stale"}),
    ("evolution_loop", {"has_gate", "auto_deploy_self_granted"}),
])
def test_scalar_modules_report_meaningful_fields(tmp_path, name, expect_keys):
    """这 6 个模块必须报**有意义的标量**，不能只报容器大小。"""
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    s = next(x for x in reg.status() if x["name"] == name)
    live = s.get("live") or {}
    assert live.get("source") == "get_status", \
        f"{name} 应走 get_status，实得 {live.get('source')}"
    missing = expect_keys - set(live)
    assert not missing, f"{name} 缺字段 {missing}（实得 {list(live)}）"


def test_danger_exposes_switch_state(tmp_path):
    """`danger` 必须把「哪个开关开着」摊出来。

    这个模块**默认全关**，面板上看到 instantiated=true 会误以为它在跑。
    显式报 `enabled_switches` 能一眼确认「它现在什么都没做」。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    s = next(x for x in reg.status() if x["name"] == "danger")
    sw = (s.get("live") or {}).get("enabled_switches")
    assert isinstance(sw, dict) and sw, "没报开关状态"
    assert all(isinstance(v, bool) for v in sw.values())


def test_evolution_loop_reports_no_self_grant(tmp_path):
    """`evolution_loop` 必须明确报「无法自我授权」。

    R9 的安全承诺是「能提提案，但永远无法自我授权」。
    光靠代码注释不够 —— 面板上要能直接读到。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    s = next(x for x in reg.status() if x["name"] == "evolution_loop")
    live = s.get("live") or {}
    assert live.get("auto_deploy_self_granted") is False
    assert live.get("has_gate") is True


def test_life_reports_staleness(tmp_path):
    """`life` 必须报「早安/晚安有没有按时跑」。

    这几个都是日更动作，超过 25 小时没跑就说明调度断了 ——
    光看 `instantiated=true` 发现不了这种「活着但不干活」。
    """
    reg = ModuleRegistry(data_dir=str(tmp_path))
    reg.register_all(default_specs())
    asyncio.run(reg.startup(_FakeCore(str(tmp_path))))
    s = next(x for x in reg.status() if x["name"] == "life")
    live = s.get("live") or {}
    assert "stale" in live and isinstance(live["stale"], bool)
    assert "flow_count" in live
