"""出站 Markdown/LaTeX 清洗（2026-09-06 用户体验测试④回归）。"""
from anima.pipeline.stages._shared import _clean_transport_text as clean


def test_bold_and_heading_and_dollar():
    src = "### 标题\n当前**英仙座流星雨**，天顶流量 $ZHR$ 约 100"
    out = clean(src)
    assert "**" not in out and "$" not in out
    assert out.startswith("标题")
    assert "英仙座流星雨" in out and "ZHR" in out


def test_latex_commands_readable():
    src = r"和为 **500500**，即 $\frac{n(n+1)}{2}$，误差 $\pm 0$，约 $\approx 100$"
    out = clean(src)
    assert "**" not in out and "$" not in out and r"\frac" not in out
    assert "(n(n+1))/(2)" in out
    assert "±" in out and "≈" in out


def test_power_operator_preserved():
    """不成对的 **（幂运算）不能被误删"""
    assert "2 ** 10" in clean("2 ** 10 等于 1024")
    assert clean("a ** b") == "a ** b"


def test_cq_and_whitespace_unchanged():
    assert clean("[CQ:at,qq=1]你好") == "你好"
