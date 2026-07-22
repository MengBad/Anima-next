import asyncio

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig
from anima.plugins import NativePluginRuntime
from anima.tools import create_default_registry


def test_native_plugin_manifest_loads_and_unloads(tmp_path):
    plugin = tmp_path / "plugins" / "hello"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("id: hello\nname: Hello\nversion: '1.0'\nmin_level: 2\n", encoding="utf-8")
    (plugin / "main.py").write_text(
        "class P:\n"
        "    def __init__(self, ctx): self.ctx=ctx\n"
        "def create_plugin(ctx):\n"
        "    ctx.register_tool('hello', 'say hi', {}, lambda: 'hi', required=[])\n"
        "    return P(ctx)\n", encoding="utf-8")
    core = AnimaCore(AnimaConfig.model_validate({"persistence":{"data_dir":str(tmp_path)}}))
    core.tools = create_default_registry(core)
    runtime = NativePluginRuntime(core, tmp_path / "plugins")
    assert asyncio.run(runtime.load_all()) == 1
    assert "plugin_hello_hello" in core.tools.tools
    assert asyncio.run(runtime.unload("hello"))
    assert "plugin_hello_hello" not in core.tools.tools
