"""LLM 调用用量账本与模块上下文。

只保存调用元数据，不保存 prompt、图片内容或模型回复。SQLite 使用标准库，
因此即使没有安装可选的 aiosqlite 也能在生产环境工作；写入在工作线程中完成。
"""

from __future__ import annotations

import asyncio
import contextvars
import csv
import functools
import inspect
import io
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class LLMCallInfo:
    module: str = "chat"
    operation: str = "chat"
    trace_id: str = ""
    #: 是否为「出站正文调用」。
    #:
    #: 归因判据（`react._LLM_FAULT_COUNTERS`）**只认**这类调用的故障计数。旁路
    #: （跨群提取／表情判定／反刍／模拟生活…）与出站正文共用同一个 llm 实例，
    #: 若不区分，一次旁路的 200-空 就会把「本 turn 上游是否静默」判成 True ⇒
    #: 归因桶被翻成 `upstream_empty`（本该 `no_tool`）并顺带触发挽救、白等一次预算。
    #: 默认 False 是刻意的：**没声明就不算**（旁路大多没有 scope，默认值即安全值）。
    outbound: bool = False


_CALL_INFO: contextvars.ContextVar[LLMCallInfo] = contextvars.ContextVar(
    "anima_llm_call_info", default=LLMCallInfo()
)


@contextmanager
def llm_scope(module: str, operation: str = "chat", trace_id: str | None = None,
              outbound: bool = False) -> Iterator[str]:
    """将当前协程中的 LLM 调用归属到一个可读模块。

    ``outbound=True`` 额外声明「这是要发给用户的正文生成」—— 见 `LLMCallInfo.outbound`。
    """
    trace = trace_id or uuid.uuid4().hex[:16]
    token = _CALL_INFO.set(LLMCallInfo(
        module=module or "chat", operation=operation, trace_id=trace,
        outbound=bool(outbound),
    ))
    try:
        yield trace
    finally:
        _CALL_INFO.reset(token)


def current_call_info() -> LLMCallInfo:
    return _CALL_INFO.get()


def scoped_llm(module: str, operation: str = "chat"):
    """Mark every LLM request made by an async operation with its module.

    Keeping the scope at the operation boundary avoids changing the small fake
    clients used by tests and integrations, while still making model routing
    and the persistent usage ledger agree about which subsystem spent tokens.
    """
    def decorator(func):
        if not inspect.iscoroutinefunction(func):
            raise TypeError("scoped_llm can only decorate async functions")

        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            with llm_scope(module, operation):
                return await func(*args, **kwargs)

        return wrapped

    return decorator


@dataclass(frozen=True)
class UsageEvent:
    created_at: float
    trace_id: str
    module: str
    operation: str
    provider: str
    requested_model: str
    actual_model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    latency_ms: float
    retries: int
    status: str
    estimated: bool = False
    error: str = ""
    #: 上游「思考」token（OpenAI 形态 `usage.completion_tokens_details.reasoning_tokens`、
    #: Gemini 形态 `usageMetadata.thoughtsTokenCount`）。2026-10-04 起落库 —— 此前
    #: `output_tokens` 把思考与正文混在一起，**无法归因「这次为什么慢」**（实测 49–2129 波动）。
    #: 缺字段即 0（「没有」≠「有」），存量行由迁移补 0。
    reasoning_tokens: int = 0
    #: 🔴 2026-10-16（A2 上下文预算可观测）：本回合**注入上下文**的估算 token 数与
    #: 记忆**召回命中条数**。与 `input_tokens` 的分工：后者是上游回报的**总量**，
    #: 这两项用于**分项归因**（到底是记忆塞太多、还是历史太长）。
    #: 缺值即 0（「没有」≠「有」），存量行由 `_migrate_columns` 补 0。
    ctx_tokens: int = 0
    recall_hits: int = 0


#: 本回合上下文采样的**进程内传递通道**：pipeline（记忆/组帧）写入，
#: `LLMClient._record_usage` 读取后入账。用 contextvar 而非改 `chat()` 签名
#: （签名一改，所有调用点与测试桩都要跟着动）。
_CTX_SAMPLE: contextvars.ContextVar[dict[str, int]] = contextvars.ContextVar(
    "anima_ctx_sample", default={}
)


def note_ctx_sample(*, ctx_tokens: int = 0, recall_hits: int = 0) -> None:
    """记下本回合的上下文预算 / 召回命中（供 usage ledger 归因）。任何异常都吞。"""
    try:
        _CTX_SAMPLE.set({"ctx_tokens": int(ctx_tokens or 0), "recall_hits": int(recall_hits or 0)})
    except Exception:  # noqa: BLE001 - 观测绝不阻断主流程
        pass


def current_ctx_sample() -> dict[str, int]:
    """读本回合采样；无采样时返回空 dict（调用方按 0 处理）。"""
    try:
        return _CTX_SAMPLE.get()
    except Exception:  # noqa: BLE001
        return {}


class UsageLedger:
    """轻量、可查询、跨重启保留的调用账本。"""

    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "llm_usage.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    trace_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    actual_model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    retries INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    estimated INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    ctx_tokens INTEGER NOT NULL DEFAULT 0,
                    recall_hits INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage_events(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage_events(actual_model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_module ON llm_usage_events(module)")
            self._migrate_columns(conn)

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """给**存量**库补列。

        🔴 `CREATE TABLE IF NOT EXISTS` 对已存在的表**完全不做改动** ⇒ 生产那个已有数万行的
        `llm_usage.sqlite3` 永远拿不到新列。必须显式 `ALTER TABLE`。
        幂等：先查 `PRAGMA table_info`，服务重启 / 多进程反复打开都安全。
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage_events)")}
        if "reasoning_tokens" not in cols:
            conn.execute("ALTER TABLE llm_usage_events "
                         "ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0")
        # 🔴 2026-10-16（A2）：存量库补上下文预算两列（同上，IF NOT EXISTS 不补列）。
        if "ctx_tokens" not in cols:
            conn.execute("ALTER TABLE llm_usage_events "
                         "ADD COLUMN ctx_tokens INTEGER NOT NULL DEFAULT 0")
        if "recall_hits" not in cols:
            conn.execute("ALTER TABLE llm_usage_events "
                         "ADD COLUMN recall_hits INTEGER NOT NULL DEFAULT 0")

    def record(self, event: UsageEvent) -> None:
        with self._db() as conn:
            conn.execute("""
                INSERT INTO llm_usage_events
                (created_at, trace_id, module, operation, provider, requested_model,
                 actual_model, input_tokens, output_tokens, cached_tokens, total_tokens,
                 latency_ms, retries, status, estimated, error, reasoning_tokens,
                 ctx_tokens, recall_hits)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.created_at, event.trace_id, event.module, event.operation,
                event.provider, event.requested_model, event.actual_model,
                event.input_tokens, event.output_tokens, event.cached_tokens,
                event.total_tokens, event.latency_ms, event.retries, event.status,
                int(event.estimated), event.error[:240], int(event.reasoning_tokens),
                int(event.ctx_tokens), int(event.recall_hits),
            ))

    async def arecord(self, event: UsageEvent) -> None:
        await asyncio.to_thread(self.record, event)

    def _where(self, start: float | None, end: float | None) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if start is not None:
            clauses.append("created_at >= ?"); params.append(start)
        if end is not None:
            clauses.append("created_at < ?"); params.append(end)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["estimated"] = bool(result.get("estimated"))
        return result

    def events(self, *, start: float | None = None, end: float | None = None,
               limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        where, params = self._where(start, end)
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_usage_events" + where +
                " ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, max(1, min(limit, 500)), max(0, offset))
            ).fetchall()
        return [self._row(row) for row in rows]

    def summary(self, *, start: float | None = None, end: float | None = None) -> dict[str, Any]:
        where, params = self._where(start, end)
        with self._db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) input_tokens, "
                "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cached_tokens),0) cached_tokens, "
                "COALESCE(SUM(total_tokens),0) total_tokens, COALESCE(SUM(CASE WHEN status='ok' THEN 0 ELSE 1 END),0) failures, "
                "COALESCE(AVG(latency_ms),0) avg_latency_ms FROM llm_usage_events" + where, params
            ).fetchone()
            models = conn.execute(
                "SELECT actual_model model, COUNT(*) calls, COALESCE(SUM(total_tokens),0) total_tokens "
                "FROM llm_usage_events" + where + " GROUP BY actual_model ORDER BY total_tokens DESC", params
            ).fetchall()
            modules = conn.execute(
                "SELECT module, COUNT(*) calls, COALESCE(SUM(total_tokens),0) total_tokens "
                "FROM llm_usage_events" + where + " GROUP BY module ORDER BY total_tokens DESC", params
            ).fetchall()
            trend = conn.execute(
                "SELECT CAST(created_at / 3600 AS INTEGER) * 3600 bucket, COUNT(*) calls, "
                "COALESCE(SUM(total_tokens),0) total_tokens FROM llm_usage_events" + where +
                " GROUP BY bucket ORDER BY bucket", params
            ).fetchall()
        data = dict(total)
        data["calls"] = int(data.get("calls") or 0)
        data["failures"] = int(data.get("failures") or 0)
        data["models"] = [dict(row) for row in models]
        data["modules"] = [dict(row) for row in modules]
        data["trend"] = [dict(row) for row in trend]
        data["estimated_calls"] = self._count_estimated(start, end)
        data["p95_latency_ms"] = self._percentile_latency(start, end, 0.95)
        return data

    def model_health(self, *, start: float | None = None,
                     end: float | None = None, min_calls: int = 5) -> dict[str, Any]:
        """按 **actual_model** 聚合健康度，输出可供网关路由消费的权重建议。

        2026-11-27 新增（路线图 P1-1「网关健康度回流」）。

        ## 为什么需要它

        目前 Anima 对上游的理解只有「这一枪成功没有」。主模型下架（400）
        或持续 429 时，只能等 fallback 链逐个撞过去，或者人工发现后改配置。
        而 `llm_usage_events` 里其实**已经存了每次调用的成功/失败与延迟**
        —— 缺的只是按模型聚合出来、并且给出可直接用于调权的数字。

        ## 权重口径

        `weight` 是 0–1 的连续值，供上层做加权随机或阈值摘除：

          weight = success_rate * latency_factor

          latency_factor = 中位延迟在 [fast_ms, slow_ms] 区间的线性映射，
                           越快越接近 1，越慢越接近 `min_latency_factor`。

        这样「成功率一样」时选更快的；「延迟一样」时选更稳的。

        ## 边界

        - `calls < min_calls` 的模型 `weight=0`、`confident=False`：
          样本不足时**不参与调权**，避免一两枪的运气决定路由。
        - 失败率 100% 的模型 `weight=0` 且 `confident=True`：明确可摘除。
        - 只读账本，不写任何东西；不触发网络。
        """
        where, params = self._where(start, end)
        with self._db() as conn:
            rows = conn.execute(
                "SELECT actual_model model, COUNT(*) calls, "
                "COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END),0) ok_calls, "
                "COALESCE(AVG(latency_ms),0) avg_latency_ms, "
                "COALESCE(MAX(latency_ms),0) max_latency_ms "
                "FROM llm_usage_events" + where +
                " GROUP BY actual_model ORDER BY calls DESC", params
            ).fetchall()
            # 延迟分位单独取（SQLite 无内置 percentile）
            lat_by_model: dict[str, list[float]] = {}
            for m_row in conn.execute(
                "SELECT actual_model model, latency_ms FROM llm_usage_events" + where +
                " ORDER BY actual_model", params
            ):
                lat_by_model.setdefault(m_row["model"], []).append(
                    float(m_row["latency_ms"] or 0)
                )
            # 错误分布：只取失败行的 error 前缀，用于区分「下架」vs「限流」vs「网络」
            err_by_model: dict[str, dict[str, int]] = {}
            for e_row in conn.execute(
                "SELECT actual_model model, error FROM llm_usage_events" + where +
                (" AND status!='ok'" if where else " WHERE status!='ok'"), params
            ):
                bucket = self._classify_error(str(e_row["error"] or ""))
                slot = err_by_model.setdefault(e_row["model"], {})
                slot[bucket] = slot.get(bucket, 0) + 1

        out: list[dict[str, Any]] = []
        for row in rows:
            model = str(row["model"] or "")
            calls = int(row["calls"] or 0)
            ok_calls = int(row["ok_calls"] or 0)
            success_rate = (ok_calls / calls) if calls else 0.0
            lats = sorted(lat_by_model.get(model, []))
            p50 = lats[len(lats) // 2] if lats else 0.0
            p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))] if lats else 0.0
            confident = calls >= max(1, int(min_calls))
            weight = self._health_weight(success_rate, p50) if confident else 0.0
            out.append({
                "model": model,
                "calls": calls,
                "ok_calls": ok_calls,
                "success_rate": round(success_rate, 4),
                "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 1),
                "p50_latency_ms": round(p50, 1),
                "p95_latency_ms": round(p95, 1),
                "max_latency_ms": round(float(row["max_latency_ms"] or 0), 1),
                "errors": err_by_model.get(model, {}),
                "confident": confident,
                "weight": round(weight, 4),
                "recommend": self._health_recommend(confident, success_rate, calls),
            })
        out.sort(key=lambda x: (-x["weight"], -x["calls"]))
        return {
            "generated_at": time.time(),
            "window": {"start": start, "end": end},
            "min_calls": int(min_calls),
            "models": out,
        }

    @staticmethod
    def _classify_error(error: str) -> str:
        """把上游错误归到少数几类，便于判断「该摘除」还是「该重试」。"""
        e = (error or "").lower()
        if not e:
            return "unknown"
        if "model_not_found" in e or "unknown provider" in e or "404" in e:
            return "gone"          # 模型下架 / provider 不存在 → 该摘除
        if "429" in e or "rate" in e or "quota" in e:
            return "rate_limited"  # 限流 → 该降权、冷却后恢复
        if any(k in e for k in ("timeout", "clientoserror", "disconnect",
                                "connection", "transport")):
            return "transport"     # 网络 → 与模型本身无关
        if "400" in e or "invalid" in e:
            return "bad_request"   # 参数/协议问题
        if "auth" in e or "401" in e or "403" in e:
            return "auth"          # 凭据问题
        return "other"

    @staticmethod
    def _health_weight(success_rate: float, p50_latency_ms: float,
                       *, fast_ms: float = 1500.0, slow_ms: float = 15000.0,
                       min_latency_factor: float = 0.4) -> float:
        """成功率 × 延迟因子，得到 0–1 的连续权重。"""
        sr = max(0.0, min(1.0, float(success_rate)))
        if sr <= 0:
            return 0.0
        if p50_latency_ms <= fast_ms:
            lf = 1.0
        elif p50_latency_ms >= slow_ms:
            lf = min_latency_factor
        else:
            ratio = (p50_latency_ms - fast_ms) / (slow_ms - fast_ms)
            lf = 1.0 - ratio * (1.0 - min_latency_factor)
        return max(0.0, min(1.0, sr * lf))

    @staticmethod
    def _health_recommend(confident: bool, success_rate: float, calls: int) -> str:
        if not confident:
            return "insufficient_samples"
        if success_rate >= 0.98:
            return "keep"
        if success_rate >= 0.9:
            return "keep_demote"
        if success_rate >= 0.5:
            return "demote"
        return "drop"

    def _count_estimated(self, start: float | None, end: float | None) -> int:
        where, params = self._where(start, end)
        with self._db() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM llm_usage_events" + where + " AND estimated=1" if where else "SELECT COUNT(*) FROM llm_usage_events WHERE estimated=1", params).fetchone()[0])

    def _percentile_latency(self, start: float | None, end: float | None, percentile: float) -> float:
        where, params = self._where(start, end)
        with self._db() as conn:
            rows = [float(row[0]) for row in conn.execute("SELECT latency_ms FROM llm_usage_events" + where + " ORDER BY latency_ms", params).fetchall()]
        if not rows:
            return 0.0
        return round(rows[min(len(rows) - 1, max(0, int((len(rows) - 1) * percentile)))], 2)

    def export(self, *, start: float | None = None, end: float | None = None, fmt: str = "json") -> tuple[str, str]:
        rows = self.events(start=start, end=end, limit=5000)
        if fmt == "csv":
            out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["created_at"])
            writer.writeheader(); writer.writerows(rows)
            return out.getvalue(), "text/csv"
        return json.dumps(rows, ensure_ascii=False, indent=2), "application/json"


def estimate_tokens(value: Any) -> int:
    """无 usage 返回时的保守估算；响应中明确标记 estimated。"""
    try:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        return max(0, (len(text) + 3) // 4)
    except Exception:
        return 0
