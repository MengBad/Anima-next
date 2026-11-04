"""通用浏览器 —— 持久化登录态 + 截图回传（2026-11-06）。

## 为什么

browser.py 原来每次 launch() + new_context()，跑完就丢 cookie jar，
华为云 / GitCode 这类「先登录、再操作」的任务永远做不成。改成
launch_persistent_context 后自带登录态，顺手把截图路径接进既有出站通道。

本文件全部用 fake，**不启动真浏览器**。
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

import anima.tools as tools_pkg
import anima.tools.browser as br


# ── fake playwright ───────────────────────────────────────────────────────


class _FakePage:
    def __init__(self, shot_dir: "pathlib.Path | None" = None):
        self.shot_dir = shot_dir
        self.screenshots: list[str] = []
        self.pressed: list[tuple] = []
        self.filled: list[tuple] = []

    async def evaluate(self, script: str):
        return "标题 正文"

    async def title(self):
        return "示例标题"

    async def goto(self, url, timeout=None, wait_until=None):
        return None

    async def press(self, selector, key, timeout=None):
        self.pressed.append((selector, key))

    async def screenshot(self, path: str, full_page: bool = True) -> None:
        self.screenshots.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG-fake")


class _FakeChromium:
    def __init__(self, page: _FakePage):
        self._page = page
        self.calls: list[dict] = []
        self.closed = 0

    async def launch_persistent_context(self, path, **kwargs):
        ctx = _FakeContext(path, self._page)
        self.calls.append({"path": path, **kwargs, "ctx": ctx})
        return ctx


class _FakeContext:
    def __init__(self, path, page: _FakePage):
        self.path = path
        self.pages: list[_FakePage] = [page]
        self.closed = 0

    async def new_page(self):
        return self.pages[0]

    async def close(self) -> None:
        self.closed += 1


class _FakePlaywright:
    def __init__(self, page: _FakePage):
        self.chromium = _FakeChromium(page)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _install_fake(monkeypatch, tmp_path, page: "_FakePage | None" = None) -> "_FakePlaywright":
    fake = _FakePlaywright(page or _FakePage())
    monkeypatch.setattr(br, "_playwright_factory", lambda: (lambda: fake))
    monkeypatch.setattr(br, "PROFILE_DIR", tmp_path / "browser_profile")
    monkeypatch.setattr(br, "SCREENSHOT_DIR", tmp_path / "screenshots")
    return fake


# ── (a) PROFILE_DIR 解析 ─────────────────────────────────────────────────


def test_profile_dir_follows_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANIMA_DATA_DIR", str(tmp_path))
    assert br._resolve_data_dir() == tmp_path
    # 模块级常量在 import 时定型；重算一次确认形状一致
    assert br._resolve_data_dir() / "browser_profile" == tmp_path / "browser_profile"


def test_profile_dir_falls_back_to_prod(monkeypatch, tmp_path):
    """没有环境变量 → 生产数据目录（存在时），其下 browser_profile。"""
    monkeypatch.delenv("ANIMA_DATA_DIR", raising=False)
    prod = tmp_path / "proddata"
    prod.mkdir()
    monkeypatch.setattr(br, "_PROD_DATA_DIR", prod)
    assert br._resolve_data_dir() == prod
    assert br._resolve_data_dir() / "browser_profile" == prod / "browser_profile"


def test_profile_dir_last_resort_is_relative_data(monkeypatch, tmp_path):
    monkeypatch.delenv("ANIMA_DATA_DIR", raising=False)
    monkeypatch.setattr(br, "_PROD_DATA_DIR", tmp_path / "nope")
    assert br._resolve_data_dir() == pathlib.Path("data")


def test_browser_unavailable_still_exported():
    """调用方 except BrowserUnavailable 依赖它 —— 重构时被吃掉过一次。"""
    assert issubclass(br.BrowserUnavailable, RuntimeError)


def test_module_exposes_required_constants():
    assert isinstance(br.PROFILE_DIR, pathlib.Path)
    assert br.PROFILE_DIR.name == "browser_profile"
    assert br.PROFILE_MAX_BYTES == 256 * 1024 * 1024
    assert "Cookies" in br._PROFILE_KEEP
    assert "Local State" in br._PROFILE_KEEP
    assert {"Cache", "Code Cache", "GPUCache", "Service Worker"} <= br._PROFILE_CACHE_DIRS


# ── (b) 同一 profile 串行 ────────────────────────────────────────────────


async def test_concurrent_sessions_do_not_overlap(monkeypatch, tmp_path):
    """两个并发调用不得同时持有 profile（Chromium singleton-lock）。"""
    active = 0
    max_active = 0
    page = _FakePage()
    fake = _FakePlaywright(page)
    original = fake.chromium.launch_persistent_context

    async def _tracked(path, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)   # 放大重叠窗口
            return await original(path, **kwargs)
        finally:
            active -= 1

    fake.chromium.launch_persistent_context = _tracked
    monkeypatch.setattr(br, "_playwright_factory", lambda: (lambda: fake))
    monkeypatch.setattr(br, "PROFILE_DIR", tmp_path / "browser_profile")

    async def _one():
        async with br._persistent_session() as ctx:
            await asyncio.sleep(0.02)
            return ctx

    contexts = await asyncio.gather(_one(), _one())
    assert max_active == 1, "两次 launch 重叠了，profile 会被 singleton-lock 拒绝"
    assert len(fake.chromium.calls) == 2
    assert all(c.closed == 1 for c in contexts)


async def test_lock_released_after_exception(monkeypatch, tmp_path):
    """launch 抛错也必须把锁还回来，否则后续所有抓取永久阻塞。"""
    page = _FakePage()
    fake = _FakePlaywright(page)
    boom = {"left": 1}
    original = fake.chromium.launch_persistent_context

    async def _flaky(path, **kwargs):
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("singleton-lock")
        return await original(path, **kwargs)

    fake.chromium.launch_persistent_context = _flaky
    monkeypatch.setattr(br, "_playwright_factory", lambda: (lambda: fake))
    monkeypatch.setattr(br, "PROFILE_DIR", tmp_path / "browser_profile")

    with pytest.raises(RuntimeError, match="singleton-lock"):
        async with br._persistent_session():
            pass  # pragma: no cover - 不会执行到
    assert not br._LAUNCH_LOCK.locked()

    async with br._persistent_session() as ctx:
        assert ctx is not None


async def test_persistent_context_is_used_and_profile_kept(monkeypatch, tmp_path):
    """必须走 launch_persistent_context，且结束时只关上下文、不删 profile。"""
    page = _FakePage()
    fake = _install_fake(monkeypatch, tmp_path, page)
    profile = tmp_path / "browser_profile"

    async with br._persistent_session() as ctx:
        assert profile.is_dir()
        assert ctx.path == str(profile)
        (profile / "Default").mkdir(parents=True, exist_ok=True)
        (profile / "Default" / "Cookies").write_bytes(b"cookie")

    assert len(fake.chromium.calls) == 1
    assert fake.chromium.calls[0]["path"] == str(profile)
    assert fake.chromium.calls[0]["headless"] is True
    assert ctx.closed == 1
    # profile 与登录态原样留在磁盘上
    assert (profile / "Default" / "Cookies").read_bytes() == b"cookie"


# ── (c) 截图路径回传 get_call_context ────────────────────────────────────


async def test_screenshot_publishes_path_into_call_context(monkeypatch, tmp_path):
    page = _FakePage()
    _install_fake(monkeypatch, tmp_path, page)
    context: dict = {}
    token = tools_pkg._CALL_CONTEXT.set(context)
    try:
        log = await br._run_actions(page, [{"op": "screenshot"}])
    finally:
        tools_pkg._CALL_CONTEXT.reset(token)

    paths = context.get("generated_image_paths")
    assert isinstance(paths, list) and len(paths) == 1
    assert pathlib.Path(paths[0]).is_absolute()
    assert pathlib.Path(paths[0]).name.startswith("shot_")
    assert paths[0] in log[0]          # 日志里带绝对路径
    assert page.screenshots == paths   # 落盘的就是回传的那个文件


async def test_screenshot_without_call_context_does_not_crash(monkeypatch, tmp_path):
    """没绑定上下文时（离线/后台）也必须把截图落盘、日志正常，绝不抛。"""
    page = _FakePage()
    _install_fake(monkeypatch, tmp_path, page)
    log = await br._run_actions(page, [{"op": "screenshot"}])
    assert "已截图" in log[0]
    assert len(page.screenshots) == 1
    assert pathlib.Path(page.screenshots[0]).is_file()


# ── (d) wait / press ────────────────────────────────────────────────────


def test_wait_and_press_accepted():
    got = br.validate_actions([
        {"op": "wait", "ms": 1500},
        {"op": "press", "selector": "#kw", "key": "Enter"},
    ])
    assert got[0] == {"op": "wait", "ms": 1500}
    assert got[1] == {"op": "press", "selector": "#kw", "key": "Enter"}
    assert "wait" in br.ALLOWED_OPS and "press" in br.ALLOWED_OPS
    assert br.MAX_OPS == 12


def test_wait_defaults_to_one_second():
    assert br.validate_actions([{"op": "wait"}])[0]["ms"] == 1000


@pytest.mark.parametrize("ms", [0, -1, 30001, 10 ** 9])
def test_wait_rejects_out_of_range(ms):
    with pytest.raises(ValueError, match="ms"):
        br.validate_actions([{"op": "wait", "ms": ms}])


@pytest.mark.parametrize("ms", ["abc", object()])
def test_wait_rejects_non_numeric(ms):
    with pytest.raises(ValueError, match="ms"):
        br.validate_actions([{"op": "wait", "ms": ms}])


@pytest.mark.parametrize("action", [
    {"op": "press", "key": "Enter"},
    {"op": "press", "selector": "#kw"},
    {"op": "press", "selector": " ", "key": " "},
])
def test_press_requires_selector_and_key(action):
    with pytest.raises(ValueError, match="press"):
        br.validate_actions([action])


def test_press_does_not_relax_ssrf():
    """新动作不得引入新 URL —— navigate 的内网拦截原样生效。"""
    with pytest.raises(ValueError):
        br.validate_actions([{"op": "navigate", "url": "http://127.0.0.1/admin"}])
    with pytest.raises(ValueError):
        br.validate_actions([{"op": "navigate", "url": "http://169.254.169.254/latest/meta-data/"}])


async def test_run_actions_executes_wait_and_press(monkeypatch, tmp_path):
    page = _FakePage()
    _install_fake(monkeypatch, tmp_path, page)
    log = await br._run_actions(page, [{"op": "wait", "ms": 1},
                                       {"op": "press", "selector": "#kw", "key": "Enter"}])
    assert "等待 1 毫秒" in log[0]
    assert "按 Enter" in log[1]
    assert page.pressed == [("#kw", "Enter")]


# ── (e) profile 超限清理 ────────────────────────────────────────────────


def _build_profile(root: pathlib.Path) -> pathlib.Path:
    """造一个「缓存 + 登录态」混杂的 profile。"""
    (root / "Default" / "Cache" / "Cache_Data").mkdir(parents=True)
    (root / "Default" / "Cache" / "Cache_Data" / "f_000001").write_bytes(b"x" * 4096)
    (root / "Default" / "Code Cache" / "js").mkdir(parents=True)
    (root / "Default" / "Code Cache" / "js" / "index").write_bytes(b"y" * 4096)
    (root / "Default" / "GPUCache").mkdir(parents=True)
    (root / "Default" / "GPUCache" / "data_0").write_bytes(b"z" * 4096)
    (root / "Default" / "Service Worker" / "CacheStorage").mkdir(parents=True)
    (root / "Default" / "Service Worker" / "CacheStorage" / "s").write_bytes(b"s" * 4096)
    (root / "Default" / "Cookies").write_bytes(b"c" * 512)
    (root / "Default" / "Login Data").write_bytes(b"l" * 512)
    (root / "Default" / "Preferences").write_bytes(b"p" * 512)
    (root / "Local State").write_bytes(b"ls" * 512)
    return root


def test_prune_only_removes_cache_dirs(tmp_path):
    profile = _build_profile(tmp_path / "browser_profile")
    result = br._prune_profile(profile, max_bytes=1024)

    assert result["removed"] > 0
    assert not (profile / "Default" / "Cache").exists()
    assert not (profile / "Default" / "Code Cache").exists()
    assert not (profile / "Default" / "GPUCache").exists()
    assert not (profile / "Default" / "Service Worker").exists()
    # 登录态一个都不能少
    assert (profile / "Default" / "Cookies").read_bytes() == b"c" * 512
    assert (profile / "Default" / "Login Data").exists()
    assert (profile / "Default" / "Preferences").exists()
    assert (profile / "Local State").exists()


def test_prune_is_noop_below_limit(tmp_path):
    profile = _build_profile(tmp_path / "browser_profile")
    result = br._prune_profile(profile, max_bytes=10 ** 9)
    assert result["removed"] == 0
    assert (profile / "Default" / "Cache").exists()


def test_prune_dry_run_keeps_everything(tmp_path):
    profile = _build_profile(tmp_path / "browser_profile")
    result = br._prune_profile(profile, max_bytes=1024, dry_run=True)
    assert result["removed"] > 0
    assert (profile / "Default" / "Cache").exists()
    assert (profile / "Default" / "Cookies").exists()


def test_prune_missing_profile_is_safe(tmp_path):
    result = br._prune_profile(tmp_path / "nope")
    assert result == {"before": 0, "after": 0, "removed": 0, "kept_dirs": []}


def test_prune_never_touches_login_state_inside_cache(monkeypatch, tmp_path):
    """白名单双保险：即使 cache 目录里出现登录态路径分量也跳过。"""
    profile = tmp_path / "browser_profile"
    kept = profile / "Cache" / "Local Storage"
    kept.mkdir(parents=True)
    (kept / "data").write_bytes(b"k" * 4096)
    (profile / "Cache" / "dead").write_bytes(b"d" * 4096)
    result = br._prune_profile(profile, max_bytes=1024)
    assert kept.exists()
    assert str(kept.relative_to(profile)) in result["kept_dirs"]


def test_prune_failure_never_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(br, "PROFILE_DIR", tmp_path / "browser_profile")

    def _boom(profile=None, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(br, "_prune_profile", _boom)
    br._prune_profile_if_needed()   # 不抛


# ── 公共 API 签名不变 ───────────────────────────────────────────────────


def test_public_signatures_unchanged():
    import inspect

    f = inspect.signature(br.fetch_page)
    assert list(f.parameters) == ["url", "timeout_sec", "max_chars"]
    assert f.parameters["timeout_sec"].kind is inspect.Parameter.KEYWORD_ONLY
    assert f.parameters["max_chars"].kind is inspect.Parameter.KEYWORD_ONLY

    r = inspect.signature(br.run_actions)
    assert list(r.parameters) == ["actions", "timeout_sec"]
    assert r.parameters["timeout_sec"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_run_actions_uses_persistent_session(monkeypatch, tmp_path):
    page = _FakePage()
    fake = _install_fake(monkeypatch, tmp_path, page)
    await br.run_actions([{"op": "get_text"}])
    assert len(fake.chromium.calls) == 1
    assert fake.chromium.calls[0]["ctx"].closed == 1

