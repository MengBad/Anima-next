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


def read_file(filename: str, data_dir: str = "data", max_bytes: int = 200_000) -> str:
    """读取 data 目录内的文件，返回文本内容。

    **只读**：只调用读 API，绝不写入；路径强制限制在 ``data_dir`` 内，
    禁止 ``../`` 目录穿越；超过 ``max_bytes`` 拒读，避免大文件撑爆上下文。
    """
    base = Path(data_dir).resolve()
    path = (base / filename).resolve()
    # 防目录穿越：解析后必须仍在 base 内（覆盖 '..'、软链、绝对路径混入等）
    if not str(path).startswith(str(base)):
        raise ValueError(f"非法读取路径: {filename}")
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {filename}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"文件过大 ({size} > {max_bytes} 字节)，拒绝读取")
    return path.read_text(encoding="utf-8", errors="replace")


def make_read_file_tool(core=None):
    """创建绑定 core 配置的文件读取 handler（只读）。"""
    def _read(filename: str, max_bytes: int = 200_000) -> str:
        data_dir = "data"
        if core and hasattr(core, "config"):
            data_dir = core.config.persistence.data_dir
        return read_file(filename, data_dir, max_bytes)
    return _read
