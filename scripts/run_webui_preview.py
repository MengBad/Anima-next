"""Run the built WebUI against an isolated, non-QQ Anima core for visual QA."""

from __future__ import annotations

import argparse
import asyncio
import tempfile

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig, WebUIConfig
from anima.webui import WebUIServer


async def serve(port: int, token: str) -> None:
    with tempfile.TemporaryDirectory(prefix="anima-webui-preview-") as data_dir:
        config = AnimaConfig(
            persistence=PersistenceConfig(data_dir=data_dir),
            permissions=PermissionConfig(owners=[1]),
            webui=WebUIConfig(enabled=True, host="127.0.0.1", port=port, token=token),
        )
        core = AnimaCore(config)
        core._running = True
        core.audit.emit("preview_started", ok=True)
        server = WebUIServer(core)
        await server.start()
        print(f"Anima WebUI preview: http://127.0.0.1:{port}/?token={token}", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await server.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--token", default="preview-token")
    args = parser.parse_args()
    asyncio.run(serve(args.port, args.token))


if __name__ == "__main__":
    main()
