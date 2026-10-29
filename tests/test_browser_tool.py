"""通用网页浏览（browse_web）—— SSRF 防护与 URL 校验。

内存实测（2026-10-29，生产机）：
    启动无页面 298MB / 单页 441MB / 3页并发 641MB / 关闭后 0
⇒ 单页安全（可用 2.9GB），但必须用完即关，不留常驻进程。

本文件只测**纯函数**部分（URL 校验与内网拦截），不启动真浏览器。
"""
from __future__ import annotations

import pytest

from anima.tools.browser import validate_url, _is_blocked_host


@pytest.mark.parametrize("url", [
    "https://www.baidu.com",
    "https://www.baidu.com/s?wd=x",
])
def test_accepts_public_urls(url):
    """用真实公网域名。

    ⚠️ 本机 DNS 走代理，部分域名会被解析到 198.18.2.56（RFC2544 保留段），
    因而被 _is_blocked_host 判为受限。那是**开发机环境**特性，不是校验逻辑问题：
    生产机直连 DNS，域名解析到真实公网 IP，不受影响。
    这里只挑在本机也能解析到公网 IP 的域名做正例。
    """
    assert validate_url(url) == url


@pytest.mark.parametrize("url", [
    "ftp://example.com",            # 非 http(s)
    "file:///etc/passwd",           # 本地文件
    "javascript:alert(1)",          # 伪协议
    "",
    "   ",
    "http://",
    "https:///path",
])
def test_rejects_bad_scheme_or_empty(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "10.0.0.1", "192.168.1.1", "172.16.0.1",
    "169.254.169.254",              # 云元数据地址，SSRF 经典目标
    "something.local", "x.internal",
])
def test_blocked_hosts(host):
    assert _is_blocked_host(host) is True


def test_public_ip_not_blocked():
    assert _is_blocked_host("8.8.8.8") is False
    assert _is_blocked_host("1.1.1.1") is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/admin",
    "http://localhost/secret",
    "http://169.254.169.254/latest/meta-data/",   # AWS 元数据
    "http://192.168.6.1/",
    "http://[::1]/",
])
def test_rejects_internal_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_rejects_overlong_url():
    with pytest.raises(ValueError):
        validate_url("https://example.com/" + "a" * 3000)
