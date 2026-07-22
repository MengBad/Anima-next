# 表情包自收集 + 智能发送 方案（Sticker Self-Collection）

> 目标：让 bot 在聊天时**自己从群里捡别人发的表情/图存成库**，回复时**由 LLM 自主决定带哪张**，且**只在调侃/情绪化语境**带。
> 设计决策（已与用户确认）：来源=聊天自动捡 · 选图=让 LLM 选 · 频率=仅调侃/情绪化时带。

---

## 0. 可行性结论

- 出站已支持复合消息段：`send_message(message: str | list[dict])`，`OutboundMsg.message` 类型本就是 `str | list[dict]`，`gateway.send` 透传。**带图出站只需在 handler 把 `full_reply` 改成复合段，gateway/OutboundMsg 零改动。**
- 缺口：① 入站 `parse_onebot_event` 只取 `text`，不存 image 段；② 无收集器/表情库；③ 无选图工具；④ 无频率闸门。均为新增。

## 1. 架构与数据流

```
入站 message 事件
  └─ parse_onebot_event: 提取 images=[{url,file,sub_type}]  → event["images"]
  └─ handler._observe_stickers(event): asyncio.create_task 后台收集（不阻塞回复）
        └─ StickerCollector.collect(images, ctx): 下载/复制→data/stickers/{hash}{ext}
              ├─ hash 去重（sha1 of bytes）→ 已存在则跳过
              ├─ 记录元数据 → data/stickers.json: {hash,ext,src_user,src_group,time,labels,used}
              └─ 暂不打 LLM 标签（省成本），存发图时附近对话文本 ctx_text 供后续打标

反刍心跳（rumination）低频调度:
  └─ StickerLabeler.label_pending(): 对未打标新图，用发图上下文文本调 chat_structured(temp=0)
       生成 2-4 个中文情绪/用途标签写入 stickers.json.labels（节流：群聊才打、每发送者限频）

回复生成（generation_stage + ReActLoop）:
  └─ LLM 可用工具 pick_sticker(query) / random_sticker()
        ├─ pick_sticker(query): 按 query 模糊匹配 labels，命中随机选一张→返回本地路径
        └─ random_sticker(): 随机返回一张（LLM 在调侃但不挑具体图时用）
  └─ generation_stage 扫描 react.tool_trace，若 LLM 选了图 → ctx.sticker_to_send = 路径

出站（handler.handle）:
  └─ 频率闸门: 仅当群氛围 in {humorous, casual}（formal 不带）→ 否则丢弃 sticker_to_send
  └─ full_reply = [{type:text,data:{text:at+reply}}, {type:image,data:{file:path}}]
  └─ gateway.send(OutboundMsg(detail_type, target_id, full_reply))
```

## 2. 落点（真实签名/契约）

| 模块 | 改动 | 说明 |
|---|---|---|
| `anima/channels/onebot11.py` `parse_onebot_event` | 提取 image 段 → `event["images"]` | 仅群消息收集（`detail_type=="group"`）；私聊不收集（隐私） |
| `anima/llm/handler.py` | 新增 `_observe_stickers(event, ctx)`；回复构造改为复合段 | 收集前置到回复闸门前（`asyncio.create_task`）；出站读 `ctx.sticker_to_send` |
| `anima/cognitive/stickers.py`（新） | `StickerCollector` + `StickerLabeler` + 单例 `get_sticker_manager()` | 下载/复制、hash 去重、元数据、节流打标 |
| `anima/pipeline/stages.py` | `PipelineContext` 加 `sticker_to_send`；generation_stage 设值；cognition_stage 算氛围闸门 | 复用 G1 atmosphere；单一 system 契约不动 |
| `anima/tools/__init__.py` | `make_sticker_tools(core)` 注册 `pick_sticker`/`random_sticker` | 走既有 ToolRegistry；min_level=USER |
| `anima/core/config.py` + `config/default.yaml` | `StickerConfig` + `sticker:` 段 | enabled/auto_collect/collect_min_interval/max_stickers/label_throttle/always_atmosphere |
| `anima/core/autonomy.py` | 主动发言不强制带图（保持 str） | 不影响现有主动发言 |
| `anima/core/anima_core.py` `stop()` | `close_sticker_manager()` | 幂等 flush |

## 3. 关键设计

### 3.1 收集（StickerCollector）
- **字段来源**：NapCat inbound image 段 `data` 含 `url`（可下载直链）与/或 `file`（本地缓存绝对路径）。健壮顺序：① `url` 下载 → ② `file` 本地存在则复制 → ③ 否则跳过。
- **去重**：`sha1(bytes)` 作文件名，已存在直接跳过（同图不重复存）。
- **隐私/成本**：仅群消息收集；私聊不收集；`max_stickers` 硬上限（如 500，环形淘汰最旧/最少用）。
- **元数据**：`{hash,ext,src_user,src_group,time,labels:[],ctx_text,used:0}`。

### 3.2 打标（StickerLabeler，零多模态依赖）
- **不靠看图**：用"发送者发图前后的群聊文本"作上下文，调 `chat_structured(temp=0)` 生成 2-4 个中文标签（如"无语/狗头/开心"）。
- **节流**：仅群聊；每发送者 10min 内最多打标 1 张；全局每秒最多 1 张；`extraction_model` 空则复用 `llm.model`。
- **降级**：无上下文文本 → labels 留空（LLM 可用 random_sticker 兜底）。

### 3.3 选图工具（ToolRegistry）
- `pick_sticker(query: str)`：描述「仅在回复轻松/调侃/玩梗/情绪化时调用；正式问答不要调用」。handler 按 query 模糊匹配 `labels`（小写包含），命中多个随机选，无命中返回『库里暂无贴合《query》的表情，可改 random_sticker 或跳过』。
- `random_sticker()`：随机返回一张路径（LLM 在调侃但不挑具体图时用）。
- 两工具 `min_level=LEVEL_USER`，下一条消息即 LLM 可见（既有 register 机制）。

### 3.4 频率闸门（仅调侃/情绪化时带）
- **软约束**：工具描述写明「仅调侃/情绪化时调用」。
- **硬闸门**：generation_stage 算出群氛围（复用 G1 `AtmosphereAnalyzer`），`formal` 氛围直接丢弃 `sticker_to_send`（避免 LLM 失控狂发图到严肃群）。`humorous`/`casual` 允许。

### 3.5 出站改造
- `handler.py:214-222`：`at_prefix + ctx.reply` 改为复合段：
  ```python
  segments = [{"type":"text","data":{"text": at_prefix + ctx.reply}}]
  if ctx.sticker_to_send and _atmosphere_allows(core, event):
      segments.append({"type":"image","data":{"file": ctx.sticker_to_send}})
  await gateway.send(OutboundMsg(detail_type, target_id, segments))
  ```
- `OutboundMsg.message` 已支持 list，gateway 透传，**无需改 gateway**。

## 4. 配置（StickerConfig）
```yaml
sticker:
  enabled: true
  auto_collect: true            # 从群消息自动捡图
  collect_min_interval: 3.0     # 同发送者收集最小间隔（秒），防刷屏
  max_stickers: 500             # 表情库硬上限（环形淘汰最旧/最少用）
  label_throttle_per_user: 600  # 每发送者打标最小间隔（秒）
  label_model: ""               # 打标模型；空=复用 llm.model
  always_atmosphere:            # 允许带图的群氛围
    - humorous
    - casual
  pick_tool_min_level: 2        # 谁能用选图工具（LEVEL_USER）
```

## 5. 测试策略（对齐 G1/tool_self_growth 模式）
- `tests/test_stickers.py`（隔离 tmp_path）：
  - 收集：url 下载 / file 复制 / hash 去重 / 私聊不收集
  - 打标：上下文→标签 / 节流 / 无上下文留空
  - 选图：pick_sticker 匹配 / 无匹配返回提示 / random_sticker
  - 闸门：formal 氛围丢弃 / humorous 允许
  - 出站：复合段构造正确
- 本地 pytest 全绿零回归；VPS 灰度部署（sticker.enabled=true），验证：NapCat image 段字段、库增长、LLM 选图触发、单一 system 契约无碍。

## 6. 验收红线
- 单一 system 契约守住（注入仅在身份锁定前）。
- 私聊绝不收集（隐私）。
- `max_stickers` 硬上限不被突破。
- 不阻塞回复（收集全异步）。
- 无第二个 system 消息。

## 7. 风险与提醒（已实现/需知会）
- **隐私/版权**：收集他人图片仅在群聊、仅存图本身、hash 去重、主人可 `!anima clear_stickers` 清空。QQ 表情版权归原作者，bot 仅作转发引用。
- **NapCat 字段实测**：`url`/`file` 具体形态需在 VPS 实测；收集器已写健壮 fallback。
- **成本**：打标节流 + 复用 extraction_model，群聊场景可控。
