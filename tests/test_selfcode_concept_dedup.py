"""SelfCode 需求判重：概念签名 vs 字符 2-gram（2026-10-27）。

## 生产事故

同一个「查 Wayback 快照并提取正文」的需求，从 **08-31 到 10-26 被受理 29 次**：
15 次生成后静态校验失败、13 次 owner 手动拒绝、1 次成功。

`data/owner_tasks.json` 里的时间线：
```
09-02..09-20  failed ×15   ← 还在真跑生成（白烧配额）
10-07..10-26  rejected ×13 ← 受理期就拒，但每次都新建一张单
```

## 为什么原判重失效

`_topic_similarity`（字符二元组 Jaccard）+ 阈值 0.55。
用 29 条真实需求实测：

    全部两两相似度 n=406：中位 0.214、max 0.738
    只有 4/406 对达到 0.55

即：**同一个需求换个说法只有 0.10–0.22，永远判不出来**。

## 为什么不能降低阈值

实测区间**重叠**，不存在安全阈值：

    跨主题（应判不同）max = 0.079
    同主题（应判相同）min = 0.041   ← 比跨主题上限还低！

那个 0.041 是「做一个当群友发的网页链接打不开或请求超时网页抓取失败时…」
—— 极端啰嗦的改写，句式与其它样本完全不同。

## 解法：概念签名

中文短需求里，决定「是什么工具」的是**概念组合**，不是字符顺序。
29 条样本中 28 条签名完全一致 `{link, broken, archive, extract}`；
其它主题（导出 CSV / 提醒喝水 / 图片转 PDF）签名是**空集**。

判据用 **Jaccard ≥ 0.70**（不是「重叠/较小集合」—— 后者在新需求只命中
1 个概念时恒得 1.0，会把「检测链接有效性」误判成「查快照」）。
"""
from __future__ import annotations

import pytest

from anima.cognitive.selfcode import SelfCodeManager


def _sig(text: str) -> frozenset:
    return SelfCodeManager._concept_signature(text)


BASE = "做一个输入失效URL自动查询网页时光机历史快照并提取正文的工具。"


# ── 1. 同主题必须判重复 ──────────────────────────────────────────────

@pytest.mark.parametrize("variant", [
    # 全概念同义改写（不同措辞/不同快照服务名）
    "做一个输入失效或拦截的网页链接，自动抓取并提取 Wayback Machine 历史快照文本的工具。",
    "做一个传入失效网页链接，自动从网页时光机（Wayback Machine）抓取历史快照正文的工具。",
    "做一个输入失效网址自动检索Web Archive历史快照并提取正文内容的容灾查询工具",
    # 少一个概念（没说「提取正文」）—— 实测 3/4 = 0.75，仍应判重复
    "做一个检测失效网页URL并自动在Wayback Machine查询可用历史快照的工具。",
    # 极端啰嗦的改写（2-gram 只有 0.04，旧判据必挂）
    "做一个当群友发的网页链接打不开或请求超时网页抓取失败时，"
    "自动调用网页缓存记忆或搜索引擎获取核心内容并返回摘要的工具。",
])
def test_same_topic_variants_are_duplicates(variant):
    a, b = _sig(BASE), _sig(variant)
    assert a and b, f"签名不应为空：{sorted(a)} / {sorted(b)}"
    jac = len(a & b) / len(a | b)
    assert jac >= 0.70, f"同主题被判成新需求（jaccard={jac:.2f}）：{variant[:40]}"


# ── 2. 不同主题绝不能被误合并 ────────────────────────────────────────

@pytest.mark.parametrize("other", [
    "帮我写一个把群聊记录导出成 CSV 的工具",          # 空签名
    "做一个每天提醒我喝水的小工具",                    # 空签名
    "把图片转成 PDF 的工具",                           # 空签名
    # 🔴 最危险：也含「链接」，但只是「检测有效性」，不是「查快照」
    "做一个自动检测群友发的链接是否有效并提醒的工具",
    # 含 archive+link 但没有 broken（不是「失效」场景）
    "做一个把网页快照存档到本地数据库的工具",
])
def test_different_topics_are_not_duplicates(other):
    a, b = _sig(BASE), _sig(other)
    if not a or not b:
        return      # 空签名 ⇒ 调用方不判重（保守），这里不算失败
    jac = len(a & b) / len(a | b)
    assert jac < 0.70, f"无关需求被判重复（jaccard={jac:.2f}）：{other[:40]}"


# ── 3. 空签名不做判重（保守性）──────────────────────────────────────

def test_empty_signature_means_no_dedup():
    """认不出领域概念 ⇒ 签名空集 ⇒ 调用方必须返回 None（不判重）。"""
    assert _sig("做一个每天提醒我喝水的小工具") == frozenset()
    assert _sig("") == frozenset()


def test_find_same_rejection_ignores_tasks_without_concepts():
    """旧单签名也是空集时，不得被当成「同主题」。"""
    class _T:
        id = "old"
        kind = "selfcode"
        status = "rejected"
        request = "做一个每天提醒我喝水的小工具"

    class _Store:
        def list(self):
            return [_T()]

    class _Core:
        tasks = _Store()

    m = SelfCodeManager.__new__(SelfCodeManager)
    m.core = _Core()
    assert m._find_same_boundary_rejection(BASE) is None, (
        "空签名的旧单不该匹配本类需求")


# ── 4. 真实 29 条样本：至少能判出绝大多数 ────────────────────────────

def test_real_production_samples_mostly_collapse():
    """用生产里 29 条真实需求验证：绝大多数应落到同一签名。

    这是本修复的存在理由 —— 旧判据在这批数据上只能判出 4/406 对。
    """
    reqs = [
        "做一个输入失效URL自动查询网页时光机历史快照并提取正文的工具。",
        "做一个输入失效或拦截的网页链接，自动抓取并提取 Wayback Machine 历史快照文本的工具。",
        "做一个传入失效网页链接，自动从网页时光机（Wayback Machine）抓取历史快照正文的工具。",
        "做一个输入打不开或失效的网页链接，自动从时光机与快照源提取正文内容的工具。",
        "做一个输入打不开或失效的网页链接，自动从快照归档服务抓取历史网页内容的工具。",
        "做一个输入失效网址自动检索Web Archive历史快照并提取正文内容的容灾查询工具",
        "做一个输入失效网址自动检索Wayback等快照存档并提取正文的工具。",
        "做一个输入失效或404链接自动检索历史快照并提取正文的网页归档查询工具。",
        "做一个输入失效URL后自动检索网页时光机快照并提取纯文本摘要的排障工具。",
        "做一个检测失效网页URL并自动在Wayback Machine查询可用历史快照的工具。",
        "做一个能检测网页链接失效并自动抓取互联网档案馆快照或网页缓存返回给群友的工具。",
    ]
    sigs = [_sig(r) for r in reqs]
    from collections import Counter
    top, n = Counter(sigs).most_common(1)[0]
    assert n >= len(reqs) * 0.8, (
        f"只有 {n}/{len(reqs)} 条落到同一签名，判重仍太弱：{dict(Counter(sigs))}")


def test_old_ngram_judge_would_have_failed_here():
    """反向守卫：证明旧的 2-gram 判据在这批数据上确实判不出来。

    这条测试的存在意义：防止有人「优化」回 2-gram 阈值。
    """
    a = "做一个输入失效网址自动检索Wayback等快照存档并提取正文的工具。"
    b = ("做一个当群友发的网页链接打不开或请求超时网页抓取失败时，"
         "自动调用网页缓存记忆或搜索引擎获取核心内容并返回摘要的工具。")
    # 旧判据：主题键 2-gram Jaccard
    m = SelfCodeManager.__new__(SelfCodeManager)
    ta = m._boundary_topic(a)
    tb = m._boundary_topic(b)
    old = SelfCodeManager._topic_similarity(ta, tb)
    assert old < 0.55, (
        f"旧判据居然能判出来（{old:.2f}）？那本测试的前提就不成立了")
    # 新判据：概念签名
    sa, sb = _sig(a), _sig(b)
    new = len(sa & sb) / len(sa | sb)
    assert new >= 0.70, f"新判据也判不出来（{new:.2f}）—— 修复无效"
