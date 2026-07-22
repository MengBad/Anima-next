"""回归测试：rumination._extract_json 对 LLM 噪声 JSON 的容错。"""
from anima.cognitive.rumination import _extract_json


def test_extract_json_clean():
    assert _extract_json('{"need": false}') == {"need": False}


def test_extract_json_with_fence():
    t = '```json\n{"need": true, "name": "x"}\n```'
    assert _extract_json(t) == {"need": True, "name": "x"}


def test_extract_json_with_surrounding_text():
    t = '好的，这是结果：{"need": true, "name": "认人小雷达"} 完毕'
    assert _extract_json(t) == {"need": True, "name": "认人小雷达"}


def test_extract_json_unbalanced_braces():
    # 缺闭合括号：截取首个 { 到末个 } 后仍失败 → None
    assert _extract_json('{"need": true, "name": "x"') is None


def test_extract_json_garbage():
    assert _extract_json("完全不是 json 的随便说说") is None
    assert _extract_json("") is None
    assert _extract_json(None) is None
