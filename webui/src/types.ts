export type StatusTone = "good" | "warn" | "bad" | "quiet";

export interface Health {
  running: boolean;
  channel_bound: boolean;
  channel_connected: boolean;
  heartbeat_running: boolean;
  timestamp: number;
  history?: { samples: number; latest_at?: number | null; retention_samples?: number; sample_interval_sec?: number };
  llm?: Record<string, any>;
  audio?: Record<string, any>;
  sylann?: Record<string, any>;
  watchdog?: Record<string, any>;
  config_recovery?: {
    state: string;
    change_id?: string;
    armed_at?: number;
    attempt_started_at?: number;
    healthy_at?: number;
    rollback_reason?: string;
  };
}

export interface HealthSample {
  ts: number;
  running: boolean;
  channel_connected: boolean;
  heartbeat_running: boolean;
  watchdog_running: boolean;
  watchdog_lag_sec: number;
  watchdog_max_lag_sec: number;
  watchdog_late_ticks: number;
  llm_requests: number;
  llm_failures: number;
  sylann_observations: number;
  sylann_ticks: number;
  sylann_errors: number;
  sylann_responses: number;
  sylann_expression_hints: number;
  audio_requests: number;
  audio_successes: number;
  audio_failures: number;
  audio_throttled: number;
  agenda_pending: number;
  approvals_pending: number;
  subscriptions_total: number;
  subscriptions_enabled: number;
  subscriptions_live: number;
  skill_drafts_pending: number;
}

export interface ModuleStatus {
  id: string;
  label: string;
  status: string;
  message: string;
  metrics: Record<string, any>;
}

export interface UsageSummary {
  calls: number;
  failures: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens?: number;
  total_tokens: number;
  estimated_calls: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  models: Array<{ model: string; calls: number; total_tokens: number }>;
  modules: Array<{ module: string; calls: number; total_tokens: number }>;
}

export interface ConfigField {
  path: string;
  label: string;
  type: "url" | "secret" | "text" | "csv" | "number" | "boolean" | "select" | "providers" | "routes" | "group_policies";
  min?: number;
  max?: number;
  options?: string[];
  risk: "green" | "yellow" | "red";
  restart: boolean;
  impact: string[];
}

export interface StickerAsset {
  id: string;
  labels: string[];
  emotion: string;
  category: string;
  status: string;
  source_group: string;
  scope: string;
  is_sticker: boolean | null;
  size_bytes: number;
  created_at: number;
  last_used: number;
  used: number;
}

export interface ToastMessage {
  id: number;
  tone: "success" | "error" | "info";
  title: string;
  detail?: string;
}

export interface EvolutionProposal {
  id: string;
  title: string;
  rationale: string;
  target_files: string[];
  diff_summary: string;
  risk: string;
  rollback: string;
  evidence: string;
  status: string;
  decision: string;
  touches_core: boolean;
  created_at: number;
  evidence_jury?: { decision?: string; reasons?: string[] } | null;
  society_review?: { available?: boolean; verdict?: { decision?: string } } | null;
  ci_dod?: { pass?: boolean; reasons?: string[] } | null;
}
