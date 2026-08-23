"""统一配置变更服务。

WebUI 不再由各页面自行写 YAML。所有变更先形成可审阅的 change-set，
经过 Pydantic 全量校验后再写入；敏感值只在输入时接收，响应永远脱敏。
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError

from .config import AnimaConfig
from .config_recovery import arm as arm_config_recovery, clear as clear_config_recovery


_SECRET_PARTS = ("api_key", "password", "secret", "cookie", "sessdata", "bili_jct")


def _is_secret_key(key: str) -> bool:
    low = key.lower()
    return (any(part in low for part in _SECRET_PARTS)
            or low in {"token", "access_token", "webui_token"}
            or low.endswith("_token"))


def _plain(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _masked(value: Any, key: str = "") -> Any:
    if key and _is_secret_key(key):
        if isinstance(value, dict) and "configured" in value:
            return value
        return {"configured": bool(value)}
    if isinstance(value, SecretStr):
        return {"configured": bool(value.get_secret_value())}
    if isinstance(value, dict):
        return {str(k): _masked(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v, key) for v in value]
    return value


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


MANIFEST: list[dict[str, Any]] = [
    {"path": "llm.endpoint", "label": "LLM Base URL", "type": "url", "risk": "yellow", "restart": True, "impact": ["主聊天", "认知", "工具"]},
    {"path": "llm.api_key", "label": "LLM API Key", "type": "secret", "risk": "yellow", "restart": True, "impact": ["所有 LLM 模块"]},
    {"path": "llm.model", "label": "默认模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["继承默认模型的模块"]},
    {"path": "llm.active_provider", "label": "当前提供商", "type": "text", "risk": "yellow", "restart": True, "impact": ["所有 LLM 模块"]},
    {"path": "llm.providers", "label": "提供商配置档案", "type": "providers", "risk": "yellow", "restart": True, "impact": ["LLM 路由"]},
    {"path": "llm.module_models", "label": "模块模型路由", "type": "routes", "risk": "yellow", "restart": True, "impact": ["指定模块"]},
    {"path": "llm.timeout", "label": "请求超时", "type": "number", "min": 5, "max": 300, "risk": "green", "restart": True, "impact": ["LLM 请求"]},
    {"path": "llm.retries", "label": "失败重试次数", "type": "number", "min": 0, "max": 4, "risk": "green", "restart": True, "impact": ["LLM 请求"]},
    {"path": "llm.fallback_models", "label": "备用模型", "type": "csv", "risk": "yellow", "restart": True, "impact": ["LLM 容灾", "主聊天", "认知"]},
    {"path": "llm.circuit_failure_threshold", "label": "熔断失败阈值", "type": "number", "min": 1, "max": 20, "risk": "yellow", "restart": True, "impact": ["LLM 容灾", "QQ 回复"]},
    {"path": "llm.circuit_cooldown_sec", "label": "熔断冷却（秒）", "type": "number", "min": 1, "max": 600, "risk": "yellow", "restart": True, "impact": ["LLM 容灾", "QQ 回复"]},
    {"path": "autonomy.active_reply_prob", "label": "主动发言概率", "type": "number", "min": 0, "max": 1, "risk": "green", "restart": False, "impact": ["主动行为"]},
    {"path": "autonomy.active_global_cooldown_sec", "label": "主动发言全局冷却（秒）", "type": "number", "min": 60, "max": 86400, "risk": "green", "restart": False, "impact": ["主动行为"]},
    {"path": "autonomy.active_silence_min_sec", "label": "沉默多久后考虑插话（秒）", "type": "number", "min": 30, "max": 3600, "risk": "green", "restart": False, "impact": ["主动行为"]},
    {"path": "autonomy.active_min_group_messages", "label": "群内最少人类消息数", "type": "number", "min": 1, "max": 50, "risk": "green", "restart": False, "impact": ["主动行为"]},
    {"path": "autonomy.active_daily_max_per_group", "label": "每群每日主动发言上限", "type": "number", "min": 0, "max": 50, "risk": "green", "restart": False, "impact": ["主动行为", "群策略"]},
    {"path": "autonomy.active_context_score_threshold", "label": "语境评分阈值", "type": "number", "min": 0, "max": 1, "risk": "green", "restart": False, "impact": ["主动行为"]},
    {"path": "autonomy.daily_max_per_group", "label": "单群每日总上限（含议程）", "type": "number", "min": 0, "max": 1000, "risk": "green", "restart": False, "impact": ["群策略"]},
    {"path": "autonomy.reply_only_when_mentioned", "label": "仅被呼叫时回复", "type": "boolean", "risk": "green", "restart": False, "impact": ["回复门控"]},
    {"path": "autonomy.familiar_reply_min", "label": "熟人模式亲密度门槛", "type": "number", "min": 0, "max": 1, "risk": "green", "restart": False, "impact": ["回复门控"]},
    {"path": "permissions.group_policies", "label": "逐群策略", "type": "group_policies", "risk": "green", "restart": False, "impact": ["群策略", "回复门控", "工具与表情"]},
    # 心跳对象是在启动阶段按该开关装配的；切换开关必须受控重启，
    # 否则仅替换 Pydantic 配置并不会创建/销毁运行中的任务。
    {"path": "autonomy.enabled", "label": "自主行为总开关", "type": "boolean", "risk": "red", "restart": True, "impact": ["心跳", "主动行为"]},
    {"path": "vision.model", "label": "图片理解模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["图片理解"]},
    {"path": "sticker.label_model", "label": "表情审核模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["表情资产"]},
    {"path": "sticker.select_model", "label": "表情选图模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["表情发送"]},
    {"path": "image_generation.model", "label": "生图模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["图片生成"]},
    {"path": "sylann.mode", "label": "Sylann 模式", "type": "select", "options": ["disabled", "shadow", "active"], "risk": "red", "restart": True, "impact": ["Sylann 认知"]},
    {"path": "crossgroup.enabled", "label": "跨群记忆共享", "type": "boolean", "risk": "red", "restart": True, "impact": ["跨群记忆"]},
    # —— 表达与节奏 ——
    {"path": "reply_split.enabled", "label": "拟人分段发送", "type": "boolean", "risk": "green", "restart": False, "impact": ["回复分段"]},
    {"path": "reply_split.max_segments", "label": "最大分段数", "type": "number", "min": 1, "max": 10, "risk": "green", "restart": False, "impact": ["回复分段"]},
    {"path": "reply_split.trigger_threshold", "label": "分段触发字数", "type": "number", "min": 20, "max": 500, "risk": "green", "restart": False, "impact": ["回复分段"]},
    {"path": "reply_split.min_segment_len", "label": "单段最小字数", "type": "number", "min": 10, "max": 200, "risk": "green", "restart": False, "impact": ["回复分段"]},
    {"path": "reply_split.max_segment_len", "label": "单段最大字数", "type": "number", "min": 30, "max": 500, "risk": "green", "restart": False, "impact": ["回复分段"]},
    {"path": "reply_split.delay_enabled", "label": "段间拟真延迟", "type": "boolean", "risk": "green", "restart": False, "impact": ["回复延迟"]},
    {"path": "reply_split.delay_base", "label": "基础延迟（秒）", "type": "number", "min": 0, "max": 5, "risk": "green", "restart": False, "impact": ["回复延迟"]},
    {"path": "reply_split.delay_factor", "label": "每字延迟（秒/字）", "type": "number", "min": 0, "max": 0.5, "risk": "green", "restart": False, "impact": ["回复延迟"]},
    {"path": "reply_split.delay_jitter", "label": "延迟抖动上限（秒）", "type": "number", "min": 0, "max": 3, "risk": "green", "restart": False, "impact": ["回复延迟"]},
    {"path": "debounce.group_enabled", "label": "群聊消息防抖", "type": "boolean", "risk": "green", "restart": False, "impact": ["群聊合并"]},
    {"path": "debounce.group_wait_sec", "label": "防抖等待窗口（秒）", "type": "number", "min": 0.5, "max": 10, "risk": "green", "restart": False, "impact": ["群聊合并"]},
    {"path": "debounce.group_max_wait_sec", "label": "最大等待时间（秒）", "type": "number", "min": 1, "max": 30, "risk": "green", "restart": False, "impact": ["群聊合并"]},
    {"path": "debounce.adaptive", "label": "自适应等待", "type": "boolean", "risk": "green", "restart": False, "impact": ["群聊合并"]},
    {"path": "debounce.short_msg_threshold", "label": "短句阈值（字）", "type": "number", "min": 3, "max": 50, "risk": "green", "restart": False, "impact": ["群聊合并"]},
    {"path": "voice.enabled", "label": "AI 语音消息", "type": "boolean", "risk": "green", "restart": False, "impact": ["语音 TTS"]},
    {"path": "voice.auto_probability", "label": "自动语音触发概率", "type": "number", "min": 0, "max": 1, "risk": "green", "restart": False, "impact": ["语音 TTS"]},
    {"path": "meme.enabled", "label": "趣味图片互动", "type": "boolean", "risk": "green", "restart": False, "impact": ["摸头杀"]},
    {"path": "meme.petpet_on_poke", "label": "被戳摸头回击概率", "type": "number", "min": 0, "max": 1, "risk": "green", "restart": False, "impact": ["摸头杀"]},
    # —— 认知系统 ——
    {"path": "soul.enabled", "label": "灵魂能量槽", "type": "boolean", "risk": "yellow", "restart": True, "impact": ["LLM 参数", "语气调节"]},
    {"path": "soul.ema_alpha", "label": "EMA 平滑系数", "type": "number", "min": 0.05, "max": 1, "risk": "green", "restart": False, "impact": ["能量平滑"]},
    {"path": "gate.enabled", "label": "轻重双模型前置判断", "type": "boolean", "risk": "yellow", "restart": True, "impact": ["群消息过滤"]},
    {"path": "gate.model", "label": "Gate 轻量模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["Gate 判断"]},
    {"path": "gate.timeout_ms", "label": "Gate 超时（毫秒）", "type": "number", "min": 100, "max": 3000, "risk": "green", "restart": False, "impact": ["Gate 延迟"]},
    # —— 行为 / 社交 ——
    {"path": "qzone.enabled", "label": "QQ 空间说说", "type": "boolean", "risk": "yellow", "restart": True, "impact": ["空间说说"]},
    {"path": "qzone.daily_max", "label": "每日说说上限", "type": "number", "min": 0, "max": 10, "risk": "green", "restart": False, "impact": ["空间说说"]},
    {"path": "qzone.cooldown_sec", "label": "说说冷却（秒）", "type": "number", "min": 60, "max": 86400, "risk": "green", "restart": False, "impact": ["空间说说"]},
    {"path": "qq_status.enabled", "label": "QQ 在线状态同步", "type": "boolean", "risk": "green", "restart": True, "impact": ["在线状态"]},
    # —— 感知 / 媒体 ——
    {"path": "bilibili_read.enabled", "label": "B 站视频理解", "type": "boolean", "risk": "green", "restart": False, "impact": ["视频字幕"]},
    {"path": "bilibili_read.sessdata", "label": "B 站 SESSDATA", "type": "secret", "risk": "yellow", "restart": False, "impact": ["视频字幕"]},
    {"path": "bilibili_read.bili_jct", "label": "B 站 bili_jct", "type": "secret", "risk": "yellow", "restart": False, "impact": ["视频字幕"]},
    {"path": "bilibili_read.max_subtitle_length", "label": "字幕截断上限", "type": "number", "min": 500, "max": 20000, "risk": "green", "restart": False, "impact": ["视频字幕"]},
    # —— 向量 / 语义检索（硅基流动）——
    {"path": "vector.enabled", "label": "语义向量记忆", "type": "boolean", "risk": "yellow", "restart": True, "impact": ["语义检索", "长期记忆"]},
    {"path": "vector.embedding_backend", "label": "Embedding 后端", "type": "text", "risk": "yellow", "restart": True, "impact": ["语义检索"]},
    {"path": "vector.embedding_endpoint", "label": "Embedding 端点", "type": "url", "risk": "yellow", "restart": True, "impact": ["语义检索"]},
    {"path": "vector.embedding_model", "label": "Embedding 模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["语义检索"]},
    {"path": "vector.embedding_dim", "label": "向量维度", "type": "number", "min": 64, "max": 8192, "risk": "yellow", "restart": True, "impact": ["语义检索"]},
    {"path": "vector.embedding_api_key", "label": "Embedding API Key", "type": "secret", "risk": "yellow", "restart": True, "impact": ["语义检索"]},
    {"path": "vector.cache", "label": "缓存 Embedding", "type": "boolean", "risk": "green", "restart": False, "impact": ["语义检索"]},
    {"path": "vector.rerank_enabled", "label": "语义重排", "type": "boolean", "risk": "yellow", "restart": True, "impact": ["排序质量"]},
    {"path": "vector.rerank_endpoint", "label": "Rerank 端点", "type": "url", "risk": "yellow", "restart": True, "impact": ["排序质量"]},
    {"path": "vector.rerank_model", "label": "Rerank 模型", "type": "text", "risk": "yellow", "restart": True, "impact": ["排序质量"]},
    {"path": "vector.rerank_top_n", "label": "重排取前 N", "type": "number", "min": 1, "max": 50, "risk": "green", "restart": False, "impact": ["排序质量"]},
    {"path": "vector.rerank_api_key", "label": "Rerank API Key", "type": "secret", "risk": "yellow", "restart": True, "impact": ["排序质量"]},
]


class ConfigService:
    def __init__(self, core: Any):
        self.core = core
        self.persistence = core.persistence
        self._changes: dict[str, dict[str, Any]] = self.persistence.read_json("config_changes.json", {}) or {}
        # 早期版本曾把历史快照以明文写入；启动时立即重写为脱敏格式，
        # 避免旧文件继续暴露密钥。当前进程仍可用内存中的完整值完成回滚。
        if self._changes:
            self._persist()

    @property
    def config(self) -> AnimaConfig:
        return self.core.config

    def manifest(self) -> dict[str, Any]:
        return {"fields": MANIFEST, "presets": [
            {"id": "quiet", "label": "安静模式", "patch": {"autonomy": {"active_reply_prob": 0.01, "daily_max_per_group": 1}}},
            {"id": "balanced", "label": "均衡模式", "patch": {"autonomy": {"active_reply_prob": 0.04, "daily_max_per_group": 3}}},
            {"id": "lively", "label": "活泼模式", "patch": {"autonomy": {"active_reply_prob": 0.10, "daily_max_per_group": 8}}},
            {"id": "mention_only", "label": "仅被呼叫回复", "patch": {"autonomy": {"reply_only_when_mentioned": True, "active_reply_prob": 0}}},
            {"id": "maintenance", "label": "安全维护模式", "patch": {"autonomy": {"enabled": False}}},
        ]}

    def state(self) -> dict[str, Any]:
        raw = _plain(self.config.model_dump())
        return _masked(raw)

    @staticmethod
    def masked(value: Any) -> Any:
        return _masked(value)

    def _raw_state(self) -> dict[str, Any]:
        return _plain(self.config.model_dump())

    def _impact(self, patch: dict[str, Any]) -> tuple[list[str], bool, str]:
        changed = {f"{section}.{key}" for section, values in patch.items() if isinstance(values, dict) for key in values}
        fields = [item for item in MANIFEST if item["path"] in changed]
        impact = sorted({name for item in fields for name in item.get("impact", [])})
        restart = any(item.get("restart") for item in fields)
        risk = "red" if any(item.get("risk") == "red" for item in fields) else ("yellow" if any(item.get("risk") == "yellow" for item in fields) else "green")
        return impact, restart, risk

    def validate(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("变更不能为空")
        current = self._raw_state()
        # WebUI secret 字段留空代表保持原值；"configured" 对象也不能被写回。
        cleaned = copy.deepcopy(patch)
        llm = cleaned.get("llm") if isinstance(cleaned.get("llm"), dict) else {}
        if "api_key" in llm and (llm["api_key"] in (None, "") or isinstance(llm["api_key"], dict)):
            llm.pop("api_key", None)
        # 向量检索密钥同样：留空/configured 对象不写回，保持原值
        vector = cleaned.get("vector") if isinstance(cleaned.get("vector"), dict) else {}
        for _vk in ("embedding_api_key", "rerank_api_key"):
            if _vk in vector and (vector[_vk] in (None, "") or isinstance(vector[_vk], dict)):
                vector.pop(_vk, None)
        merged = _deep_merge(current, cleaned)
        return _plain(AnimaConfig.model_validate(merged).model_dump())

    def create_change_set(self, patch: dict[str, Any]) -> dict[str, Any]:
        after = self.validate(patch)
        before = self._raw_state()
        impact, restart, risk = self._impact(patch)
        change_id = "cfg_" + uuid.uuid4().hex[:12]
        item = {"id": change_id, "status": "pending", "created_at": time.time(), "patch": patch,
                "before": before, "after": after, "impact": impact, "restart_required": restart, "risk": risk}
        self._changes[change_id] = item
        self._persist()
        return self._public_change(item)

    def _public_change(self, item: dict[str, Any]) -> dict[str, Any]:
        out = dict(item)
        out["patch"] = _masked(item.get("patch", {}))
        out["before"] = _masked(item.get("before", {}))
        out["after"] = _masked(item.get("after", {}))
        return out

    def _persist(self) -> None:
        # 变更历史可审计，但永远不把 API Key / Cookie / Token 落到 JSON。
        self.persistence.write_json("config_changes.json", {
            key: self._public_change(item) for key, item in self._changes.items()
        })

    def get_change_set(self, change_id: str) -> dict[str, Any] | None:
        item = self._changes.get(change_id)
        return self._public_change(item) if item else None

    def get_raw_change_set(self, change_id: str) -> dict[str, Any] | None:
        """Return the in-memory record for trusted runtime preflight only.

        This is deliberately not used as an HTTP response: the record may hold
        a newly supplied API key until the change is applied.  The WebUI server
        uses it solely to validate a provider before replacing configuration.
        """
        return self._changes.get(change_id)

    def list_change_sets(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the newest public change-set records for the WebUI.

        The persistence file is intentionally treated as an audit/history store,
        not as an implementation detail of the page.  Always return masked
        snapshots and clamp the caller supplied limit so a corrupted or overly
        large query cannot make the console allocate an unbounded response.
        """
        try:
            size = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            size = 50
        items = sorted(self._changes.values(), key=lambda item: float(item.get("created_at", 0)), reverse=True)
        return [self._public_change(item) for item in items[:size]]

    def apply(self, change_id: str) -> dict[str, Any]:
        item = self._changes.get(change_id)
        if not item:
            raise KeyError("变更不存在")
        if item.get("status") == "applied":
            return self._public_change(item)
        validated = AnimaConfig.model_validate(item["after"])
        path = getattr(self.core, "config_path", None)
        if path:
            backup_path = self._write_yaml(Path(path), _plain(validated.model_dump()), self.persistence.data_dir / "config_backups")
            if backup_path:
                item["backup_path"] = str(backup_path)
            if item.get("restart_required") and backup_path:
                try:
                    arm_config_recovery(self.persistence.data_dir, change_id, path, backup_path)
                except Exception:
                    # Do not leave a candidate config without a recovery guard.
                    self._restore_backup(Path(path), backup_path)
                    raise
        self.core.config = validated
        self._sync_runtime(validated)
        item["status"] = "applied"; item["applied_at"] = time.time()
        self._persist()
        return self._public_change(item)

    def rollback(self, change_id: str) -> dict[str, Any]:
        item = self._changes.get(change_id)
        if not item:
            raise KeyError("变更不存在")
        before = item["before"]
        if isinstance((before.get("llm") if isinstance(before, dict) else None), dict) and isinstance(before.get("llm", {}).get("api_key"), dict):
            backup_path = Path(str(item.get("backup_path") or ""))
            if not backup_path.is_file():
                raise ValueError("该变更的敏感配置历史已脱敏，且找不到可回滚备份")
            loaded = yaml.safe_load(backup_path.read_text(encoding="utf-8")) or {}
            restored = AnimaConfig.model_validate(loaded)
        else:
            restored = AnimaConfig.model_validate(before)
        path = getattr(self.core, "config_path", None)
        if path:
            self._write_yaml(Path(path), _plain(restored.model_dump()), self.persistence.data_dir / "config_backups")
            clear_config_recovery(self.persistence.data_dir, change_id)
        self.core.config = restored
        self._sync_runtime(restored)
        item["status"] = "rolled_back"; item["rolled_back_at"] = time.time()
        self._persist()
        return self._public_change(item)

    def reject(self, change_id: str, reason: str = "Owner 驳回") -> dict[str, Any]:
        """标记尚未应用的变更为驳回，不修改当前运行配置。"""
        item = self._changes.get(change_id)
        if not item:
            raise KeyError("变更不存在")
        status = str(item.get("status") or "pending")
        if status == "rejected":
            return self._public_change(item)
        if status != "pending":
            raise ValueError(f"只有待应用变更可以驳回（当前状态：{status}）")
        cleaned = " ".join(str(reason or "Owner 驳回").split())[:240] or "Owner 驳回"
        item["status"] = "rejected"
        item["rejected_at"] = time.time()
        item["rejection_reason"] = cleaned
        self._persist()
        return self._public_change(item)

    @staticmethod
    def _restore_backup(path: Path, backup_path: Path) -> None:
        """Restore a config file when arming startup recovery fails."""
        with backup_path.open("rb") as source, path.open("wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())

    def _sync_runtime(self, config: AnimaConfig) -> None:
        """将无需重启的心跳参数同步到已装配的运行时对象。

        LLM/Sylann 等黄色配置仍由受控重启完成完整重建；绿色主动行为参数
        则必须立即反映到 Heartbeat，否则 WebUI 显示的值与实际行为会分离。
        """
        heartbeat = getattr(self.core, "heartbeat", None)
        if heartbeat is None:
            return
        autonomy = config.autonomy
        updates = {
            "active_reply_prob": autonomy.active_reply_prob,
            "active_global_cooldown_sec": autonomy.active_global_cooldown_sec,
            "active_silence_min_sec": autonomy.active_silence_min_sec,
            "active_min_group_messages": autonomy.active_min_group_messages,
            "active_daily_max_per_group": autonomy.active_daily_max_per_group,
            "active_context_score_threshold": autonomy.active_context_score_threshold,
            "daily_max": autonomy.daily_max_per_group,
        }
        for attr, value in updates.items():
            if hasattr(heartbeat, attr):
                setattr(heartbeat, attr, value)

    @staticmethod
    def _write_yaml(path: Path, data: dict[str, Any], backup_dir: Path | None = None) -> Path | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同目录原子替换，并保留一份可人工恢复的上一版本。
        backup_path: Path | None = None
        if path.exists() and backup_dir is not None:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / (path.name + "." + str(int(time.time())) + ".bak")
            shutil.copyfile(path, backup_path)
        # 生产 systemd 只允许 data/ 写入；先把临时文件放在允许写入的目录，
        # 最后复制到 config.yaml（同目录可写时仍走 os.replace）。
        temp_dir = backup_dir if backup_dir is not None else path.parent
        temp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".anima-config-", suffix=".yaml", dir=str(temp_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
                fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp, path)
            return backup_path
        except OSError:
            # systemd ProtectSystem=strict 下，目标文件可被 ReadWritePaths 放行，
            # 但同目录临时文件未必可创建；退化为先完成校验再覆盖目标文件。
            try:
                shutil.copyfile(tmp, path)
                os.unlink(tmp)
                return backup_path
            except Exception:
                try: os.unlink(tmp)
                except OSError: pass
                raise
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise
