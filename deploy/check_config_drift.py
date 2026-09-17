#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产配置漂移检查器（只读）。

背景：`config.yaml` 被 `.gitignore` 忽略 ⇒ 生产是服务器手工件、与仓库模板是两个真源。
这正是「治理文档写 capability_mode=false、生产实际 true」能长期不被发现的机制
（2026-09-18 审查 P0-2 / P0-3）。

本工具把「生产实配 vs 仓库模板」的比较变成一个**可重复执行的命令**，只比三件事：

  1. 键集漂移 —— 生产有而模板无 / 模板有而生产无（段级 + 键级）
  2. 开关面   —— 生产中显式设为 true、而代码默认 false 的布尔开关（治理开关最危险的一类）
  3. 密钥面   —— 生产 yaml 里疑似**明文**存放的密钥键名（只报键名与长度，不回报值）

只读：不写 config.yaml、不 import anima、不启服务。
退出码：0 = 无漂移；1 = 发现漂移（可用于 CI / 运维巡检）。

用法：
    python deploy/check_config_drift.py                       # 默认 /opt/anima/config.yaml
    python deploy/check_config_drift.py --prod /path/config.yaml
    python deploy/check_config_drift.py --template config/production.example.yaml
"""
from __future__ import annotations

import argparse
import os
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 pyyaml：venv/bin/pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

SECRET_HINT = re.compile(
    r"key|token|secret|password|passwd|pwd|cookie|auth|credential|webhook", re.I)
# 明显不是密钥的键：token 预算类。避免误报。
SECRET_WHITELIST = re.compile(r"token_budget|max_tokens|budget|_limit|token_estimate", re.I)


def _load(path: str) -> dict:
    if not os.path.isfile(path):
        raise SystemExit("找不到配置文件：%s" % path)
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict[str, object] = {}
    for k, v in (d or {}).items():
        p = "%s.%s" % (prefix, k) if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(_flatten(v, p))
        else:
            out[p] = v
    return out


def _is_secret_key(path: str) -> bool:
    return bool(SECRET_HINT.search(path)) and not SECRET_WHITELIST.search(path)


# 动态键前缀：这些路径的子键随运营变化（群号 / 用户号），漂移检查按形状比对而非字面键。
# 归约规则：`<prefix>.<id>.<rest...>` → `<prefix>.*.<rest...>`（只替换一级 id）。
DYNAMIC_PREFIXES = ("permissions.group_policies",)


def _normalize_dynamic(flat: dict) -> dict:
    """把动态键的 id 段归约为 `*`，使「键集比对」只关心形状。"""
    out: dict = {}
    for k, v in flat.items():
        nk = k
        for pre in DYNAMIC_PREFIXES:
            if k.startswith(pre + "."):
                rest = k[len(pre) + 1:]
                parts = rest.split(".", 1)
                nk = "%s.*.%s" % (pre, parts[1]) if len(parts) == 2 else "%s.*" % pre
                break
        out[nk] = v
    return out


def _drop_empty_dict_conflicts(only_a: list[str], a: dict, b: dict) -> list[str]:
    """剔除「空 dict 叶子 vs 有子键的子树」这类**扁平化伪差异**。

    例：生产 `llm.module_models = {}`（空 dict 落成叶子），参考侧是
    `llm.module_models.image_generation`（递归成子键）—— 同一配置面，不应报漂移。
    """
    out = []
    for k in only_a:
        if a.get(k) == {} and any(r.startswith(k + ".") for r in b):
            continue
        out.append(k)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生产 config.yaml 与仓库参考的漂移检查（只读）")
    ap.add_argument("--prod", default=os.environ.get("ANIMA_CONFIG", "/opt/anima/config.yaml"))
    ap.add_argument("--template", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "production.example.yaml"))
    ap.add_argument("--default", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "default.yaml"),
        help="仓库基线默认配置；与模板合并构成参考键集")
    args = ap.parse_args()

    prod = _load(args.prod)
    tmpl = _load(args.template)
    base = _load(args.default) if os.path.isfile(args.default) else {}

    # 参考 = 基线默认 ∪ 生产模板（模板优先）。
    # 语义：生产**可以**只设置默认值里存在的键（不设置=走默认，不算漂移）；
    #       只有「生产有、而参考两侧都没有」才是真正未登记的新键。
    ref = dict(base)
    for k, v in (tmpl or {}).items():
        if isinstance(v, dict) and isinstance(ref.get(k), dict):
            merged = dict(ref[k])
            merged.update(v)
            ref[k] = merged
        else:
            ref[k] = v

    drift = 0

    print("=" * 74)
    print("生产配置漂移检查（只读）")
    print("  prod     = %s  (%s B)" % (args.prod, os.path.getsize(args.prod)))
    print("  template = %s  (%s B)" % (args.template, os.path.getsize(args.template)))
    print("  default  = %s  (%s B)  ← 与 template 合并后作为参考键集"
          % (args.default, os.path.getsize(args.default) if os.path.isfile(args.default) else 0))
    print("=" * 74)

    # --- 1) 段级漂移 ---------------------------------------------------------
    only_prod = sorted(set(prod) - set(ref))
    only_ref = sorted(set(ref) - set(prod))
    print("\n[1] 段级")
    print("    生产有 / 参考无 (=真漂移) : %s" % (only_prod or "无 ✅"))
    print("    参考有 / 生产无 (=走默认) : %s" % (only_ref or "无"))
    drift += len(only_prod)

    # --- 2) 键级漂移 ---------------------------------------------------------
    fp, fr = _normalize_dynamic(_flatten(prod)), _normalize_dynamic(_flatten(ref))
    k_prod = _drop_empty_dict_conflicts(sorted(set(fp) - set(fr)), fp, fr)
    k_ref = _drop_empty_dict_conflicts(sorted(set(fr) - set(fp)), fr, fp)
    print("\n[2] 键级")
    print("    生产有 / 参考无 (=真漂移) (%d):" % len(k_prod))
    for k in k_prod:
        v = fp[k]
        shown = "<SECRET len=%d>" % len(str(v)) if _is_secret_key(k) and v not in (None, "") else repr(v)[:70]
        print("      + %-46s = %s" % (k, shown))
    if not k_prod:
        print("      ✅ 无未登记键")
    print("    参考有 / 生产无 (=该键走默认值) 共 %d 个（不视为漂移）" % len(k_ref))
    drift += len(k_prod)

    # --- 3) 开关面 ----------------------------------------------------------
    print("\n[3] 开关面：生产显式为 true 的布尔键")
    risky = [k for k, v in sorted(fp.items()) if isinstance(v, bool) and v is True]
    for k in risky:
        print("      ON  %s" % k)
    print("    合计 %d 个" % len(risky))

    # --- 4) 密钥面 ----------------------------------------------------------
    print("\n[4] 生产 yaml 中疑似明文密钥（只报键名与长度）")
    found = [(k, len(str(v))) for k, v in sorted(fp.items())
             if _is_secret_key(k) and v not in (None, "", 0, False, True)
             and not isinstance(v, (dict, list))]
    for k, n in found:
        print("      🔴 %-46s 明文 %d 字符" % (k, n))
    if not found:
        print("      未发现（或均已外置到环境变量）")
    print("    合计 %d 处" % len(found))

    print("\n" + "=" * 74)
    if drift:
        print("结论：发现 %d 处未登记漂移 —— 生产存在参考侧没有的配置面。" % drift)
        print("      处理方式：确认该差异是否有意，然后登记进 config/production.example.yaml。")
        print("=" * 74)
        return 1
    print("结论：无未登记漂移（生产配置面已被模板完整覆盖）。值级差异仍需人工审阅。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
