<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  Activity, ArrowUpRight, Boxes, BrainCircuit, CircleX, Cpu, Database, GitBranch,
  HeartPulse, History, Layers, Lightbulb, Network, RefreshCw, Scale, ShieldCheck, Sparkles
} from "@lucide/vue";
import { bestEffort, getOsOverview, postOsCommand, timeAgo, type OsCommandResult } from "../api";
import { navigate } from "../navigation";
import StatusDot from "../components/StatusDot.vue";
import Card from "../components/Card.vue";
import SectionHeader from "../components/SectionHeader.vue";
import StateBlock from "../components/StateBlock.vue";
import { useCommandHistory } from "../composables/useCommandHistory";

type Dict = Record<string, any>;

const overview = ref<Dict | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);

const commandText = ref("");
const running = ref(false);
const commandResult = ref<OsCommandResult | null>(null);
const commandError = ref<string | null>(null);
const showHistory = ref(false);
const { history, push: pushHistory, clear: clearHistory } = useCommandHistory();

const SUGGESTIONS = [
  "ANIMA 昨天有没有异常？",
  "演化影响评分怎么样？效果值不值？",
  "做一次依赖自检，看看有没有过期依赖",
  "Agent 社会辩论：这个架构改动合不合理？",
  "你还记得我的习惯吗？为什么这么设计？"
];

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  const data = await bestEffort(getOsOverview(), null);
  if (data === null) error.value = "无法读取 ANIMA OS 概览";
  overview.value = data;
  loading.value = false;
}

async function runCommand(text: string): Promise<void> {
  const q = (text || "").trim();
  if (!q || running.value) return;
  showHistory.value = false;
  pushHistory(q);
  running.value = true;
  commandError.value = null;
  commandResult.value = null;
  try {
    commandResult.value = await postOsCommand(q);
  } catch (e: any) {
    commandError.value = e?.message || "指令执行失败";
  } finally {
    running.value = false;
  }
}

onMounted(load);

const ov = computed(() => (overview.value?.overview as Dict) || {});
const stability = computed(() => (overview.value?.stability as Dict) || {});
const environment = computed(() => (overview.value?.environment as Dict) || {});
const impact = computed(() => (overview.value?.impact as Dict) || {});
const memory = computed(() => (overview.value?.memory as Dict) || {});
const maintenance = computed(() => (overview.value?.maintenance as Dict) || {});
const skill = computed(() => (overview.value?.skill as Dict) || {});

const osTone = computed(() => (ov.value?.degraded ? "attention" : "alive"));
const osCopy = computed(() => (osTone.value === "attention" ? "有治理层需要留意" : "运行、观察、自治、进化链路就绪"));

function stableValue(window: string, key: string): string {
  const w = stability.value?.[window] as Dict | undefined;
  if (!w) return "—";
  const v = w[key];
  if (v === undefined || v === null) return "—";
  return String(v);
}

const impactScore = computed(() => {
  const s = impact.value?.overall_score;
  if (s === undefined || s === null) return null;
  return Number(s);
});
const impactHasData = computed(() => Boolean(impact.value?.has_data));
const impactSuggestions = computed(() => (impact.value?.suggestions as any[]) || []);
const envHasData = computed(() => Boolean(environment.value?.has_data));
const envSignals = computed(() => (environment.value?.signals as any[]) || []);
const memorySummary = computed(() => (memory.value?.available === false ? null : memory.value));
const maintenanceProposed = computed(() => ((maintenance.value?.proposals as any[]) || []).filter((p) => p.proposal).length);
const maintenanceSkipped = computed(() => Number(maintenance.value?.skipped || 0));
const lastCommand = computed(() => commandResult.value);
const cmd = computed(() => lastCommand.value?.command as Dict | undefined);

const state = computed<"loading" | "error" | "empty" | "ready">(() => {
  if (loading.value && !overview.value) return "loading";
  if (error.value) return "error";
  if (overview === null) return "empty";
  return "ready";
});
</script>

<template>
  <section class="page os-page">
    <SectionHeader title="ANIMA OS" hint="把长期运行、真实观察、自治维护与受控进化放在同一视野里。只读、可审计、等你批准。" />

    <StateBlock
      v-if="state !== 'ready'"
      :state="state === 'empty' ? 'empty' : state"
      :loading-rows="4"
      empty-title="没有治理数据"
      empty-detail="先部署最新 master 并运行一段时间，OS 治理层会逐渐填充。"
      :error-title="error || '读取失败'"
      error-detail="请稍后重试，或确认服务已部署最新版本。"
    />

    <template v-else>
      <div class="command-bar card card-pad">
        <div class="command-input-row">
          <div class="command-input-wrap">
            <History :size="15" class="history-icon" @click="showHistory = !showHistory" />
            <input
              v-model="commandText"
              class="command-input"
              type="text"
              placeholder="问 ANIMA OS：昨天有没有异常？演化影响评分？做一次依赖自检？"
              @keyup.enter="runCommand(commandText)"
              @focus="history.length && (showHistory = true)"
            />
            <div v-if="showHistory && history.length" class="history-drop">
              <button v-for="h in history" :key="h" class="history-item" @click="commandText = h; showHistory = false">
                <History :size="13" />{{ h }}
              </button>
              <button class="history-clear" @click="clearHistory(); showHistory = false">清空历史</button>
            </div>
          </div>
          <button class="action-button" :disabled="running || !commandText.trim()" @click="runCommand(commandText)">
            <Sparkles :size="15" />{{ running ? "执行中" : "运行" }}
          </button>
        </div>
        <div class="command-suggestions">
          <button v-for="s in SUGGESTIONS" :key="s" class="suggestion-chip" @click="runCommand(s)">{{ s }}</button>
        </div>

        <div v-if="commandError" class="command-error">{{ commandError }}</div>
        <div v-else-if="cmd" class="command-result">
          <div class="command-result-head">
            <StatusDot :status="cmd.needs_confirmation ? 'warn' : 'running'" :label="cmd.needs_confirmation ? '需 Owner 批准' : '只读'" />
            <span class="command-intent">{{ cmd.intent }}</span>
            <span class="command-preview">{{ cmd.preview }}</span>
          </div>
          <div v-if="cmd.needs_confirmation" class="command-gated">
            <ShieldCheck :size="15" />特权动作已生成待确认指令，请到<button class="text-action" @click="navigate('engineering')">审批中心</button>完成授权。绝不自我执行。
          </div>
          <pre v-else class="command-json">{{ JSON.stringify(lastCommand, null, 2) }}</pre>
        </div>
      </div>

      <div class="os-grid">
        <Card class="os-card" :enter-index="0" :class="osTone">
          <div class="os-card-head"><HeartPulse :size="17" /><strong>每日健康证明</strong><StatusDot :status="osTone === 'alive' ? 'running' : 'warn'" :label="osTone === 'alive' ? '正常' : '需留意'" /></div>
          <div class="os-card-body">
            <div class="kv"><span>版本</span><b>{{ ov.version || '—' }}</b></div>
            <div class="kv"><span>提交</span><b class="mono">{{ String(ov.commit_sha || '—').slice(0, 10) }}</b></div>
            <div class="kv"><span>权限异常</span><b :class="ov.permission_anomaly ? 'bad' : 'good'">{{ ov.permission_anomaly ? '有' : '无' }}</b></div>
            <div class="kv"><span>Audit 异常</span><b :class="ov.audit_anomaly ? 'bad' : 'good'">{{ ov.audit_anomaly ? '有' : '无' }}</b></div>
            <div class="kv"><span>Agent 状态</span><b>{{ ov.agent_status || '—' }}</b></div>
          </div>
          <div class="os-card-foot">{{ ov.attested_at ? '证明于 ' + timeAgo(ov.attested_at) : '尚无证明显式记录' }}</div>
        </Card>

        <Card class="os-card" :enter-index="1">
          <div class="os-card-head"><Activity :size="17" /><strong>长期运行稳定</strong><Scale :size="14" class="muted" /></div>
          <div class="window-tabs"><span>7天</span><span>30天</span><span>90天</span></div>
          <div class="os-card-body compact">
            <div class="kv"><span>uptime 7d</span><b>{{ stableValue('7d', 'uptime_ratio') }}</b></div>
            <div class="kv"><span>异常 7d</span><b :class="Number(stableValue('7d','anomaly_total'))>0?'bad':'good'">{{ stableValue('7d', 'anomaly_total') }}</b></div>
            <div class="kv"><span>权限漂移 7d</span><b :class="Number(stableValue('7d','permission_drift_count'))>0?'bad':'good'">{{ stableValue('7d', 'permission_drift_count') }}</b></div>
            <div class="kv"><span>演化计数 7d</span><b>{{ stableValue('7d', 'evolution_count') }}</b></div>
          </div>
          <div class="os-card-foot">30d 异常 {{ stableValue('30d','anomaly_total') }} · 90d 异常 {{ stableValue('90d','anomaly_total') }}</div>
        </Card>

        <Card class="os-card" :enter-index="2">
          <div class="os-card-head"><GitBranch :size="17" /><strong>真实环境观测</strong><Network :size="14" class="muted" /></div>
          <div class="os-card-body">
            <div v-if="envHasData" class="signals">
              <span v-for="s in envSignals.slice(0,4)" :key="s.source" class="signal-chip" :class="`tone-${s.tone || 'info'}`">{{ s.source }} · {{ s.count }}</span>
            </div>
            <div v-else class="muted-note">尚未接入真实观测源（GitHub/Audit/性能）。先让系统跑一段时间。</div>
          </div>
          <div class="os-card-foot">{{ environment.has_data ? '有观测数据' : '数据为空 → 诚实降级' }}</div>
        </Card>

        <Card class="os-card" :enter-index="3">
          <div class="os-card-head"><BrainCircuit :size="17" /><strong>演化影响评分</strong><Sparkles :size="14" class="muted" /></div>
          <div class="os-card-body">
            <div class="big-score" :class="impactScore === null ? 'null' : impactScore >= 0.5 ? 'good' : 'warn'">
              {{ impactScore === null ? '—' : impactScore.toFixed(2) }}
            </div>
            <div class="muted-note" v-if="!impactHasData">无历史 Before/After 证据，评分记 0（诚实）。</div>
            <ul v-else class="suggest-list">
              <li v-for="(sg, i) in impactSuggestions.slice(0,3)" :key="i"><Lightbulb :size="13" />{{ sg.text }}</li>
            </ul>
          </div>
          <div class="os-card-foot">{{ impactHasData ? '基于历史证据语料' : '等待演化产生证据' }}</div>
        </Card>

        <Card class="os-card" :enter-index="4">
          <div class="os-card-head"><Network :size="17" /><strong>Agent 社会</strong><Layers :size="14" class="muted" /></div>
          <div class="os-card-body">
            <div class="muted-note">多视角辩论（架构/安全/研究/编码/批判/裁判）。用命令栏发起一次辩论，例如「Agent 社会辩论这个改动」。</div>
          </div>
          <div class="os-card-foot">结论建议性，不授权执行</div>
        </Card>

        <Card class="os-card interactive" :enter-index="5" role="button" tabindex="0" :aria-label="'查询长期记忆'" @click="runCommand('你还记得我的习惯吗？为什么这么设计？')" @keydown.enter="runCommand('你还记得我的习惯吗？为什么这么设计？')" @keydown.space.prevent="runCommand('你还记得我的习惯吗？为什么这么设计？')">
          <div class="os-card-head"><Database :size="17" /><strong>长期记忆</strong><Cpu :size="14" class="muted" /></div>
          <div class="os-card-body" v-if="memorySummary">
            <div class="kv"><span>知识</span><b>{{ memorySummary.knowledge_count ?? '—' }}</b></div>
            <div class="kv"><span>行为模式</span><b>{{ memorySummary.pattern_count ?? '—' }}</b></div>
            <div class="kv"><span>设计原因</span><b>{{ memorySummary.why_designed_count ?? '—' }}</b></div>
          </div>
          <div class="os-card-body" v-else>
            <div class="muted-note">暂无记忆数据。运行越久，经验积累越厚。</div>
          </div>
          <div class="os-card-foot clickable">点击查询「你还记得我的习惯吗？」</div>
        </Card>

        <Card class="os-card" :enter-index="6">
          <div class="os-card-head"><ShieldCheck :size="17" /><strong>自治维护</strong><Cpu :size="14" class="muted" /></div>
          <div class="os-card-body">
            <div v-if="maintenance.available === false" class="muted-note">{{ maintenance.reason || '维护扫描不可用' }}</div>
            <div v-else>
              <div class="kv"><span>待审提案</span><b>{{ maintenanceProposed }}</b></div>
              <div class="kv"><span>跳过</span><b>{{ maintenanceSkipped }}</b></div>
              <div class="muted-note small">工程准备自动，执行等 Owner 批准。</div>
            </div>
          </div>
          <div class="os-card-foot">绝不自我安装</div>
        </Card>

        <Card class="os-card" :enter-index="7">
          <div class="os-card-head"><Boxes :size="17" /><strong>个人能力演进</strong><Sparkles :size="14" class="muted" /></div>
          <div class="os-card-body">
            <div v-if="skill.available === false" class="muted-note">{{ skill.reason || '能力演进不可用' }}</div>
            <div v-else class="muted-note">Skill 提案→沙箱→Benchmark→采纳判定。采纳仍须 Owner。</div>
          </div>
          <div class="os-card-foot">复用 SkillDraftManager 护栏</div>
        </Card>
      </div>
    </template>
  </section>
</template>

<style scoped>
.os-page{max-width:1180px;margin:0 auto}
.os-error{display:flex;align-items:center;gap:8px;color:var(--red);border-left:2px solid var(--red);padding:12px 14px;margin-bottom:14px}
.command-bar{margin-bottom:14px}
.command-input-row{display:flex;gap:10px}
.command-input-wrap{position:relative;flex:1;display:flex;align-items:center}
.history-icon{position:absolute;left:12px;color:var(--muted);cursor:pointer}
.command-input{flex:1;background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:11px;padding:11px 13px 11px 36px;color:var(--ink);font-size:var(--fs-sm);outline:none}
.command-input:focus{border-color:var(--lime)}
.history-drop{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:40;display:grid;border:1px solid var(--line);border-radius:11px;overflow:hidden;background:var(--panel-solid);box-shadow:var(--shadow-md)}
.history-item{display:flex;align-items:center;gap:8px;padding:9px 13px;border:0;background:transparent;color:var(--ink-soft);font-size:var(--fs-sm);text-align:left;cursor:pointer}
.history-item:hover{background:rgba(196,246,110,.07)}
.history-clear{justify-self:start;padding:8px 13px;border:0;border-top:1px solid var(--line);background:transparent;color:var(--muted);font-size:var(--fs-xs);cursor:pointer}
.command-suggestions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.suggestion-chip{background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:999px;padding:6px 11px;font-size:var(--fs-sm);color:var(--muted);cursor:pointer;transition:.15s}
.suggestion-chip:hover{border-color:var(--lime);color:var(--ink-soft)}
.command-error{margin-top:10px;color:var(--red);font-size:var(--fs-xs)}
.command-result{margin-top:12px;border-top:1px solid var(--line);padding-top:12px}
.command-result-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.command-intent{font:700 var(--fs-sm) ui-monospace,monospace;letter-spacing:.6px;color:var(--cyan);text-transform:uppercase}
.command-preview{color:var(--muted);font-size:var(--fs-sm)}
.command-gated{display:flex;align-items:center;gap:7px;margin-top:9px;font-size:var(--fs-sm);color:var(--amber)}
.command-json{margin:9px 0 0;background:rgba(0,0,0,.28);border:1px solid var(--line);border-radius:10px;padding:11px;font-size:var(--fs-xs);line-height:1.5;color:var(--ink-soft);max-height:280px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.os-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:var(--space-grid)}
.os-card{padding:var(--space-card);display:flex;flex-direction:column;gap:10px}
.os-card.attention{border-left:2px solid var(--amber)}
.os-card-head{display:flex;align-items:center;gap:8px}
.os-card-head strong{font-size:var(--fs-title);flex:1}
.os-card-head .muted{color:var(--muted)}
.os-card-body{display:flex;flex-direction:column;gap:6px}
.os-card-body.compact{gap:4px}
.kv{display:flex;justify-content:space-between;align-items:center;font-size:var(--fs-sm)}
.kv span{color:var(--muted)}
.kv b{font-weight:600}
.kv b.good{color:var(--lime)}.kv b.bad{color:var(--red)}.kv b.mono{font:600 var(--fs-sm) ui-monospace,monospace}
.big-score{font:800 30px ui-monospace,monospace;text-align:center;padding:4px 0}
.big-score.good{color:var(--lime)}.big-score.warn{color:var(--amber)}.big-score.null{color:var(--muted)}
.muted-note{font-size:var(--fs-xs);color:var(--muted);line-height:1.5}
.muted-note.small{font-size:var(--fs-xs);margin-top:4px}
.muted{color:var(--muted)}
.signals{display:flex;flex-wrap:wrap;gap:5px}
.signal-chip{font-size:var(--fs-xs);padding:4px 7px;border-radius:999px;border:1px solid var(--line)}
.signal-chip{border-color:rgba(120,220,255,.25)}
.signal-chip.tone-info{color:var(--cyan)}
.signal-chip.tone-warn{color:var(--amber);border-color:rgba(255,201,107,.25)}
.signal-chip.tone-bad{color:var(--red);border-color:rgba(255,123,133,.25)}
.suggest-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:5px}
.suggest-list li{display:flex;gap:6px;align-items:flex-start;font-size:var(--fs-sm);color:var(--ink-soft);line-height:1.4}
.suggest-list li :deep(svg){color:var(--lime);flex:0 0 auto;margin-top:1px}
.window-tabs{display:flex;gap:8px;font-size:var(--fs-xs);color:var(--muted)}
.os-card-foot{font-size:var(--fs-xs);color:var(--muted);border-top:1px solid var(--line);padding-top:8px}
.os-card-foot.clickable{color:var(--lime)}
@media(max-width:1150px){.os-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.os-grid{grid-template-columns:1fr}.command-input-row{flex-direction:column}.command-input-row .action-button{width:100%}}
</style>
