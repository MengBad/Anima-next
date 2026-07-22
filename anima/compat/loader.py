"""AstrBot 插件加载器。

扫描 plugins/ 目录，加载 Star 子类，实例化并注册到 AnimaCore。
支持两种结构：
1. 旧结构：plugins_dir/*.py
2. AstrBot 标准结构：plugins_dir/<plugin_name>/main.py
"""

from __future__ import annotations
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Any

from anima.compat.astrbot import Star, Context, AstrMessageEvent, _REGISTRY, AstrBotConfig

logger = logging.getLogger("anima.compat.loader")

_loaded_plugins: list[Any] = []


def _install_astrbot_modules() -> None:
    """将 anima.compat.astrbot 注册为 astrbot 包的根入口。

    子模块（astrbot.api.star、astrbot.api.event 等）通过 Python 正常包
    导入机制从 anima.compat.astrbot 子包中解析，无需手动映射。
    """
    from anima.compat import astrbot as ab
    sys.modules.setdefault("astrbot", ab)


def _collect_handlers(cls) -> list[tuple[str, dict, int]]:
    """从 Star 子类中收集被装饰的方法。"""
    handlers = []
    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        filters = getattr(method, "_anima_filters", [])
        for f in filters:
            handlers.append((name, f["filter"], f["priority"]))
    return handlers


def _collect_hooks(cls) -> dict[str, list[str]]:
    """收集被 @hook.on_llm_request 等装饰的方法。"""
    hooks = {}
    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        h = getattr(method, "_anima_hooks", [])
        if h:
            hooks[name] = h
    return hooks


def _load_single_module(path: Path, package_name: str) -> Any:
    """加载单个 Python 文件并返回模块。"""
    spec = importlib.util.spec_from_file_location(package_name, path, submodule_search_locations=None)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # 强制设置 package，确保相对导入可用
    module.__package__ = package_name.rsplit(".", 1)[0] if "." in package_name else ""
    sys.modules[package_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        logger.exception("加载模块失败: %s", path)
        sys.modules.pop(package_name, None)
        return None
    return module


def _load_package_plugin(plugin_dir: Path, core=None) -> int:
    """加载一个插件目录（标准 AstrBot 结构）。"""
    import types
    count = 0
    main_file = plugin_dir / "main.py"
    if not main_file.exists():
        return 0

    package_name = plugin_dir.name

    # 安全护栏：禁止加载与已导入顶层包同名的插件目录（典型如项目自身的 `anima`）。
    # 否则会用空壳包覆盖 sys.modules[package_name]，导致整个运行时导入链崩溃。
    if package_name in sys.modules:
        logger.warning("跳过插件目录 %s：与已导入的顶层包同名，避免覆盖运行时", package_name)
        return 0

    # 将插件目录加入路径，确保相对导入可用
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))

    # 注册包本身（否则相对导入失败）
    if package_name not in sys.modules:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(plugin_dir)]
        pkg.__file__ = str(plugin_dir / "__init__.py")
        pkg.__package__ = package_name
        sys.modules[package_name] = pkg

    # 创建包 __init__（只读文件系统如 ProtectSystem=strict 下无写权限，跳过即可：
    # 包已通过 sys.modules 手动注册，物理 __init__.py 非必需）
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        try:
            init_file.write_text("", encoding="utf-8")
        except OSError:
            pass

    module = _load_single_module(main_file, f"{package_name}.main")
    if module is None:
        return 0

    # 查找 Star 子类
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Star) and obj is not Star:
            try:
                context = Context(
                    llm_client=getattr(core, "llm", None),
                    channel=getattr(core, "gateway", None),
                    config=getattr(core, "config", None),
                    provider=getattr(core, "llm", None),
                    core=core,
                    plugin_id=package_name,
                )
                # 构造签名：尝试 (context, config) 或 (context)
                try:
                    sig = inspect.signature(obj.__init__)
                    params = list(sig.parameters.keys())
                    if len(params) >= 3 and "config" in params:
                        instance = obj(context, AstrBotConfig({}))
                    else:
                        instance = obj(context)
                except Exception:
                    instance = obj(context)

                # 尝试调用 initialize（在已有事件循环中创建后台任务，不阻塞）
                if hasattr(instance, "initialize"):
                    try:
                        init_coro = instance.initialize()
                        if inspect.iscoroutine(init_coro):
                            import asyncio
                            try:
                                loop = asyncio.get_running_loop()
                                asyncio.create_task(init_coro)
                            except RuntimeError:
                                asyncio.run(init_coro)
                    except Exception:
                        logger.exception("插件 %s initialize 失败", name)

                instance._anima_handlers = _collect_handlers(obj)
                instance._anima_hooks = _collect_hooks(obj)
                instance._anima_context = context
                _loaded_plugins.append(instance)
                count += 1
                logger.info("加载 AstrBot 插件: %s", name)
            except Exception:
                logger.exception("实例化插件 %s 失败", name)

    return count


def load_plugins(plugins_dir: str | Path, core=None) -> int:
    """加载 plugins_dir 下的 AstrBot 插件。返回加载数量。"""
    _install_astrbot_modules()
    path = Path(plugins_dir)
    if not path.exists():
        return 0

    count = 0

    # 1. 标准目录结构：plugins/<plugin_name>/main.py
    for subdir in sorted(path.iterdir()):
        if subdir.is_dir() and not subdir.name.startswith("_"):
            count += _load_package_plugin(subdir, core)

    # 2. 旧结构：plugins/*.py
    for py_file in sorted(path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module = _load_single_module(py_file, py_file.stem)
        if module is None:
            continue
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Star) and obj is not Star:
                try:
                    context = Context(
                        llm_client=getattr(core, "llm", None),
                        channel=getattr(core, "gateway", None),
                        config=getattr(core, "config", None),
                        provider=getattr(core, "llm", None),
                        core=core,
                        plugin_id=py_file.stem,
                    )
                    instance = obj(context)
                    instance._anima_handlers = _collect_handlers(obj)
                    instance._anima_hooks = _collect_hooks(obj)
                    instance._anima_context = context
                    _loaded_plugins.append(instance)
                    count += 1
                    logger.info("加载 AstrBot 插件: %s", name)
                except Exception:
                    logger.exception("实例化插件 %s 失败", name)

    return count


def _load_plugin(plugin_dir: Path, core=None) -> int:
    """兼容旧命名：加载单个插件目录。"""
    return _load_package_plugin(plugin_dir, core)


def load_plugin(plugin_dir: str | Path, core=None) -> int:
    """加载单个 AstrBot 插件目录（标准结构）。

    与 `load_plugins` 不同，本函数把 `plugin_dir` 本身当作一个插件包加载，
    适用于单独测试/启用某一个插件的场景。
    """
    _install_astrbot_modules()
    return _load_package_plugin(Path(plugin_dir), core)


def unload_plugins() -> None:
    """卸载所有插件。"""
    import asyncio

    for plugin in _loaded_plugins:
        try:
            context = getattr(plugin, "_anima_context", None)
            if hasattr(plugin, "terminate"):
                term = plugin.terminate()
                if inspect.iscoroutine(term):
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.create_task(term)
                    except RuntimeError:
                        asyncio.run(term)
            if hasattr(plugin, "on_unload"):
                plugin.on_unload()
            if context is not None:
                context.close()
        except Exception:
            logger.exception("插件卸载异常")
    _loaded_plugins.clear()
