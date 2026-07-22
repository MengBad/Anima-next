# 全量多模态视觉理解（vision）设计文档

> 用户定调：**体验优先于 token 成本**。原"为省 token 不用视觉"的设计决策作废。
> 范围：**全量多模态**——bot 真·看见图片（描述 / 打标 / 聊天接话）。

## 可行性命门（已验证）
- `gemini-antigravity` 网关（OpenAI 兼容层）**确实透传 `image_url`** 内容段。
- VPS spike：发 1x1 红图 `data:image/png;base64` → 网关返回 `Coral`（视觉模型工作）。
- 模型 `gemini-3.5-flash-low` 原生支持视觉。

## 架构

### 共享层 `anima/media.py`
- `fetch_image_bytes(img, max_bytes)`：url 下载（aiohttp）→ fallback 本地 file 复制；超限返回 None。
- `to_data_uri(data, img)`：magic number 推断后缀 → `data:image/<ext>;base64,<...>` 内联。
- `build_multimodal_user_content(text, uris)`：构造 OpenAI 视觉格式多模态 user content。
- 图片仅下载到本机内存、内联给网关，**不落盘**（sticker 收集除外，那是用户明确要的素材库）。

### 视觉模块 `anima/cognitive/vision.py`
- `VisionManager.describe(llm, bytes, img, model)` → 一句中文描述。
- `VisionManager.label(llm, bytes, img, ctx_text, model)` → 2-4 中文标签（`record_sticker_labels` 工具调用）。
- per-user 节流；单例 `get_vision_manager` / `close_vision_manager`。
- 所有调用 try/except 退化，绝不阻塞回复。

### 生成注入（核心体验）`anima/pipeline/stages.py`
- `generation_stage`：若 `ctx.inbound_images` 且 vision 启用，下载图→内联→把当轮 user 消息 content 改为多模态列表 → LLM **当场看见图并回复**（无额外延迟）。
- `memory_stage`：`not ctx.text and not ctx.inbound_images` 才 SKIP_REPLY（修复"纯图片消息不回复"）；图片消息占位文本 `[用户发送了图片]`。
- `_collect_vision_uris`：每回合最多 4 张图，控 token。

### 后台描述存储 `anima/llm/handler.py`
- pipeline 跑完（用户条目已落记忆）后 `asyncio.create_task(_vision_describe(...))`。
- 生成中文描述 → `chat_memory.set_vision_desc(...)` → 后续轮次 `get_context` 输出 `[图片:...]`，bot 能忆起"你发过那张图"。
- 仅对进入 pipeline 的消息（过回复闸门）做描述，不在未 @bot 的群图上浪费视觉 token。

### 表情打标升级 `anima/cognitive/stickers.py`
- `label_pending` 优先**视觉打标**（真看图，`vision.label`）；失败/关闭时退化文本推断。
- 配置 `sticker.label_by_vision`（默认 true）。

### 记忆 `anima/memory/chat_memory.py`
- `add(..., vision_desc="")` 写入条目；`get_context` 含 `[图片:<desc>]`；`set_vision_desc(...)` 补写最近用户条目。
- **图片本身不进记忆**，只存文字描述（隐私 + 省 token）。

### 配置 `config.py` / `default.yaml`
- `VisionConfig`：`enabled / model / max_inline_bytes(4MB) / describe_throttle_per_user / comprehend_in_chat / store_desc_to_memory`，默认全开。

### 关闭 `anima/core/anima_core.py`
- `stop()` 调 `close_vision_manager()`。

## 单 system 契约
图片只进 user 消息（多模态 content），绝不插入第二个 system 消息。人格锁定契约不变。

## 隐私
- 入站图仅下载到本机内存后内联给网关；不原文件落盘（sticker 库除外）。
- 描述文本进对话记忆，图片本身不进记忆。

## 验收
- 本地 121 passed（含 `test_vision.py` 13 例：media 工具 / vision 模块 / vision_desc / 多模态注入 / 纯图片不跳过）。
- VPS 部署后：import 校验 + 真实视觉调用（describe 真图）验证端到端。
