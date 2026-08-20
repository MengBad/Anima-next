import type { EvolutionProposal } from "./types";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function cookie(name: string): string {
  const prefix = `${name}=`;
  const item = document.cookie.split("; ").find((part) => part.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const headers = new Headers(init.headers || {});
  if (!(init.body instanceof FormData) && init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = cookie("anima_webui_csrf");
    if (csrf) headers.set("X-Anima-CSRF", csrf);
  }
  const response = await fetch(path, { ...init, method, headers, credentials: "same-origin" });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new ApiError(401, "登录已失效，请重新登录");
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    // 先按 Content-Type 决定读法，避免 json() 失败后 body 流已读导致 text() 报
    // "body stream already read"（例如 403/非 JSON 错误体）。
    const isJson = (response.headers.get("content-type") || "").includes("application/json");
    try {
      if (isJson) {
        const payload = await response.json();
        message = payload.message || payload.error || JSON.stringify(payload);
      } else {
        const text = await response.text();
        if (text) message = text;
      }
    } catch {
      // 忽略二次解析失败，保留 status + statusText
    }
    throw new ApiError(response.status, message.replace(/^\d+: /, ""));
  }
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") || "";
  return (type.includes("application/json") ? response.json() : response.text()) as Promise<T>;
}

/** Optional dashboard data must never make an entire page unavailable. */
export async function bestEffort<T>(request: Promise<T>, fallback: T): Promise<T> {
  try { return await request; } catch { return fallback; }
}

export function json(method: string, body?: unknown): RequestInit {
  return { method, body: body === undefined ? undefined : JSON.stringify(body) };
}

export function formatNumber(value: unknown): string {
  const number = Number(value || 0);
  return new Intl.NumberFormat("zh-CN", { notation: number >= 10000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(number);
}

export function formatTime(value: unknown): string {
  const number = Number(value || 0);
  if (!number) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(number * 1000));
}

export interface EvolutionDecision {
  id: string;
  decision: string;
  issues?: string[];
  message?: string;
}

export async function getEvolutionProposals(): Promise<{ items: EvolutionProposal[] }> {
  return api<{ items: EvolutionProposal[] }>("/api/evolution/proposals");
}

export async function proposeEvolution(goal: string, targetFiles: string[] = []): Promise<{ proposal: EvolutionProposal | null; decision: EvolutionDecision }> {
  return api<{ proposal: EvolutionProposal | null; decision: EvolutionDecision }>(
    "/api/evolution/propose",
    json("POST", { goal, target_files: targetFiles }),
  );
}

export async function approveEvolution(proposalId: string): Promise<EvolutionDecision> {
  return api<EvolutionDecision>(`/api/evolution/${encodeURIComponent(proposalId)}/approve`, json("POST", {}));
}

export async function rejectEvolution(proposalId: string, reason = ""): Promise<EvolutionDecision> {
  return api<EvolutionDecision>(`/api/evolution/${encodeURIComponent(proposalId)}/reject`, json("POST", { reason }));
}

export function timeAgo(value: unknown): string {
  const seconds = Math.max(0, Date.now() / 1000 - Number(value || 0));
  if (!value) return "从未";
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

// ---------------------------------------------------------------------------
// ANIMA OS 治理层（P0–P6 后端只读聚合 + 受限指令）
// ---------------------------------------------------------------------------
export interface OsCommandResult {
  command: {
    intent: string;
    needs_confirmation: boolean;
    preview?: string;
    confirmation_endpoint?: string;
  };
  [key: string]: unknown;
}

export async function getOsOverview(): Promise<Record<string, unknown>> {
  return api<Record<string, unknown>>("/api/os/overview");
}

export async function postOsCommand(command: string): Promise<OsCommandResult> {
  return api<OsCommandResult>("/api/os/command", json("POST", { command }));
}
