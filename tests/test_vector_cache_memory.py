"""向量缓存的内存表示守卫（2026-10-16 定位）。

缺陷（生产实测）：
    ``Embedder._cache: dict[str, list[float]]`` —— ``_decode_vec()`` 返回
    ``list(arr)``，把 float32 解成 **1024 个独立的 Python float 对象**。

    实测单条开销（生产真实 1024 维数据）::

        list[float] = 32,824 B
        array('f')  =  4,444 B        ⇒ 7.4x

    生产 6361 条 ⇒ ``list`` 199.1MB vs ``array`` 27.0MB ⇒ **可省 172MB**。
    逐阶段实测确认 ``_init_vector_memory()`` 一步吃掉 299.8MB，
    占进程 660MB 的 45%，是最大单一成因。

修法：内部存 ``array('f')``，``embed()`` **出口转 list** ⇒ 消费方零改动。

🔴 为什么出口必须转 list：``VectorMemory.add()`` 与 ``search()`` 里有
``json.dumps(vec)``，而 **``json.dumps`` 不接受 array**。
"""
from __future__ import annotations

import array
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anima.memory.vector_memory import Embedder  # noqa: E402

DIM = 1024
TEXTS = ["记忆一", "记忆二", "缓存命中测试"]


def _vec(seed: float) -> list[float]:
    return [seed + i * 1e-6 for i in range(DIM)]


def _cache_payload() -> dict:
    """构造 v2（base64）格式的缓存负载。"""
    out = {}
    for i, t in enumerate(TEXTS):
        arr = array.array("f", _vec(float(i)))
        out[Embedder._hash(t)] = base64.b64encode(arr.tobytes()).decode("ascii")
    return out


@pytest.fixture()
def embedder(tmp_path):
    (tmp_path / "embedding_cache.json").write_text(
        json.dumps(_cache_payload()), encoding="utf-8")
    # endpoint 指向不可达地址：本组用例只走缓存命中路径，绝不联网
    return Embedder(endpoint="http://127.0.0.1:1/v1", api_key="test",
                    data_dir=str(tmp_path))


def test_cache_stores_array_not_list(embedder):
    """核心判据：缓存内部值必须是 array('f')，不得是 list[float]。

    list[float] 在 1024 维下是 32.8KB/条，array 是 4.4KB/条（7.4x）。
    """
    assert embedder._cache, "缓存未加载"
    bad = {k: type(v).__name__ for k, v in embedder._cache.items()
           if not isinstance(v, array.array)}
    assert not bad, (
        "缓存内部仍存 list[float] ⇒ 内存放大 7.4x（生产 6361 条 ≈ 199MB）。"
        "问题条目样例：%r" % dict(list(bad.items())[:3])
    )


def test_cache_array_is_float32(embedder):
    """必须是 'f'（float32）—— 与 sqlite-vec `float[1024]` 同精度。"""
    typecodes = {v.typecode for v in embedder._cache.values()}
    assert typecodes == {"f"}, "typecode 应为 'f'(float32)，实际 %r" % typecodes


@pytest.mark.asyncio
async def test_embed_hit_returns_plain_list(embedder):
    """出口契约：embed() 必须返回 list[float]。

    `json.dumps(array)` 会抛 TypeError，而 add()/search() 都依赖 json.dumps。
    """
    for t in TEXTS:
        vec = await embedder.embed(t)
        assert vec is not None, "缓存命中失败：%r" % t
        assert isinstance(vec, list), (
            "embed() 返回了 %s —— json.dumps 不接受 array，会打断 add()/search()"
            % type(vec).__name__
        )
        assert len(vec) == DIM
        assert isinstance(vec[0], float)


@pytest.mark.asyncio
async def test_embed_hit_does_not_share_mutable_state(embedder):
    """出口应是**新** list：调用方改返回值不得污染缓存。"""
    t = TEXTS[0]
    vec = await embedder.embed(t)
    vec[0] = 999.0
    again = await embedder.embed(t)
    assert again[0] != 999.0, "embed() 返回了缓存内部对象 ⇒ 调用方可污染缓存"


def test_decoded_vec_is_compact(embedder):
    """单条内存必须显著小于 list 表示（防回退）。"""
    one = next(iter(embedder._cache.values()))
    compact = sys.getsizeof(one)
    as_list = list(one)
    naive = sys.getsizeof(as_list) + sum(sys.getsizeof(x) for x in as_list)
    assert naive / compact > 5.0, (
        "array 相对 list 的收益异常（%.1fx）—— 判据或实现有问题" % (naive / compact)
    )


def test_encode_decode_roundtrip(embedder):
    """编解码往返必须保真（array 化不得改数值语义）。"""
    for t in TEXTS:
        key = Embedder._hash(t)
        arr = embedder._cache[key]
        raw = embedder._encode_vec(arr)
        back = Embedder._decode_vec(raw)
        assert isinstance(back, array.array)
        assert list(back) == list(arr), "往返不保真：%r" % t


def test_v1_legacy_list_payload_still_loads(tmp_path):
    """v1（裸 list）负载必须仍能加载 —— 升级不丢历史缓存。"""
    payload = {Embedder._hash("老格式"): _vec(0.5)}
    (tmp_path / "embedding_cache.json").write_text(
        json.dumps(payload), encoding="utf-8")
    emb = Embedder(endpoint="http://127.0.0.1:1/v1", api_key="t",
                   data_dir=str(tmp_path))
    assert len(emb._cache) == 1, "v1 负载被丢弃"
    assert isinstance(next(iter(emb._cache.values())), array.array)
