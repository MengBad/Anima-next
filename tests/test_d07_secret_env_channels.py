# -*- coding: utf-8 -*-
"""D-07 密钥外置：补齐最后两处无 env 通道的密钥（2026-10-17）。

背景：`deploy/check_config_drift.py` 第 4 节判「生产 config.yaml 明文密钥 = 0 处」。
实测生产有 6 处明文：
    llm.api_key · tavily.api_key · vector.embedding_api_key · vector.rerank_api_key
    **webui.token** · **x_browser.password**

前 4 处 `load_config` 已有 env 通道（`ANIMA_LLM_API_KEY` 等，见 `config.py:1201-1212`），
值已落在 systemd `EnvironmentFile=/opt/anima/.secrets`。**后 2 处没有通道** ⇒ 只能明文落盘。

本测试钉死新增的两条通道，并回归既有 4 条（防手滑删掉）。
"""
from __future__ import annotations

import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anima.core.config import load_config  # noqa: E402

#: (env 名, 扁平键路径, 取值函数)
CHANNELS = [
    ("ANIMA_LLM_API_KEY", "llm.api_key"),
    ("ANIMA_EMBEDDING_API_KEY", "vector.embedding_api_key"),
    ("ANIMA_RERANK_API_KEY", "vector.rerank_api_key"),
    ("ANIMA_TAVILY_API_KEY", "tavily.api_key"),
    ("ANIMA_WEBUI_TOKEN", "webui.token"),                      # 2026-10-17 新增
    ("ANIMA_X_BROWSER_PASSWORD", "x_browser.password"),        # 2026-10-17 新增
]

_EMPTY_YAML = textwrap.dedent("""
    llm:
      api_key: ""
    vector:
      embedding_api_key: ""
      rerank_api_key: ""
    tavily:
      api_key: ""
    webui:
      token: ""
    x_browser:
      password: ""
""")


def _cfg(tmp_path, monkeypatch, env_name, value):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(_EMPTY_YAML, encoding="utf-8")
    monkeypatch.setenv(env_name, value)
    return load_config(str(cfg_file))


def _read(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        node = getattr(node, part)
    # SecretStr 兼容
    if hasattr(node, "get_secret_value"):
        return node.get_secret_value()
    return node


@pytest.mark.parametrize("env_name,dotted", CHANNELS)
def test_secret_env_channel_overrides_config(tmp_path, monkeypatch, env_name, dotted):
    """每条通道都必须能把值注入配置（config.yaml 留空也不影响运行）。"""
    secret = "S3CR3T-%s" % env_name
    cfg = _cfg(tmp_path, monkeypatch, env_name, secret)
    assert _read(cfg, dotted) == secret, (
        "%s 未从环境变量注入 %s ⇒ 该密钥只能明文落 config.yaml（D-07）"
        % (env_name, dotted)
    )


def test_no_env_means_empty_no_crash(tmp_path, monkeypatch):
    """未设 env 时不得抛异常（值留空 = 空串/None）。"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(_EMPTY_YAML, encoding="utf-8")
    for env_name, _ in CHANNELS:
        monkeypatch.delenv(env_name, raising=False)
    cfg = load_config(str(cfg_file))
    # 不抛异常即可；空值语义由各自消费者决定
    assert cfg is not None


def test_two_new_channels_documented():
    """元守卫：源码里必须同时存在两条新通道（防被回退）。"""
    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "anima", "core", "config.py"),
        encoding="utf-8",
    ).read()
    for name in ("ANIMA_WEBUI_TOKEN", "ANIMA_X_BROWSER_PASSWORD"):
        assert name in src, "缺少 env 通道 %s（D-07 会复发）" % name
