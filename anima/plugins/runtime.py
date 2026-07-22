"""Anima 原生插件运行时：只加载 Owner 已安装的声明式插件包。"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from anima.tools import LEVEL_OWNER, Tool

logger = logging.getLogger("anima.plugins")


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    description: str = ""
    min_level: int = LEVEL_OWNER
    entrypoint: str = "main.py"
    enabled: bool = True
    capabilities: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "PluginManifest":
        plugin_id = str(raw.get("id") or "").strip()
        if not plugin_id or not plugin_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError("插件 id 必须是字母、数字、下划线或连字符")
        level = int(raw.get("min_level", LEVEL_OWNER))
        if level < 1 or level > LEVEL_OWNER:
            raise ValueError("插件 min_level 必须在 1-4")
        entrypoint = str(raw.get("entrypoint") or "main.py").strip()
        if Path(entrypoint).name != entrypoint or not entrypoint.endswith(".py"):
            raise ValueError("插件 entrypoint 必须是包内 Python 文件")
        return cls(plugin_id, str(raw.get("name") or plugin_id)[:80], str(raw.get("version") or "0.1.0")[:40],
                   str(raw.get("description") or "")[:500], level, entrypoint, bool(raw.get("enabled", True)),
                   [str(item)[:64] for item in (raw.get("capabilities") or [])[:16]])


@dataclass
class PluginContext:
    core: Any
    manifest: PluginManifest
    path: Path

    def register_tool(self, name: str, description: str, parameters: dict[str, Any], handler: Any,
                      *, min_level: int | None = None, required: list[str] | None = None,
                      danger: bool = False) -> str:
        safe = f"plugin_{self.manifest.id}_{name}".replace("-", "_")
        self.core.tools.register(Tool(safe, description, parameters, handler,
                                      min_level=min_level or self.manifest.min_level,
                                      required=required, danger=danger))
        return safe


class NativePluginRuntime:
    def __init__(self, core: Any, root: str | Path | None = None):
        self.core = core
        self.root = Path(root or Path(core.config.persistence.data_dir) / "plugins")
        self.manifests: dict[str, PluginManifest] = {}
        self.instances: dict[str, Any] = {}
        self.tools: dict[str, list[str]] = {}

    async def load_all(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        loaded = 0
        for plugin_dir in sorted(path for path in self.root.iterdir() if path.is_dir() and not path.name.startswith("_")):
            try:
                loaded += int(await self.load(plugin_dir))
            except Exception:
                logger.exception("原生插件加载失败: %s", plugin_dir.name)
        return loaded

    async def load(self, plugin_dir: str | Path) -> bool:
        path = Path(plugin_dir).resolve()
        manifest_path = path / "plugin.yaml"
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
        if not isinstance(raw, dict):
            raise ValueError("插件缺少 plugin.yaml")
        manifest = PluginManifest.from_mapping(raw)
        if not manifest.enabled:
            self.manifests[manifest.id] = manifest
            return False
        if manifest.id in self.instances:
            await self.unload(manifest.id)
        module_path = (path / manifest.entrypoint).resolve()
        if path not in module_path.parents:
            raise ValueError("插件入口越界")
        spec = importlib.util.spec_from_file_location(f"anima_native_plugin_{manifest.id}", module_path)
        if spec is None or spec.loader is None:
            raise ValueError("无法创建插件模块")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        factory = getattr(module, "create_plugin", None)
        if not callable(factory):
            raise ValueError("插件必须导出 create_plugin(context)")
        before = set(self.core.tools.tools)
        context = PluginContext(self.core, manifest, path)
        instance = factory(context)
        if inspect.isawaitable(instance): instance = await instance
        initializer = getattr(instance, "start", None)
        if callable(initializer):
            result = initializer()
            if inspect.isawaitable(result): await result
        self.manifests[manifest.id], self.instances[manifest.id] = manifest, instance
        self.tools[manifest.id] = sorted(set(self.core.tools.tools) - before)
        audit = getattr(self.core, "audit", None)
        if audit: audit.emit("native_plugin_loaded", plugin_id=manifest.id, tools=self.tools[manifest.id])
        return True

    async def unload(self, plugin_id: str) -> bool:
        instance = self.instances.pop(plugin_id, None)
        if instance is None: return False
        terminator = getattr(instance, "stop", None)
        if callable(terminator):
            result = terminator()
            if inspect.isawaitable(result): await result
        for name in self.tools.pop(plugin_id, []): self.core.tools.unregister(name)
        audit = getattr(self.core, "audit", None)
        if audit: audit.emit("native_plugin_unloaded", plugin_id=plugin_id)
        return True

    async def close(self) -> None:
        for plugin_id in list(self.instances): await self.unload(plugin_id)

    def status(self) -> list[dict[str, Any]]:
        return [{"id": manifest.id, "name": manifest.name, "version": manifest.version,
                 "description": manifest.description, "enabled": manifest.enabled,
                 "loaded": manifest.id in self.instances, "tools": self.tools.get(manifest.id, []),
                 "capabilities": manifest.capabilities}
                for manifest in self.manifests.values()]
