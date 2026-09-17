"""工具失败判定「完备性」契约（09-18 fail-open 审计补齐）。

## 背景

`anima.tools._TOOL_FAILURE_PREFIXES` 是**前缀允许清单**，而工具失败文本是自由散文
（各 handler 自己拼的中文句子）。09-18 对**全部注册点**做 AST 审计（解析每个
`Tool(...)` 的 handler → 扫其 return 字面量 → 与 `tool_result_ok()` 对表），
发现 **47 处「正在报告失败却被判成功」**。

这不是文案问题。`ok` 是真实控制流：

* `anima/agent/react.py:223` → `_FRESHNESS_TOOLS` 门控 + 工具轨迹（`tool_trace`）；
* `anima/agent/executor.py:412` → 决定一个 `AgentTask` 步骤**是否是成功**。
  被沙箱护栏**拒绝**的 `agent_*` 步骤此前被当作「成功结果」返回 —— 安全相关。

## 本文件钉死两条不变式

1. **判定**：已知的失败形态一律 `ok=False`（下面逐条列出真实可达的 shape）。
2. **文本**：修判定**不得**顺带把文本折叠成通用串 —— 这些是作者手写、可对模型的
   中文提示，此前它们以「成功」身份原样进上下文；改判定时若一并折叠，模型会从
   「知道为什么」退化为「只知道失败了」。`normalize_tool_result()` 必须原样返回。

外加一条**机制性防止回归**的守卫：遍历全部 Tool handler 的 return 字面量，
任何「看起来在报告失败」的串都必须被判失败 —— 否则新增 handler 又会静默漏判。
"""
from __future__ import annotations

import ast
import io
import os
import sys

import pytest

from anima.tools import (
    _TOOL_FAILURE_PREFIXES,
    _SAFE_TOOL_FAILURE,
    normalize_tool_result,
    tool_result_ok,
)

# ---------------------------------------------------------------------------
# 一、站点级：逐条对应一个真实可达的 return（file:line 见注释）
# ---------------------------------------------------------------------------
SITE_FAILURES = [
    # --- 全角冒号：原注册表只收 ASCII "错误:" ---
    ("错误：视频《x》字幕元数据缺失地址。", "bilibili.py:432"),
    ("内部错误：LLM 未就绪", "capability.py:64"),
    ("错误：无法获取 core", "meme_interact.py:143 / qzone.py:183 / voice.py:34"),
    # --- agent_* 沙箱工具（OWNER；错判成功 = executor 把被拒步骤当成功）---
    ("[agent] 缺少 url 参数", "agent_tools.py:100,235"),
    ("[agent] 不支持的 HTTP 方法: POST", "agent_tools.py:103"),
    ("[agent] 拒绝非 http/https URL: file:///etc/passwd", "agent_tools.py:105,238"),
    ("[agent] 拒绝工作区外路径: /etc/passwd", "agent_tools.py:131,146"),
    ("[agent] 拒绝越界目录: /etc", "agent_tools.py:171"),
    ("[agent] 命令被安全护栏拒绝: rm -rf /", "agent_tools.py:190"),
    ("[agent] 命令超时 (30s)，已终止", "agent_tools.py:202"),
    ("[agent] 执行失败: FileNotFoundError: boom", "agent_tools.py:205"),
    ("[agent] 请求失败: TimeoutError: boom", "agent_tools.py:118"),
    ("[agent] 读取失败: IsADirectoryError: boom", "agent_tools.py:137"),
    ("[agent] 写入失败: PermissionError: boom", "agent_tools.py:152"),
    ("[agent] 列举失败: OSError: boom", "agent_tools.py:182"),
    ("[agent] 文件不存在: x.txt", "agent_tools.py:133"),
    ("[agent] 目录不存在: x", "agent_tools.py:175"),
    ("[agent] 不是目录: x", "agent_tools.py:177"),
    ("[agent] 动作 'evaluate' 不在允许列表（owner_browser.allowed_actions）内", "agent_tools.py:231"),
    ("[agent] click 需要 selector", "agent_tools.py:255"),
    ("[agent] 未知 browser 动作: fly", "agent_tools.py:264"),
    ("[agent] 浏览器操作失败: TimeoutError: boom", "agent_tools.py:269"),
    ("[agent] agent_browser 不可用：未安装 Playwright。", "agent_tools.py:243"),
    # --- 其余可达点 ---
    ("计算出错: ZeroDivisionError", "system.py:37"),
    ("搜索时出错了：TimeoutError", "web.py:412"),
    ("生图失败：TimeoutError", "owner.py:64"),
    ("改图失败：TimeoutError", "owner.py:93"),
    ("整理失败喵，稍后再试", "merge.py:47"),
    ("查询关系失败", "social.py:235"),
    ("创建 SkillDraft 失败：boom", "owner.py:23"),
    ("列目录失败：boom", "repo.py:60"),
    ("读取失败：boom", "repo.py:72"),
    ("搜索失败：boom", "repo.py:81"),
    ("[self_code] 执行异常（详见日志）", "owner.py:48"),
    ("B 站视频理解功能未启用。", "bilibili.py:285"),
    ("读取 B 站视频失败：boom（bvid=BV1）", "bilibili.py:418"),
    ("读取 B 站视频失败：boom", "bilibili.py:475"),
    ("下载字幕失败（HTTP 404）。", "bilibili.py:448"),
    ("运行状态暂时不可用，本喵不想拿不准的数据糊弄你。", "system.py:110"),
    ("群信息查询不可用", "social.py:297"),
    ("群成员查询不可用（OneBot 通道未连接）", "social.py:134"),
    ("戳一戳功能不可用（OneBot 通道未连接）", "social.py:36"),
    ("禁言功能不可用", "social.py:334"),
    ("踢人功能不可用", "social.py:352"),
    ("修改名片功能不可用", "social.py:318"),
    ("表情检索暂不可用，本次可改用 send_meme 或不带图。", "sticker.py:101"),
    ("图片数据无法解析，偷取失败。", "sticker.py:151"),
    ("网页探查失败，地址不可访问或不再满足公网安全限制", "skills/registry.py:154"),
    ("当前未配置 LLM，无法执行该技能", "skills/registry.py:168"),
    ("技能不可用：boom", "skills/registry.py:214"),
    # --- 动态前缀：固定前缀表达不了，靠后缀/中缀规则 ---
    ("获取群 123456 信息失败", "social.py:304"),
    ("获取群 123456 成员列表失败", "social.py:164"),
    ("拿到了 UP 主页（1 / mid=1），但最近投稿列表接口暂时不可用（boom）。"
     "请用户直接发某个视频的 BV 链接，本喵再用 bilibili_read 读字幕。", "bilibili.py:233"),
]


@pytest.mark.parametrize("raw,origin", SITE_FAILURES, ids=[o for _, o in SITE_FAILURES])
def test_site_is_detected_as_failure(raw, origin):
    assert tool_result_ok(raw) is False, f"{origin}: 仍被判成功 → {raw!r}"


@pytest.mark.parametrize("raw,origin", SITE_FAILURES, ids=[o for _, o in SITE_FAILURES])
def test_site_text_is_not_folded(raw, origin):
    """判定修正**不得**夺走模型看到的失败原因（文本逐字节不变）。"""
    text, ok = normalize_tool_result(raw)
    assert ok is False, origin
    assert text == raw, f"{origin}: 文本被折叠成 {text!r}"


# ---------------------------------------------------------------------------
# 二、防反向回归：旧注册表语义必须保持
# ---------------------------------------------------------------------------
def test_legacy_prefixes_still_fold_to_safe_string():
    """旧前缀集合命中的文本**仍然**折叠（P0-1 的语义一字不改）。"""
    for raw in (
        "工具执行错误：boom",
        "错误: 你的权限不足，无法调用工具 'x'",
        "[沙箱] 执行异常（详见日志）",
        "天气查询失败（北京）: boom。可稍后重试或检查网络。",
        "查询失败",
        "X 搜索未完成：X 暂被风控拦截（页面空壳）",
        "X 搜索失败：Page.goto: net::ERR_CONNECTION_CLOSED",
        "X 搜索模块不可用（x_browser 未加载）。",
        "工具暂时不可用，未获得可验证结果。",
    ):
        assert tool_result_ok(raw) is False, raw
        assert normalize_tool_result(raw)[0] == _SAFE_TOOL_FAILURE, raw


def test_p0_1_whitelist_reasons_still_pass_through():
    """P0-1：白名单原因码仍原样放行（不是通用串）。"""
    from anima.tools import TOOL_REASON_X_QUOTA_SEARCH, tool_unavailable

    raw = tool_unavailable(TOOL_REASON_X_QUOTA_SEARCH)
    text, ok = normalize_tool_result(raw)
    assert ok is False
    assert text == raw
    assert text != _SAFE_TOOL_FAILURE


def test_internal_markers_never_reach_model():
    """含内部细节的失败**必须**折叠 —— 扩表不得开后门。"""
    for raw in (
        "[agent] 执行失败: Traceback (most recent call last):\n  File \"x.py\", line 1",
        "错误：boom [stderr] unshare failed",
        "[agent] 读取失败: Permission denied",
    ):
        assert tool_result_ok(raw) is False, raw
        assert normalize_tool_result(raw)[0] == _SAFE_TOOL_FAILURE, raw


def test_success_strings_are_not_flipped():
    """成功串一律不得被误判（族内成功形态 + 普通成功文本）。"""
    for raw in (
        "[agent] 已写入 out.txt (12 字节)",
        "[agent] 已写入 a.txt (1 字节) 与 b.txt (2 字节)",
        "[HTTP 200]hello / world",
        "[exit 0] / ok",
        "hello world",
        "计算结果是 42",
        "[output_image] /opt/anima/data/x.png",
    ):
        assert tool_result_ok(raw) is True, f"被误判为失败: {raw!r}"
        assert normalize_tool_result(raw) == (raw, True), raw


def test_family_success_exception_is_minimal():
    """族例外必须精确 —— 只有 `已写入` 形态放行，其余 `[agent] ` 全判失败。"""
    assert tool_result_ok("[agent] 已写入 x (1 字节)") is True
    assert tool_result_ok("[agent] 已删除 x") is False      # 未列出的族内形态 → fail-closed
    assert tool_result_ok("[agent] 未知做法") is False


# ---------------------------------------------------------------------------
# 三、守卫：机制性防止「下一条新失败串又漏判」
# ---------------------------------------------------------------------------
# 关键词：出现即「看起来在报告失败」。保守取值（只收强信号）。
_HARD_FAIL = (
    "失败", "错误", "异常", "不可用", "未完成", "超时", "拒绝", "被拦截",
    "无权限", "权限不足", "不支持", "挂了", "无法", "出错", "被拒", "熔断",
    "未启用", "未登录", "已用尽", "风控", "未获授权", "不满足", "已达上限",
    "不在允许列表",
)
# 显式豁免：**不是失败**的形态（空结果 / 成功返回内容）。每条都写明理由。
_EXEMPT = {
    # 工具成功跑通，只是源站没有字幕；这是「无数据」不是「失败」
    "视频《{x}》暂无可用字幕，无法生成总结（可凭标题/封面和你的了解聊聊，别假装看过内容）。",
    # 成功返回了真实字幕文本，只是没走总结模型
    "视频《{x}》字幕（未启用总结模型）：\n{x}",
    # 空结果（"被拒绝名单"是配置项名，不是失败描述）
    "目录 `{x}` 下没有可读条目（空或被拒绝名单全过滤）",
}

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP = {"__pycache__", "_archive", ".venv", "venv", ".git", ".workbuddy",
         "node_modules", "data", "logs", "backups", "dist", "build", ".pytest_cache"}

# 显式补充「不经 registry.register(Tool(...)) 字面量」注册的工厂：
#   * `register_capability_as_tool`（anima/tools/capability.py:81）内部用
#     `make_capability_tool` 建 `_handler` —— 角色自生长能力的动态工具。
#     `capability.py:64` 的 `内部错误：LLM 未就绪` 正是这样漏出审计的。
_EXTRA_HANDLER_FACTORIES = ("register_capability_as_tool",)


def _lit(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        b = io.StringIO()
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                b.write(v.value)
            elif isinstance(v, ast.FormattedValue):
                b.write("{x}")
        return b.getvalue()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, c = _lit(node.left), _lit(node.right)
        if a is not None and c is not None:
            return a + c
    return None


def _index():
    """函数名 → (相对路径, 函数节点)，覆盖全仓（只读）。"""
    idx = {}
    for dp, dns, fns in os.walk(_ROOT):
        dns[:] = [d for d in dns if d not in _SKIP]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            ap = os.path.join(dp, fn)
            rel = os.path.relpath(ap, _ROOT).replace("\\", "/")
            try:
                tree = ast.parse(open(ap, encoding="utf-8", errors="replace").read())
            except Exception:
                continue
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    idx.setdefault(n.name, (rel, n))
    return idx


def _handler_names():
    """解析所有含 registry.register( 的文件，取出 Tool(...) 的 handler 名 +
    ``registry.register(make_xxx(...))`` 形态的工厂名，外加显式补充的工厂。

    取域限制（诚实声明）：只覆盖**静态可解析**的注册点。native plugin / WebUI
    动态注册的 handler 不在域内 —— 它们不是仓库里的字面量，无法静态扫描。
    """
    out = list(_EXTRA_HANDLER_FACTORIES)
    for dp, dns, fns in os.walk(os.path.join(_ROOT, "anima")):
        dns[:] = [d for d in dns if d not in _SKIP]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            ap = os.path.join(dp, fn)
            try:
                txt = open(ap, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            if "registry.register(" not in txt:
                continue
            try:
                tree = ast.parse(txt)
            except Exception:
                continue
            for n in ast.walk(tree):
                # for t in make_xxx_tools(core):
                if isinstance(n, ast.For) and isinstance(n.iter, ast.Call) \
                        and isinstance(n.iter.func, ast.Name) \
                        and n.iter.func.id.startswith("make_"):
                    out.append(n.iter.func.id)
                if not isinstance(n, ast.Call):
                    continue
                if not (isinstance(n.func, ast.Attribute) and n.func.attr == "register"):
                    continue
                if not n.args:
                    continue
                a0 = n.args[0]
                # registry.register(make_code_exec_tool(...)) —— 工厂，其子树含嵌套 handler
                if isinstance(a0, ast.Call) and isinstance(a0.func, ast.Name) \
                        and a0.func.id.startswith("make_"):
                    out.append(a0.func.id)
                    continue
                if not (isinstance(a0, ast.Call) and isinstance(a0.func, ast.Name)
                        and a0.func.id == "Tool"):
                    continue
                h = a0.args[3] if len(a0.args) > 3 else None
                for kw in a0.keywords:
                    if kw.arg == "handler":
                        h = kw.value
                if h is None:
                    continue
                if isinstance(h, ast.Name):
                    out.append(h.id)
                elif isinstance(h, ast.Call):
                    f_ = h.func
                    if isinstance(f_, ast.Name):
                        out.append(f_.id)
                    elif isinstance(f_, ast.Attribute) and h.args \
                            and isinstance(h.args[0], ast.Name):
                        out.append(h.args[0].id)
    return out


def _tool_return_literals():
    idx = _index()
    seen, out = set(), []
    for name in _handler_names():
        if name not in idx:
            continue
        rel, node = idx[name]
        for n in ast.walk(node):
            if isinstance(n, ast.Return) and n.value is not None:
                s = _lit(n.value)
                if s and s.strip():
                    key = (rel, s.strip())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append((rel, n.lineno, s.strip()))
    return out


def test_guard_every_failure_looking_tool_return_is_detected():
    """机制性守卫：任何「看起来在报告失败」的工具返回串都必须被判失败。

    新增 handler 若引入新的失败文案而未注册，本测试会失败 → 强制显式分类
    （要么补进注册表，要么以理由加入 `_EXEMPT`）。
    """
    lits = _tool_return_literals()
    assert len(lits) >= 60, f"取域过窄，扫描失效？（只拿到 {len(lits)} 条）"
    missed = []
    for rel, lineno, text in lits:
        if text in _EXEMPT:
            continue
        if not any(k in text for k in _HARD_FAIL):
            continue
        if tool_result_ok(text) is not False:
            missed.append(f"{rel}:{lineno}  {text[:100]}")
    assert not missed, "以下失败形返回串仍被判成功：\n  " + "\n  ".join(sorted(missed))


def test_guard_scan_domain_is_real():
    """守卫自身的取域自检：必须真的扫到了关键 handler（含动态注册工厂）。"""
    names = set(_handler_names())
    for must in ("tool_calc", "tool_web_search", "make_get_user_relations",
                 "make_group_kick", "agent_exec", "make_sticker_tools",
                 "make_code_exec_tool", "register_capability_as_tool"):
        assert must in names, f"守卫取域漏掉 {must}"
    assert len(_tool_return_literals()) >= 60
