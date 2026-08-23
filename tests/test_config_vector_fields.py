# -*- coding: utf-8 -*-
"""config_service：WebUI 新增 vector/embedding/rerank 可编辑字段。

验证：
- MANIFEST 暴露 vector.* 字段（端点/模型/维度/开关/两个 secret key）；
- validate 对 vector.embedding_api_key / rerank_api_key 的「留空 / configured 对象」
  都按保持原值处理，不写回、不触发校验失败；
- change-set 的 restart_required 随 vector 端点变更正确置位。
"""
from __future__ import annotations

import pytest

from anima.core.config_service import MANIFEST, _masked, _plain


def _mk_core():
    from anima.core.config_service import ConfigService
    from anima.core.anima_core import AnimaCore
    import tempfile
    from pathlib import Path
    # 用一个最小 stub，按需提供 config / config_path / persistence.data_dir
    return None


class _Stub:
    def __init__(self):
        from anima.core.config import AnimaConfig
        self.config: AnimaConfig = AnimaConfig()
        self.config_path = None
        class Pers:
            data_dir = None
            def read_json(self, name, default):
                return {} if name == "config_changes.json" else default
        self.persistence = Pers()


class _Core:
    def __init__(self):
        from anima.core.config_service import ConfigService
        # 用一个 stub config + 本地单例即可
        self.service = ConfigService(_Stub()) if False else None


# 直接测 MANIFEST：不依赖 core
def test_manifest_exposes_vector_fields():
    vec = [f for f in MANIFEST if f["path"].startswith("vector.")]
    paths = {f["path"] for f in vec}
    assert "vector.embedding_endpoint" in paths
    assert "vector.embedding_model" in paths
    assert "vector.embedding_dim" in paths
    assert "vector.embedding_api_key" in paths
    assert "vector.rerank_endpoint" in paths
    assert "vector.rerank_model" in paths
    assert "vector.rerank_api_key" in paths
    assert "vector.enabled" in paths
    assert "vector.rerank_enabled" in paths
    # 端点/密钥这类变更应要求受控重启
    for p in ("vector.embedding_endpoint", "vector.embedding_api_key", "vector.embedding_model"):
        f = next(x for x in vec if x["path"] == p)
        assert f["restart"] is True, f"{p} 应要求重启"


def test_vector_secrets_masked_in_state_shape():
    # _masked 应对 vector key 输出 configured 布尔，不泄露明文
    out = _masked({"vector": {"embedding_api_key": "sk-xyz", "rerank_api_key": ""}})
    v = out["vector"]
    assert v["embedding_api_key"] == {"configured": True}
    assert v["rerank_api_key"] == {"configured": False}


def test_validate_keeps_vector_keys_when_empty():
    from unittest.mock import patch as mpatch
    from anima.core.config_service import ConfigService

    st = _Stub()
    svc = ConfigService(st)
    # 当前已有 key（SecretStr 在这里以明文出现于 model 内部；validate 里走 _raw_state）
    st.config.vector.embedding_api_key = "sk-existing-embed"  # noqa
    st.config.vector.rerank_api_key = "sk-existing-rerank"   # noqa
    with mpatch.object(svc, "_raw_state", return_value={
        "vector": {
            "embedding_endpoint": "https://api.siliconflow.cn/v1",
            "embedding_model": "BAAI/bge-m3",
            "embedding_api_key": "sk-existing-embed",
            "rerank_enabled": True,
            "rerank_endpoint": "https://api.siliconflow.cn/v1/rerank",
            "rerank_model": "BAAI/bge-reranker-v2-m3",
            "rerank_api_key": "sk-existing-rerank",
        },
    }):
        # 补丁里密钥以 WebUI 的 configured 对象/空串出现 -> 应保持原值
        after = svc.validate({
            "vector": {
                "embedding_model": "BAAI/bge-m3",
                "embedding_api_key": {"configured": True},
                "rerank_model": "BAAI/bge-reranker-v2-m3",
                "rerank_api_key": "",
            },
        })
    v = after["vector"]
    assert v["embedding_model"] == "BAAI/bge-m3"
    # 密钥保持原有明文（加载时是 str）
    assert v["embedding_api_key"] == "sk-existing-embed"
    assert v["rerank_api_key"] == "sk-existing-rerank"


def test_impact_includes_restart_for_vector_change():
    from anima.core.config_service import ConfigService
    svc = ConfigService(_Stub())
    impact, restart, risk = svc._impact({"vector": {"embedding_endpoint": "https://x"}})
    assert restart is True
    assert "语义检索" in impact
