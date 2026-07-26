"""内置文件工具。"""

from __future__ import annotations

from pathlib import Path


def write_file(filename: str, content: str, data_dir: str = "data") -> str:
    """将内容写入输出目录，返回文件路径。

    安全：仅允许文件名，禁止 ../ 目录穿越。
    """
    out_dir = Path(data_dir) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 仅取文件名，剥离任何目录成分（防止 ../ 穿越）
    safe_name = Path(filename).name or "output.txt"
    path = out_dir / safe_name
    # 二次校验：解析后必须仍在 out_dir 内
    if not str(path.resolve()).startswith(str(out_dir.resolve())):
        raise ValueError(f"非法输出路径: {filename}")
    path.write_text(content, encoding="utf-8")
    return str(path.resolve())


def make_file_tool(core=None):
    """创建绑定 core 配置的文件写入 handler。"""
    def _write(filename: str, content: str) -> str:
        data_dir = "data"
        if core and hasattr(core, "config"):
            data_dir = core.config.persistence.data_dir
        path = write_file(filename, content, data_dir)
        return f"已生成文件: {path}"
    return _write
