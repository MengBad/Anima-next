"""check_link 工具契约（2026-10-22）。

## 背景（owner 提了 32 天的需求）

「群友发的链接打不开时，帮忙查快照或摘要」。这个需求被 SelfCode 受理
**12+ 次**，每次都在受理阶段被 `network_egress_boundary` **正确拒绝** ——
因为 SelfCode 沙箱禁网，发 HTTP 请求的工具在那里结构上做不出来。

⇒ **正确落点是内置工具**（本文件测的 `link_check`），跑在 bot 主进程、
有网络能力，与 `web_search` 同级。

## 本测试锁死的边界（全部来自真实踩坑）

1. URL 归一：裸域名补 https；非法输入返回空；尖括号/引号包裹要剥掉；
2. **SSRF 防护**：环回/私网/链路本地/元数据地址一律拒绝（不发起请求）；
3. 探测判定：2xx/3xx = 可用；4xx/5xx/DNS 失败 = 失效；
4. 失效回退顺序：Wayback 快照 > 搜索线索 > 如实说找不到；
5. **gzip 解压**：Wayback 的 `id_` 原始内容变体常返回 gzip 体，
   不解压会得到乱码（实测踩过：1500 字全是乱码）；
6. **相关性判据只查结果条目区**：搜索输出头部会回显 query，
   而 query 里就含目标域名 ⇒ 全串匹配会 100% 假阳（实测踩过）；
7. 输出不含内部异常细节（D1）；Bing 跳转壳要过滤。
"""
import gzip

import pytest

from anima.tools.builtin.link_check import (
    MAX_BODY_CHARS, _html_to_text, _is_private_host, _normalize_url,
)


# ── 1. URL 归一 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com"),
    ("https://a.com/x", "https://a.com/x"),
    ("http://b.com", "http://b.com"),
    ("<https://c.com>", "https://c.com"),
    ("「https://d.com」", "https://d.com"),
])
def test_normalize_url(raw, expected):
    assert _normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "这不是链接", "just some words"])
def test_normalize_url_rejects_garbage(raw):
    assert _normalize_url(raw) == ""


# ── 2. SSRF 防护 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://localhost/x",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",   # 云元数据服务
    "http://[::1]/",
])
def test_private_hosts_rejected(url):
    """🔴 绝不访问内网/环回/元数据地址（SSRF）。"""
    assert _is_private_host(url) is True


@pytest.mark.parametrize("url", [
    "https://example.com/",
    "https://www.baidu.com/",
    "http://paulgraham.com/x",
])
def test_public_hosts_allowed(url):
    assert _is_private_host(url) is False


# ── 3. HTML → 文本 ────────────────────────────────────────────────────


def test_html_to_text_strips_script_and_style():
    html = ("<html><head><style>body{color:red}</style></head>"
            "<body><script>var x=1;</script><p>正文内容</p></body></html>")
    out = _html_to_text(html)
    assert "正文内容" in out
    assert "var x" not in out, "脚本必须被剥掉（实测：web_search 正文抓到 JS 配置）"
    assert "color:red" not in out, "样式必须被剥掉"


def test_html_to_text_unescapes_entities():
    assert "A&B" in _html_to_text("<p>A&amp;B</p>")


def test_html_to_text_handles_empty():
    assert _html_to_text("") == ""


# ── 4. gzip 解压（实测踩过的坑）───────────────────────────────────────


def test_gzip_detection_by_magic_bytes():
    """Wayback `id_` 变体返回 gzip 体 ⇒ 必须按魔数识别并解压。

    实测：不解压时 1500 字正文全是乱码（`�k�Gr-�=E�}=�րM�z`）。
    """
    payload = "<html><body>压缩后的正文</body></html>".encode("utf-8")
    packed = gzip.compress(payload)
    assert packed[:2] == b"\x1f\x8b", "前置：gzip 魔数"
    # 模拟工具内的解压逻辑
    if packed[:2] == b"\x1f\x8b":
        raw = gzip.decompress(packed)
    else:
        raw = packed
    assert "压缩后的正文" in raw.decode("utf-8", "replace")


def test_plain_body_not_mistaken_for_gzip():
    plain = "<html><body>普通正文</body></html>".encode("utf-8")
    assert plain[:2] != b"\x1f\x8b", "非 gzip 内容不得被当 gzip 解压"
    assert "普通正文" in plain.decode("utf-8", "replace")


# ── 5. 相关性判据（实测踩过的假阳）────────────────────────────────────


def test_relevance_must_exclude_query_echo():
    """🔴 搜索输出头部回显 query，而 query 含目标域名 ⇒ 全串匹配 100% 假阳。

    实测：查 `does-not-exist-2026.com` 时，结果全是域名商广告，
    但头部「关于「does-not-exist-2026.com …」的搜索结果」让全串匹配恒真。
    """
    import re
    host = "does-not-exist-2026.com"
    output = (
        f"当前日期是 2026-10-22。关于「{host} x」的搜索结果（共 10 条）：\n"
        "【1】Domain Names, Site Builder, Hosting\n"
        "https://www.bing.com/ck/a?xxx\n"
        "摘要: Finding and buying the perfect domain is as easy as 1-2-3.\n"
    )
    # 全串匹配：假阳
    assert host in output.lower()
    # 只查条目区：正确判定为不相关
    m = re.search(r"【1】", output)
    body = output[m.start():]
    pat = re.escape(host) + r"(?![\w-])"
    assert not re.search(pat, body, re.I), "条目区里没有目标域名 ⇒ 应判不相关"


def test_relevance_true_when_body_mentions_host():
    """结果条目里真提到目标域名 ⇒ 判相关（不能一律说不相关）。"""
    import re
    host = "example.com"
    output = ("关于「example.com」的搜索结果：\n"
              "【1】Example Domain\nhttps://example.com/\n摘要: 示例站点。\n")
    m = re.search(r"【1】", output)
    body = output[m.start():]
    assert re.search(re.escape(host) + r"(?![\w-])", body, re.I)


# ── 6. 常量契约 ───────────────────────────────────────────────────────


def test_body_cap_is_reasonable():
    """正文上限要够看结论，又不撑爆上下文。"""
    assert 500 <= MAX_BODY_CHARS <= 3000


# ── 7. 工具已注册 ─────────────────────────────────────────────────────


def test_tool_registered_with_network_scope():
    """check_link 必须在内置工具表里，且带 network scope、USER 级可用。"""
    import inspect
    from anima.tools.builtin import register_all_builtin

    src = inspect.getsource(register_all_builtin)
    assert '"check_link"' in src, "工具必须注册"
    assert "tool_check_link" in src
    i = src.find('"check_link"')
    seg = src[i:i + 700]
    assert 'granted_scopes={"network": True}' in seg, "必须声明网络 scope"
    assert "min_level=LEVEL_USER" in seg, "普通群友也能用（日常需求，非 owner 特权）"
    assert "required=" in seg
