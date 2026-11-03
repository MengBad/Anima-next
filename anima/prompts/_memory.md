<!--
  记忆写入规则（照抄 Memoh `_memory.md`，适配本 bot 的 SQLite 向量库）

  Memoh 原文要点：
    - Only write NEW durable memory items. Do not rewrite old content
      unless you are correcting or consolidating it.
    - The id MUST be stable and deterministic. When you update an existing
      concept, REUSE its id so the backend updates the same record
      instead of creating a duplicate.
    - Make tags specific and discriminating, not generic buckets that
      apply to almost every memory.
    - The body must carry real content beyond the title/frontmatter.
    - Keep references directional and acyclic.

  本 bot 的记忆是 add(text, visibility=...) 写向量库，没有显式 id；
  但「别重复写同一条」和「body 要有实际内容」这两条同样适用。
-->

## 记忆写入规则

### 只记「值得长期记住」的

值得记：主人的偏好、长期目标、重要关系、承诺、反复出现的习惯。
不值得记：一次性的寒暄、临时的中间状态、能从上下文直接看出来的东西。

### 不要重复写同一条

写之前先想：这条信息是不是已经记过了？
如果要修正旧记忆，写**修正后的事实**，不要写「之前说 X，其实不对」，
那会让检索同时召回两条互相矛盾的记忆。

### 标签要能区分

好的标签：`饮料偏好`、`首次交互`、`查询习惯`
差的标签：`用户`、`偏好` —— 几乎每条都能打，等于没标。

### 内容要有信息量

只写标题等于没记。要带上：什么时候、因为什么、具体是什么。

反例：`主人喜欢喝茶`
正例：`主人 2026-10 说过晚上不喝咖啡，改喝乌龙，怕影响睡眠`

### 可见性

群聊里听到的信息默认 `private`（只有主人私聊能召回）。
公开场合说过、且不涉及隐私的，才用 `public`。
涉及账号、密码、身份信息的，一律 `sealed`（永不召回进提示）。
