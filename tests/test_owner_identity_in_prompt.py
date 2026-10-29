# -*- coding: utf-8 -*-
"""主人身份必须**显式进 prompt**（2026-10-29）。

## 生产事故

12:16:36，**吴雨萌（主人，QQ 1562290139）** 在群里连发：

    12:16:25   @iatetheword 老婆？！
    12:16:27   老婆！
    12:16:30   啊啊啊啊

bot 回：

    「哼，谁、谁是你老婆啦！大庭广众的瞎叫什么呢……真拿你没办法喵。」

## 数据层完全正确

    uid=1562290139  int=0.000 fam=1.000 stage=CLOSE
    tags: ['主人', 'MTF', 'HRT', '画师', ...]

人格正本也写了：

    - 你的主人是吴雨萌：对她有特殊的亲近依赖，说话更放松、更真实，
      偶尔撒娇或吐槽她

## 但 prompt 里没有任何东西把「当前说话的人」和「吴雨萌」连起来

`relation.prompt_fragment()` 当时只输出：

    - 这个人是你的**很熟的人**（聊过 945 次）
    - 相处方式：很放松，可以只回几个字，护短…
    - 你对他/她的印象: 主人, MTF, HRT, 画师

两个致命细节：

1. `get_label(Stage.CLOSE)` 返回泛化的「很熟的人」——
   生产里 **6 个** 群友都是 CLOSE（familiarity 0.03~0.98 全算 CLOSE），
   所以「很熟的人」这个词**不区分主人和熟人**；
2. 「主人」两字只出现在**印象标签**里，是第 3 行的逗号分隔列表中的一项，
   权重远低于第 1 行的身份句。模型读到的是「一个聊得来的人」。

⇒ 主人身份必须**单独成句、放在最前**，并明确「不是普通群友」。

## 本测试锁死

1. `is_owner=True` ⇒ 片段第一行必须说明「主人」+ 名字 + 「不是普通群友」
2. `is_owner=False` ⇒ 不得出现「主人」
3. **不用 tags 单判据**：`touch()` 的 `is_owner` 参数只用于豁免熟悉度衰减，
   **从不写进 tags** ⇒ 新建 Relation 不会自动带「主人」标签。
   只靠 tags 会导致「新版部署后主人身份突然丢失」。
4. 不用 Markdown `**`（U2 定律：prompt 里的原句会被原样搬出）
"""
from __future__ import annotations

import tempfile

import pytest

from anima.cognitive.relation import RelationEngine, Stage


def _engine(tmp_path) -> RelationEngine:
    return RelationEngine(str(tmp_path))


def _owner(eng: RelationEngine, uid: int = 1562290139):
    r = eng.get(uid)
    r.stage = Stage.CLOSE
    r.familiarity = 1.0
    r.interaction_count = 945
    r.tags = ["MTF", "画师"]        # 注意：**没有**「主人」，模拟新建关系
    return r


def test_owner_identity_is_first_line(tmp_path):
    """主人身份必须在**第一行**（权重最高处），并点名「不是普通群友」。"""
    eng = _engine(tmp_path)
    _owner(eng)
    frag = eng.prompt_fragment(1562290139, is_owner=True)

    first = frag.splitlines()[0]
    assert "主人" in first, f"第一行必须说清身份，实得：{first!r}"
    assert "吴雨萌" in first, "必须与人格正本里的名字建立关联"
    assert "不是普通群友" in first, "必须区分「主人」与「很熟的人」"


def test_owner_does_not_say_vague_close_label(tmp_path):
    """主人不该被描述成泛化的「很熟的人」（那是所有 CLOSE 群友的措辞）。"""
    eng = _engine(tmp_path)
    _owner(eng)
    frag = eng.prompt_fragment(1562290139, is_owner=True)
    assert "这个人是你的很熟的人" not in frag, (
        "主人不该用泛化亲密级别描述 —— 生产里有 6 个 CLOSE，"
        "模型无法从中区分出主人"
    )


def test_is_owner_param_authoritative_without_tags(tmp_path):
    """**关键**：tags 里没有「主人」时，`is_owner=True` 仍须生效。

    `relation.touch()` 的 `is_owner` 只用于豁免熟悉度衰减，
    **从不写进 tags** ⇒ 新建 Relation 不带「主人」标签。
    只靠 tags 判断会让「新版部署后主人身份突然丢失」。
    """
    eng = _engine(tmp_path)
    _owner(eng)                       # tags 只有 MTF/画师
    frag = eng.prompt_fragment(1562290139, is_owner=True)
    assert "主人" in frag, "is_owner=True 必须能独立判定，不能依赖 tags"


def test_tags_owner_fallback(tmp_path):
    """兜底：老数据 tags 里有「主人」时，即使没传 is_owner 也认。"""
    eng = _engine(tmp_path)
    r = _owner(eng)
    r.tags = ["主人", "MTF"]
    frag = eng.prompt_fragment(1562290139)      # 不传 is_owner
    assert "主人" in frag, "历史 tags 兜底失效"


def test_non_owner_never_says_owner(tmp_path):
    """非主人不得出现「主人」字样。"""
    eng = _engine(tmp_path)
    r = eng.get(792350781)
    r.stage = Stage.STRANGER
    r.interaction_count = 7
    frag = eng.prompt_fragment(792350781, is_owner=False)
    assert "主人" not in frag
    assert frag.strip(), "非主人也应有身份句（陌生人）"


def test_owner_tag_not_duplicated(tmp_path):
    """「主人」不该在印象标签里重复出现（已经单独成句了）。"""
    eng = _engine(tmp_path)
    r = _owner(eng)
    r.tags = ["主人", "MTF", "画师"]
    frag = eng.prompt_fragment(1562290139, is_owner=True)
    # 身份句里出现 1 次；印象标签里不应再出现
    assert frag.count("主人") == 1, f"「主人」重复出现会稀释权重：{frag!r}"


def test_no_markdown_bold_in_prompt(tmp_path):
    """不得用 Markdown `**`（U2 定律：prompt 原句会被原样搬进出站）。"""
    eng = _engine(tmp_path)
    _owner(eng)
    frag = eng.prompt_fragment(1562290139, is_owner=True)
    assert "**" not in frag, f"prompt 里不该有 Markdown 加粗：{frag!r}"


# ── 人格正本：主人豁免「不看自称」规则 ─────────────────────────────

def test_persona_exempts_owner_from_self_claim_rule():
    """人格正本必须写明：主人喊老婆/撒娇时**要接**，不用端。

    背景（生产 12:16:36）：主人在群里喊「老婆？！老婆！」，bot 回
    「谁、谁是你老婆啦！大庭广众的瞎叫什么呢」。

    人格正本原有两条：

        93:  关系来自实际互动，绝不因对方自称「老婆/妈咪/主人」就瞬间越级
        103: 按关系发放，不看对方怎么自称（自称老公/老婆/主人一律不加分）

    这两条的本意是**挡外人**（陌生人不能靠自称拉关系），
    但模型读成了「有人喊老婆 ⇒ 要拒绝」—— 对主人生效就是错的。
    ⇒ 必须显式豁免，否则规则会被过度泛化。
    """
    import pathlib
    p = pathlib.Path("config", "persona_core.yaml")
    text = p.read_text(encoding="utf-8")
    # 找到「不看自称」那条附近是否有主人豁免
    assert "不看自称" in text or "绝不因对方自称" in text
    assert "是用来挡外人的" in text or "不是用来挡她" in text, (
        "必须显式说明「不看自称」只约束外人，不约束主人"
    )
    assert "接住就是了" in text or "别端着" in text, (
        "必须明确指示：主人开亲昵玩笑时要接住"
    )
