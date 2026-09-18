#!/usr/bin/env python3
"""GitHub ↔ 生产 一致性门（只读 · 零副作用）。

为什么需要它（债 D-28）
----------------------
生产 `/opt/anima` **不是** git 检出，是 SFTP 搬运的树；两边长期无自动一致性门。
结果是两类差异都能潜伏很久：
  * **仅本地有**（GitHub 有、生产无）—— 融合时漏传的**新增件**。最危险：新代码
    import 一个生产不存在的模块 ⇒ **启动即 ImportError**。
  * **仅生产有** —— 生产独有的废弃模块（已知 12 个），其内部 import 已悬空。

判方向的**唯一可靠口径是 LF 归一化后的内容哈希，不是行 diff**：
本机工作树是 CRLF、生产是 LF，行 diff 会把每个文件都报成"不同"（CRLF 噪声），
据此判方向会得出完全错误的结论。

用法
----
    # ① 本地（仓库）侧清单
    python deploy/check_github_prod_parity.py emit --root . > local.txt
    # ② 生产侧清单（脚本可经 stdin 传过去，不落地）
    cd /opt/anima && ./venv/bin/python - --root /opt/anima emit < check.py > prod.txt
    # ③ 比对
    python deploy/check_github_prod_parity.py compare local.txt prod.txt

退出码：0 = 内容一致；1 = 存在差异（或比对文件缺失）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# 默认比对范围：代码 + 测试 + schema + 两份配置（生产实配 config.yaml 故意排除 ——
# 它含 6 处明文密钥且不在版本控制，见债 D-07）。
DEFAULT_PATHS = (
    "anima",
    "tests",
    "schemas",
    "config/default.yaml",
    "config/production.example.yaml",
)
_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".toml", ".cfg", ".txt"}


def _lf_sha256(path: Path) -> str:
    """LF 归一化后的内容哈希（跨 CRLF/LF 稳定）。"""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def emit(root: Path, paths: tuple[str, ...] = DEFAULT_PATHS) -> dict[str, str]:
    """产出一份 {相对路径: lf_sha256} 清单。"""
    out: dict[str, str] = {}
    for spec in paths:
        base = root / spec
        if base.is_file():
            out[spec] = _lf_sha256(base)
        elif base.is_dir():
            for f in sorted(base.rglob("*")):
                if not f.is_file():
                    continue
                if "__pycache__" in f.parts or f.suffix not in _SUFFIXES:
                    continue
                out[str(f.relative_to(root)).replace("\\", "/")] = _lf_sha256(f)
    return out


def _read_manifest(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    try:  # 支持 JSON（含 --json emit 的输出）
        data = json.loads(text)
        if isinstance(data, dict) and "manifest" in data:
            return {str(k): str(v) for k, v in data["manifest"].items()}
    except Exception:  # noqa: BLE001 - 回落到 TSV
        pass
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def compare(local: dict[str, str], remote: dict[str, str]) -> dict:
    keys = set(local) | set(remote)
    differ = sorted(k for k in keys if k in local and k in remote and local[k] != remote[k])
    only_local = sorted(set(local) - set(remote))
    only_remote = sorted(set(remote) - set(local))
    return {
        "compared": len(keys),
        "differ": differ,
        "only_local": only_local,     # GitHub 有、生产无 ⇒ 融合漏传风险（最危险）
        "only_remote": only_remote,   # 生产独有 ⇒ 废弃模块 / 未回收改动
        "in_sync": not (differ or only_local or only_remote),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GitHub ↔ 生产 一致性门（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="产出哈希清单")
    e.add_argument("--root", default=".")
    e.add_argument("--json", action="store_true")
    e.add_argument("--paths", nargs="*", default=list(DEFAULT_PATHS))

    c = sub.add_parser("compare", help="比对两份清单")
    c.add_argument("local")
    c.add_argument("remote")
    c.add_argument("--json", action="store_true")
    c.add_argument("--show", type=int, default=40, help="每节最多列多少条")

    args = ap.parse_args(argv)

    if args.cmd == "emit":
        manifest = emit(Path(args.root).resolve(), tuple(args.paths))
        if args.json:
            print(json.dumps({"manifest": manifest}, ensure_ascii=False, indent=1))
        else:
            for rel in sorted(manifest):
                print(f"{manifest[rel]}\t{rel}")
        return 0

    report = compare(_read_manifest(Path(args.local)), _read_manifest(Path(args.remote)))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["in_sync"] else 1

    print(f"比对文件数: {report['compared']}")
    print("-" * 72)
    print(f"[内容不同] {len(report['differ'])}")
    for rel in report["differ"][: args.show]:
        print(f"   ≠ {rel}")
    print(f"[仅本地有 = GitHub 有 / 生产无 ⇒ 融合漏传，可能启动即 ImportError] "
          f"{len(report['only_local'])}")
    for rel in report["only_local"][: args.show]:
        print(f"   + {rel}")
    print(f"[仅生产有 = 生产独有/未回收] {len(report['only_remote'])}")
    for rel in report["only_remote"][: args.show]:
        print(f"   - {rel}")
    print("-" * 72)
    print("✅ 完全一致" if report["in_sync"] else "⚠️ 存在差异（见上）")
    return 0 if report["in_sync"] else 1


if __name__ == "__main__":
    sys.exit(main())
