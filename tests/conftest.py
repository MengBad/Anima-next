"""测试公共 fixture。"""

import pytest

# 归档测试（tests/_archive/）不参与收集。
# 注意：pytest 默认会递归 `_` 前缀目录（9.x 实测如此），必须显式忽略。
collect_ignore_glob = ["_archive"]


@pytest.fixture
def crossgroup_tmp(monkeypatch, tmp_path):
    """隔离的 crossgroup 单例 + 临时数据目录。

    每个 test 自带干净的单例与 data_dir，互不污染；test 结束后还原。
    """
    import anima.crossgroup.manager_singleton as ms

    ms._instance = None
    d = tmp_path / "cg"
    monkeypatch.setenv("ANIMA_CROSSGROUP_DIR", str(d))
    mgr = ms.get_crossgroup_manager()
    yield mgr
    ms._instance = None


@pytest.fixture(autouse=True)
def _reset_data_dir_ignoring_singletons():
    """逐测重置**忽略 data_dir 的全局单例**，消除按文件顺序的隐性耦合。

    只收录**已证**「构造后忽略 data_dir」的单例；不做全量单例清理
    （另 39 处单例中多数被测试有意共享，盲清会破坏它们）。

    - `anima.cognitive.world_model._world_model`
      （`world_model.py:291-295`；本轮回归 1 failed 的直接成因，见模块 docstring）
    """
    import anima.cognitive.world_model as _wm

    saved = _wm._world_model
    _wm._world_model = None
    yield
    _wm._world_model = saved


@pytest.fixture(autouse=True)
def _isolate_x_browser_quota(tmp_path, monkeypatch):
    """把落在**仓库 ``data/``** 下的 X 额度计数文件重定向到 ``tmp_path``。

    D-20 起额度计数落盘（生产必需），而既有测试用仓库 ``data/x_browser`` 当 data_dir
    ⇒ 计数跨测试进程累积，会把 ``search_daily_limit=10`` 跑满并让后续整批测试
    静默快速失败（2026-09-18 实测：同一命令第二次运行 10 failed）。

    只重定向**落在仓库 data/ 下**的路径：用 ``tmp_path`` 构造的测试（如
    ``test_x_quota_persistence.py``）不受影响，仍能验真实落盘语义。
    """
    import pathlib

    import anima.cognitive.x_browser as xb

    repo_data = (pathlib.Path.cwd() / "data").resolve()
    original = xb.XBrowserSession._quota_path

    def _redirected(self):
        path = original(self)
        try:
            resolved = pathlib.Path(path).resolve()
        except OSError:
            return path
        if repo_data in resolved.parents:
            return tmp_path / "x_daily_quota.json"
        return path

    monkeypatch.setattr(xb.XBrowserSession, "_quota_path", _redirected)


@pytest.fixture(autouse=True)
def _isolate_mood_data_dir(tmp_path, monkeypatch):
    """把落在**仓库 ``data/``** 下的 mood 落盘重定向到 ``tmp_path``。

    `anima/cognitive/mood.py:307` 的 `get_mood_system(data_dir)` 是**全局单例**：
    首次构造后**忽略 data_dir**（与上方收录的 `world_model._world_model` 同类）。
    而 `guardrail_stage` 的 C1「情绪→回复长度约束」在 `ctx.meta["core"]` 缺失时
    **回落硬编码 ``"data"``**（`guardrail.py:806-810`）⇒ 测试期命中**仓库**
    ``data/mood.json``。

    后果（2026-10-07 实测）：某测试把 mood 写成 ``tired`` 并落盘到仓库 ⇒ C1 把 277 字
    正常回复截到 180 ⇒
    ``test_guardrail_analysis`` / ``test_guardrail_false_kill_fix`` /
    ``test_guardrail_truncation_boundary`` / ``test_sticker_outbound``
    **4 条稳定假失败**（两次对照实验证明与代码回归无关：stash 掉改动仍失败；
    ``mood.json`` tired→neutral 后 91 条全通过）。

    只重定向「落到仓库 ``data/``」的请求：用 ``tmp_path`` 构造的测试
    （如 ``test_mood_decay_not_starved.py``）不受影响，仍能验证真实落盘语义。
    """
    import pathlib

    import anima.cognitive.mood as mood_mod

    repo_data = (pathlib.Path.cwd() / "data").resolve()
    saved_singleton = mood_mod._mood_system
    saved_default = mood_mod._default_dir
    orig_get = mood_mod.get_mood_system

    # 🔴 逐测重置单例：否则**首个**测试的 data_dir 会粘住整轮（单例忽略后续参数），
    #    只重置/patch 一个都不够 —— 两者缺一，仍会有测试绑到仓库 data/。
    mood_mod._mood_system = None

    def _sandboxed(data_dir=None):
        raw = data_dir if data_dir is not None else mood_mod._default_dir
        try:
            resolved = pathlib.Path(raw).resolve()
        except OSError:
            resolved = None
        if resolved is not None and (resolved == repo_data or repo_data in resolved.parents):
            return orig_get(str(tmp_path / "mood"))
        return orig_get(data_dir)

    monkeypatch.setattr(mood_mod, "get_mood_system", _sandboxed)
    yield
    mood_mod._mood_system = saved_singleton
    mood_mod._default_dir = saved_default
