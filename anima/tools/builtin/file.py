"""内置文件工具。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("anima.tools.builtin.file")


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
        # 🔴 2026-10-21（生产实证）：此前只回「已生成文件: /opt/anima/data/output/x.txt」——
        # **路径在服务器上，QQ 里的用户根本打不开**。对话层于是说「具体写在文件里
        # 啦，自己去看吧喵」，用户什么也看不到（owner 实测提出）。
        # 修法：把写入内容登记进 tool call context，由对话出站阶段把内容
        # **贴回会话**（只在内容不长时贴全文，过长时贴开头+说明已保存）。
        try:
            from anima.tools import get_call_context
            _ctx = get_call_context()
            if isinstance(_ctx, dict):
                _files = _ctx.setdefault("generated_text_files", [])
                if isinstance(_files, list):
                    _files.append({"name": Path(path).name, "content": content})
        except Exception:
            logger.debug("登记写文件产物失败（不影响写入本身）", exc_info=True)
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


def list_workspace_files(data_dir: str = "data", limit: int = 40) -> list[str]:
    """列出工作区（``data/``）内可读的文本文件名（相对路径），按修改时间倒序。

    🔴 2026-10-20（D-37）：供「文件不存在」时回给模型，避免它**猜文件名**。
    生产实证：Agent 任务 36bcd32ab6 的步骤「先输出前 8 句」被执行为
    ``read_file("output/script_part1.txt")`` ⇒ ``FileNotFoundError``，
    而**没有任何步骤调用过 write_file** —— 工作区里根本没有这个文件。
    模型当时无从知道工作区有什么，只能猜 ⇒ 一次无意义失败 + 一次 replan。

    刻意**只读目录**且**只返回相对 data_dir 的路径**：不暴露绝对路径、
    不递归全部（限前 ``limit`` 个），避免把无关的巨型文件清单塞进上下文。
    """
    base = Path(data_dir).resolve()
    rows: list[tuple[float, str]] = []
    try:
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            try:
                rows.append((p.stat().st_mtime, str(p.relative_to(base))))
            except OSError:
                continue
    except OSError:
        return []
    rows.sort(key=lambda kv: kv[0], reverse=True)
    return [name for _m, name in rows[:limit]]


def count_workspace_files(data_dir: str = "data") -> int:
    """工作区文件总数（供截断提示用）。

    🔴 2026-10-21（B1 实证）：`list_workspace_files` 只返回最近修改的
    ``limit`` 个，而 D-37 的回执把它当**完整清单**交给模型 ⇒ 模型拿到
    残缺清单后误判「文件不存在」（实证：health_history.json 因修改时间
    不在最新 40 名内被截掉，模型据此判定它不在工作区 —— 而它确实存在）。
    有了总数，回执才能说清「清单是残缺的」，而不是假装这是全貌。
    """
    base = Path(data_dir).resolve()
    n = 0
    try:
        for p in base.rglob("*"):
            if p.is_file():
                n += 1
    except OSError:
        return 0
    return n


def make_read_file_tool(core=None):
    """创建绑定 core 配置的文件读取 handler（只读）。

    🔴 2026-10-20（D-37）：文件不存在时**不再抛裸异常**，改为返回带
    「工作区现有文件清单」的可操作回执。理由：抛异常 ⇒ 该步失败 ⇒ 触发
    replan，而模型下一轮**仍然不知道该读什么**，只会再猜一次（生产已实证）。
    确定性清单比"报错"更有用，且**不改变成功路径的任何字节**。
    """
    def _read(filename: str, max_bytes: int = 200_000) -> str:
        data_dir = "data"
        if core and hasattr(core, "config"):
            data_dir = core.config.persistence.data_dir
        try:
            return read_file(filename, data_dir, max_bytes)
        except FileNotFoundError:
            available = list_workspace_files(data_dir)
            total = count_workspace_files(data_dir)
            # 🔴 截断必须说出口：否则模型把残缺清单当全貌（B1 实证）。
            truncated = total > len(available)
            scope = (
                f"（工作区共 {total} 个文件，下面只列出最近修改的 {len(available)} 个，"
                f"**未列出的不等于不存在**）"
                if truncated else ""
            )
            hint = ("；".join(available) if available else "（工作区为空，还没有任何文件）")
            return (
                f"文件不存在: {filename}。当前工作区可用的文件有：{hint}{scope}"
                "。请先确认要读的文件名；若目标内容尚未生成，应改用写入工具先产出。"
                "注意：上面清单可能被截断，未出现在清单中的文件也可能存在。"
            )
        except ValueError as exc:
            # 🔴 2026-10-21（B1 实证）：「过大」与「不存在」此前同为裸 ValueError ⇒
            # 模型把「读不了」误解成「不存在」，该误解还被验收器固化成失败理由
            # （实证：912KB 的 health_history.json 超 200KB 上限，任务 2 次重规划后
            #  失败，失败理由写成「文件根本不存在」）。
            # 现在：路径非法仍是调用方的错，如实抛出；**过大**则给可操作回执。
            if "过大" not in str(exc):
                raise
            # 原始细节只进日志（可排障），**绝不进返回值**（模型可见 ⇒ 可能出站）。
            logger.warning("文件过大拒读: %s（%s）", filename, exc)
            # 走既有白名单机制（D1 口径）：原因码之外不夹带任何内部细节。
            from anima.tools import TOOL_REASON_FILE_TOO_LARGE, tool_unavailable
            return (
                tool_unavailable(TOOL_REASON_FILE_TOO_LARGE)
                + "。该文件确实存在，只是超过单次读取上限；"
                "不要用「文件不存在」来解释这次失败。"
            )
    return _read
