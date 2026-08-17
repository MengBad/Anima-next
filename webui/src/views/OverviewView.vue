<script setup lang="ts">
import { computed } from "vue";
import {
  Activity, ArrowUpRight, Bot, BrainCircuit, CircleAlert, Clock3, Cpu,
  HeartPulse, MessageCircleMore, Radio, ShieldCheck, Sparkles, Waves
} from "@lucide/vue";
import { formatNumber, timeAgo } from "../api";
import { navigate } from "../navigation";
import type { Health, HealthSample, ModuleStatus, UsageSummary } from "../types";
import StatusDot from "../components/StatusDot.vue";
import EmptyState from "../components/EmptyState.vue";

const props = defineProps<{
  health: Health | null;
  modules: ModuleStatus[];
  usage: UsageSummary | null;
  quality: any | null;
  ops: any | null;
  tasks: any[];
  approvals: any[];
  timeline: any[];
  subject: Record<string, any> | null;
  history: HealthSample[];
  loading: boolean;
}>();

const urgent = computed(() => [
  ...props.tasks.filter((item) => item.status === "awaiting_approval").map((item) => ({ ...item, type: "工程候选" })),
  ...props.approvals.filter((item) => item.status === "pending").map((item) => ({ ...item, type: "能力审批" }))
].slice(0, 4));
const degraded = computed(() => props.modules.filter((item) => ["degraded", "failed", "stopped"].includes(item.status)));
const lifeTone = computed(() => !props.health?.running ? "offline" : degraded.value.length ? "attention" : "alive");
const lifeCopy = computed(() => lifeTone.value === "alive" ? "感知、记忆与行动链路正在协同" : lifeTone.value === "attention" ? `有 ${degraded.value.length} 个模块需要留意` : "核心暂时失去联系");
const recoveryNotice = computed(() => {
  const state = props.health?.config_recovery?.state;
  if (state === "healthy") return { tone: "good", title: "配置已通过启动健康点", detail: `变更 ${props.health?.config_recovery?.change_id || "未知"} 已加载并稳定运行。` };
  if (state === "attempting") return { tone: "warn", title: "配置正在进行启动检查", detail: "健康点尚未确认，系统会在失败时自动恢复上一版本。" };
  return null;
});
const llmDegradationNotice = computed(() => {
  const llm = props.health?.llm;
  if (!llm?.last_degradation_at) return null;
  const before = Number(llm.last_payload_bytes || 0);
  const after = Number(llm.last_effective_payload_bytes || 0);
  const reason = llm.last_degradation_reason === "http_413_trim_retry" ? "网关拒绝后恢复" : "发送前自动保护";
  const size = (value: number) => `${(value / 1024 / 1024).toFixed(value >= 10 * 1024 * 1024 ? 1 : 2)} MB`;
  return {
    title: "最近一次大图已安全处理",
    detail: `${reason}：请求从 ${size(before)} 缩至 ${size(after)}。模型仍完成了本次调用。`
  };
});
const llmFailoverNotice = computed(() => {
  const llm = props.health?.llm;
  if (!llm?.last_failover_at) return null;
  if (Number(llm.circuit_open_until || 0) * 1000 > Date.now()) {
    return { title: "模型网关正在保护性冷却", detail: "连续可恢复失败后已暂停短暂重试；冷却结束会自动恢复，不会让 QQ 消息无限堆积。" };
  }
  return {
    title: "最近一次模型已自动切换",
    detail: `${llm.last_failover_from || "默认模型"} 暂时不可用，已使用 ${llm.last_failover_to || "备用模型"} 完成请求。`
  };
});
const healthRail = computed(() => [
  { label: "QQ 通道", value: props.health?.channel_connected ? "已连接" : "待连接", ok: !!props.health?.channel_connected, icon: MessageCircleMore },
  { label: "LLM", value: props.health?.llm?.current_state === "healthy" ? "当前正常" : props.health?.llm?.current_state === "degraded" ? "当前异常" : props.health?.llm?.current_state === "circuit_open" ? "熔断中" : "等待请求", ok: !!props.health?.llm && ["healthy","ready"].includes(props.health?.llm?.current_state || "ready"), icon: Sparkles },
  { label: "Sylann", value: props.health?.sylann?.mode || "未启用", ok: !!props.health?.sylann?.enabled, icon: BrainCircuit },
  { label: "心跳", value: props.health?.heartbeat_running ? "持续运行" : "已暂停", ok: !!props.health?.heartbeat_running, icon: HeartPulse },
  { label: "看门狗", value: props.health?.watchdog?.running ? `${Number(props.health?.watchdog?.last_lag_sec || 0).toFixed(3)}s` : "未运行", ok: !!props.health?.watchdog?.running, icon: ShieldCheck }
]);
const topModels = computed(() => (props.usage?.models || []).slice(0, 4));
const qualitySignals = computed(() => (props.quality?.signals || []).slice(0, 3));
const qualityTone = computed(() => props.quality?.status || "healthy");
const qualityTitle = computed(() => qualityTone.value === "attention" ? "有需要处理的回复问题" : qualityTone.value === "watch" ? "有轻微质量信号" : "对话表现稳定");
const opsSignals = computed(() => (props.ops?.signals || []).slice(0, 3));
const opsTone = computed(() => props.ops?.status || "healthy");
const opsTitle = computed(() => opsTone.value === "attention" ? "运行面需要处理" : opsTone.value === "watch" ? "运行面有轻微信号" : "运行面稳定");
const opsLatency = computed(() => {
  const avg = Number(props.ops?.metrics?.avg_latency_sec || 0);
  const p95 = Number(props.ops?.metrics?.p95_latency_sec || 0);
  if (!avg && !p95) return "今日暂无延迟样本";
  return `均 ${avg.toFixed(2)}s · P95 ${p95.toFixed(2)}s`;
});
const maxModelTokens = computed(() => Math.max(1, ...topModels.value.map((item) => item.total_tokens)));
const activeDesire = computed(() => props.subject?.active_desires?.[0]);
const trendBars = computed(() => props.history.slice(-24));
const latestTrend = computed(() => trendBars.value.at(-1));
const maxLag = computed(() => Math.max(0.1, ...trendBars.value.map((item) => Number(item.watchdog_lag_sec || 0))));
const trendSummary = computed(() => {
  const latest = trendBars.value.at(-1);
  if (!latest) return "等待第一个心跳样本";
  if (!latest.running || !latest.heartbeat_running) return "心跳曾中断，正在等待恢复证据";
  if (!latest.channel_connected) return "QQ 通道尚未连接";
  if (latest.watchdog_lag_sec >= 2) return "事件循环出现延迟，建议查看日志";
  return `最近 ${trendBars.value.length} 个样本均已记录`;
});

function eventTitle(item: any): string {
  const names: Record<string, string> = {
    message_received: "收到一条新消息", llm_completed: "完成一次模型思考",
    webui_config_applied: "配置已经应用", config_auto_rolled_back: "配置启动失败，已自动回滚", webui_sticker_approved: "表情资产通过审核",
    agenda_executed: "完成一项议程", heartbeat_tick: "心跳完成"
  };
  return names[item.type] || String(item.type || "系统活动").replaceAll("_", " ");
}
</script>

<template>
  <section class="page overview-page">
    <div class="page-heading">
      <div><span class="eyebrow">living overview</span><h1>她现在，活得怎么样？</h1><p>把健康、内在状态和需要你决定的事项放在同一视野里。</p></div>
      <span class="last-sync"><Clock3 :size="14" />{{ health ? timeAgo(health.timestamp) + '同步' : '正在读取' }}</span>
    </div>

    <div v-if="loading && !health" class="overview-skeleton"><div class="skeleton" /><div class="skeleton" /></div>
    <template v-else>
      <div class="life-grid">
        <article class="life-core panel" :class="lifeTone">
          <div class="life-orbit" aria-hidden="true"><span class="orbit orbit-a" /><span class="orbit orbit-b" /><span class="core-face">H</span><i class="signal signal-a" /><i class="signal signal-b" /></div>
          <div class="life-copy">
            <div class="life-kicker"><span />ANIMA CORE</div>
            <h2>{{ health?.running ? '我在这里。' : '暂时听不见她。' }}</h2>
            <p>{{ lifeCopy }}</p>
            <div class="life-meta">
              <span><Radio :size="14" />{{ health?.llm?.model || '模型待命' }}</span>
              <span><Waves :size="14" />{{ subject?.local_time ? new Date(subject.local_time).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '时间同步中' }}</span>
            </div>
          </div>
        </article>

        <article class="attention-panel panel">
          <div class="attention-head"><div><span>OWNER INBOX</span><h2>等你决定</h2></div><button class="icon-button" aria-label="进入审批中心" @click="navigate('engineering')"><ArrowUpRight :size="18" /></button></div>
          <div v-if="urgent.length" class="urgent-list">
            <button v-for="item in urgent" :key="item.id" @click="navigate('engineering')">
              <span class="urgent-mark"><CircleAlert :size="15" /></span>
              <span><small>{{ item.type }}</small><strong>{{ item.request || item.title || item.id }}</strong></span>
              <ArrowUpRight :size="15" />
            </button>
          </div>
          <EmptyState v-else title="没有悬而未决的事" detail="工程候选和高风险能力会在这里等待你的判断。" />
        </article>
      </div>

      <div class="health-rail panel" aria-label="关键链路健康状态">
        <div v-for="item in healthRail" :key="item.label" class="health-node" :class="{ ok: item.ok }">
          <component :is="item.icon" :size="17" /><span><small>{{ item.label }}</small><strong>{{ item.value }}</strong></span><i />
        </div>
      </div>
      <div v-if="recoveryNotice" class="recovery-banner panel" :class="`recovery-${recoveryNotice.tone}`">
        <ShieldCheck :size="18" /><span><strong>{{ recoveryNotice.title }}</strong><small>{{ recoveryNotice.detail }}</small></span>
      </div>
      <div v-if="llmDegradationNotice" class="recovery-banner panel recovery-warn">
        <Sparkles :size="18" /><span><strong>{{ llmDegradationNotice.title }}</strong><small>{{ llmDegradationNotice.detail }}</small></span>
      </div>
      <div v-if="llmFailoverNotice" class="recovery-banner panel recovery-warn">
        <CircleAlert :size="18" /><span><strong>{{ llmFailoverNotice.title }}</strong><small>{{ llmFailoverNotice.detail }}</small></span>
      </div>

      <div class="overview-columns">
        <section class="panel module-panel">
          <div class="panel-head"><div><strong>身体系统</strong><span>每个模块只汇报自己的职责与状态</span></div><StatusDot status="running" :label="`${modules.length} 个模块`" /></div>
          <div class="module-list">
            <div v-for="item in modules.slice(0, 12)" :key="item.id" class="module-row">
              <span class="module-icon"><Cpu v-if="item.id === 'llm'" :size="16" /><Bot v-else-if="item.id === 'gateway'" :size="16" /><Activity v-else :size="16" /></span>
              <span class="module-copy"><strong>{{ item.label }}</strong><small>{{ item.message }}</small></span>
              <StatusDot :status="item.status" :label="item.status === 'running' ? '运行中' : item.status === 'ready' ? '就绪' : item.status === 'disabled' ? '未启用' : item.status" />
            </div>
          </div>
          <button class="text-action" @click="navigate('settings')">查看全部模块与诊断 <ArrowUpRight :size="14" /></button>
        </section>

        <div class="right-stack">
          <section class="usage-panel panel">
            <div class="panel-head"><div><strong>今天的思考</strong><span>只记录用量元数据，不保存对话正文</span></div><Sparkles :size="18" /></div>
            <div class="usage-summary">
              <div><span class="usage-number metric">{{ formatNumber(usage?.total_tokens) }}</span><small>总 Token</small></div>
              <div><strong class="metric">{{ formatNumber(usage?.calls) }}</strong><small>模型调用</small></div>
              <div><strong class="metric">{{ Number(usage?.p95_latency_ms || 0).toFixed(0) }}ms</strong><small>P95 延迟</small></div>
            </div>
            <div class="model-bars">
              <div v-for="model in topModels" :key="model.model" class="model-bar">
                <span><strong>{{ model.model }}</strong><small>{{ formatNumber(model.total_tokens) }}</small></span>
                <i><b :style="{ width: `${Math.max(4, model.total_tokens / maxModelTokens * 100)}%` }" /></i>
              </div>
            </div>
            <button class="text-action" @click="navigate('settings')">打开模型控制中心 <ArrowUpRight :size="14" /></button>
          </section>

          <section class="inner-weather panel">
            <div class="weather-icon"><Sparkles :size="20" /></div>
            <div><small>此刻最强烈的愿望</small><strong>{{ activeDesire?.content || '安静地观察世界' }}</strong><span v-if="activeDesire">强度 {{ Math.round(Number(activeDesire.intensity || 0) * 100) }}%</span></div>
          </section>
          <section class="quality-panel panel" :class="qualityTone">
            <div class="quality-icon"><ShieldCheck v-if="qualityTone === 'healthy'" :size="19" /><CircleAlert v-else :size="19" /></div>
            <div><small>CONVERSATION QUALITY</small><strong>{{ qualityTitle }}</strong><span>{{ quality?.report?.total_replies || 0 }} 次回复 · {{ quality?.report?.sticker_outbounds || 0 }} 次表情发送</span></div>
            <div v-if="qualitySignals.length" class="quality-signals"><span v-for="signal in qualitySignals" :key="signal.key">{{ signal.label }} {{ signal.count }}</span></div>
            <span v-else class="quality-clean">今日未发现不可用、复读或身份泄漏</span>
          </section>
          <section class="quality-panel panel" :class="opsTone">
            <div class="quality-icon"><Activity v-if="opsTone === 'healthy'" :size="19" /><CircleAlert v-else :size="19" /></div>
            <div><small>RUNTIME QUALITY</small><strong>{{ opsTitle }}</strong><span>{{ opsLatency }} · 背压 {{ ops?.metrics?.events_backpressure_dropped || 0 }} · 失败 {{ ops?.metrics?.llm_failures || 0 }}</span></div>
            <div v-if="opsSignals.length" class="quality-signals"><span v-for="signal in opsSignals" :key="signal.key">{{ signal.label }} {{ signal.count || '' }}</span></div>
            <span v-else class="quality-clean">背压、熔断与出站失败均正常</span>
          </section>
        </div>
      </div>

      <section class="trend-panel panel">
        <div class="panel-head"><div><strong>生命体趋势</strong><span>每分钟一条聚合样本，不包含聊天正文</span></div><StatusDot :status="trendBars.length ? 'running' : 'stopped'" :label="`${trendBars.length} 个样本`" /></div>
        <div class="trend-chart" aria-label="最近健康样本">
          <div v-for="(item, index) in trendBars" :key="item.ts" class="trend-column" :title="new Date(item.ts * 1000).toLocaleString('zh-CN')">
            <i :class="{ bad: !item.running || !item.channel_connected || item.watchdog_lag_sec >= 2 }" :style="{ height: `${Math.max(8, item.watchdog_lag_sec / maxLag * 100)}%` }" />
            <b :class="{ bad: !item.running || !item.channel_connected || item.watchdog_lag_sec >= 2 }" />
            <small v-if="index === 0 || index === trendBars.length - 1">{{ new Date(item.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</small>
          </div>
          <EmptyState v-if="!trendBars.length" title="还没有趋势样本" detail="心跳启动后会自动开始记录。" />
        </div>
        <div class="trend-foot"><span>{{ trendSummary }}</span><span v-if="trendBars.length">最近延迟 {{ Number(latestTrend?.watchdog_lag_sec || 0).toFixed(3) }}s · LLM 失败 {{ latestTrend?.llm_failures || 0 }} 次</span></div>
        <div v-if="latestTrend" class="trend-signals" aria-label="自动化状态">
          <span><i class="signal-dot cyan" />订阅 {{ latestTrend.subscriptions_enabled || 0 }} 项<span v-if="latestTrend.subscriptions_live"> · {{ latestTrend.subscriptions_live }} 项直播中</span></span>
          <span><i class="signal-dot purple" />Skill 待审 {{ latestTrend.skill_drafts_pending || 0 }}</span>
          <span><i class="signal-dot lime" />Sylann 已观察 {{ latestTrend.sylann_observations || 0 }} 次</span>
        </div>
      </section>

      <div class="section-title"><div><h2>最近发生</h2><p>可读事件，而不是原始日志。</p></div></div>
      <div class="timeline-strip panel" v-if="timeline.length">
        <article v-for="item in timeline.slice(0, 8)" :key="`${item.ts}-${item.type}`">
          <i /><span><small>{{ timeAgo(item.ts) }}</small><strong>{{ eventTitle(item) }}</strong></span>
        </article>
      </div>
      <EmptyState v-else title="还没有近期活动" detail="新的感知、审批和自动化事件会依次出现在这里。" />
    </template>
  </section>
</template>

<style scoped>
.last-sync{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:var(--fs-xs)}.overview-skeleton{display:grid;grid-template-columns:1.6fr 1fr;gap:14px}.overview-skeleton>*{height:310px}.life-grid{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(320px,.8fr);gap:14px}.life-core{position:relative;min-height:310px;overflow:hidden;display:flex;align-items:center;padding:clamp(28px,5vw,56px);background:radial-gradient(circle at 20% 48%,rgba(196,246,110,.10),transparent 35%),linear-gradient(130deg,rgba(39,30,51,.94),rgba(19,16,29,.94))}.life-core::after{content:"";position:absolute;inset:0;background:linear-gradient(105deg,transparent 45%,rgba(255,255,255,.035),transparent 60%);transform:translateX(-110%);animation:sweep 6s ease-in-out infinite}@keyframes sweep{50%,100%{transform:translateX(120%)}}.life-orbit{position:relative;width:170px;height:170px;flex:0 0 170px;display:grid;place-items:center;margin-right:clamp(25px,4vw,55px)}.orbit{position:absolute;border:1px solid rgba(196,246,110,.24);border-radius:50%;inset:12px;animation:orbit-spin 13s linear infinite}.orbit::after{content:"";position:absolute;width:7px;height:7px;border-radius:50%;background:var(--lime);top:11px;left:23px;box-shadow:0 0 12px var(--lime)}.orbit-b{inset:0;border-color:rgba(255,143,184,.19);animation-duration:19s;animation-direction:reverse}.orbit-b::after{background:var(--pink);box-shadow:0 0 12px var(--pink);inset:auto 20px 18px auto}@keyframes orbit-spin{to{transform:rotate(360deg)}}.core-face{width:102px;height:102px;display:grid;place-items:center;border-radius:39% 46% 42% 35%;background:linear-gradient(145deg,var(--lime),#91ca42);color:#192610;font:900 42px Georgia,serif;box-shadow:0 0 42px rgba(196,246,110,.22),inset -9px -11px 25px rgba(42,82,18,.23);animation:core-breathe 3.6s ease-in-out infinite}@keyframes core-breathe{50%{transform:scale(1.035) rotate(1deg)}}.signal{position:absolute;width:4px;height:4px;border-radius:50%;background:white}.signal-a{top:34px;right:17px}.signal-b{bottom:43px;left:11px}.life-copy{position:relative;z-index:2}.life-kicker{display:flex;align-items:center;gap:7px;color:var(--lime);font:760 var(--fs-sm) ui-monospace,monospace;letter-spacing:1.4px}.life-kicker span{width:18px;height:1px;background:var(--lime)}.life-copy h2{margin:10px 0 7px;font:760 clamp(29px,4vw,46px) Georgia,"Songti SC",serif}.life-copy p{margin:0;color:var(--ink-soft);font-size:13px}.life-meta{display:flex;gap:17px;flex-wrap:wrap;margin-top:27px}.life-meta span{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:var(--fs-sm)}.attention-panel{min-height:310px;background:linear-gradient(160deg,rgba(62,35,53,.7),rgba(25,19,34,.9))}.attention-head{display:flex;align-items:center;padding:24px 22px 14px}.attention-head>div{flex:1}.attention-head span{color:var(--pink);font:750 var(--fs-sm) ui-monospace,monospace;letter-spacing:1.3px}.attention-head h2{margin:5px 0 0;font:730 20px Georgia,"Songti SC",serif}.urgent-list{padding:2px 13px 14px;display:grid;gap:4px}.urgent-list button{min-height:58px;padding:9px;border:0;border-top:1px solid var(--line);background:transparent;display:grid;grid-template-columns:34px 1fr 18px;align-items:center;gap:9px;text-align:left;cursor:pointer}.urgent-list button:hover{background:rgba(255,143,184,.05)}.urgent-mark{width:30px;height:30px;display:grid;place-items:center;background:rgba(255,143,184,.12);color:var(--pink)}.urgent-list small,.urgent-list strong{display:block}.urgent-list small{color:var(--muted);font-size:var(--fs-sm)}.urgent-list strong{margin-top:3px;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.urgent-list>button>svg{color:var(--muted)}.health-rail{display:grid;grid-template-columns:repeat(5,1fr);margin-top:14px;background:rgba(20,17,29,.8)}.health-node{position:relative;min-height:76px;padding:15px;display:flex;align-items:center;gap:10px;border-right:1px solid var(--line)}.health-node:last-child{border:0}.health-node>svg{color:#6c6272}.health-node.ok>svg{color:var(--lime)}.health-node small,.health-node strong{display:block}.health-node small{color:var(--muted);font-size:var(--fs-sm)}.health-node strong{margin-top:3px;font-size:11px}.health-node>i{position:absolute;right:12px;top:12px;width:5px;height:5px;border-radius:50%;background:#6d6473}.health-node.ok>i{background:var(--lime);box-shadow:0 0 8px var(--lime)}.recovery-banner{display:flex;align-items:center;gap:10px;margin-top:10px;padding:12px 15px;border-left:2px solid var(--lime);background:rgba(196,246,110,.06);color:var(--lime)}.recovery-banner.recovery-warn{border-color:var(--amber);background:rgba(255,201,107,.06);color:var(--amber)}.recovery-banner strong,.recovery-banner small{display:block}.recovery-banner strong{font-size:var(--fs-sm)}.recovery-banner small{margin-top:3px;color:var(--muted);font-size:var(--fs-xs)}.overview-columns{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(340px,.75fr);gap:14px;margin-top:14px}.panel-head>div strong,.panel-head>div span{display:block}.panel-head>div span{margin-top:3px}.module-list{padding:4px 14px}.module-row{display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:10px;min-height:62px;border-bottom:1px solid var(--line)}.module-row:last-child{border:0}.module-icon{width:30px;height:30px;display:grid;place-items:center;background:rgba(255,255,255,.045);color:var(--cyan)}.module-copy strong,.module-copy small{display:block}.module-copy strong{font-size:11px}.module-copy small{margin-top:3px;color:var(--muted);font-size:var(--fs-sm)}.text-action{min-height:42px;border:0;border-top:1px solid var(--line);width:100%;background:transparent;color:var(--lime);display:flex;align-items:center;justify-content:center;gap:7px;font-size:var(--fs-sm);cursor:pointer}.text-action:hover{background:rgba(196,246,110,.04)}.right-stack{display:grid;gap:14px;align-content:start}.usage-panel .panel-head>svg{color:var(--pink)}.usage-summary{display:grid;grid-template-columns:1.45fr 1fr 1fr;padding:18px;border-bottom:1px solid var(--line)}.usage-summary>div{padding-left:14px;border-left:1px solid var(--line)}.usage-summary>div:first-child{padding:0;border:0}.usage-summary span,.usage-summary strong,.usage-summary small{display:block}.usage-number{font:760 27px Georgia,serif;color:var(--lime)}.usage-summary strong{font-size:14px}.usage-summary small{margin-top:4px;color:var(--muted);font-size:var(--fs-xs)}.model-bars{display:grid;gap:13px;padding:18px}.model-bar span{display:flex;justify-content:space-between;gap:10px}.model-bar strong{font-size:var(--fs-sm);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.model-bar small{color:var(--muted);font-size:var(--fs-xs)}.model-bar>i{display:block;height:3px;margin-top:7px;background:rgba(255,255,255,.06)}.model-bar b{display:block;height:100%;background:linear-gradient(90deg,var(--lime),var(--cyan));transition:width 600ms var(--ease-out)}.inner-weather{min-height:92px;padding:18px;display:flex;align-items:center;gap:13px;background:linear-gradient(110deg,rgba(180,154,255,.09),rgba(255,143,184,.04))}.weather-icon{width:42px;height:42px;display:grid;place-items:center;background:rgba(180,154,255,.12);color:var(--purple)}.inner-weather small,.inner-weather strong,.inner-weather span{display:block}.inner-weather small,.inner-weather span{color:var(--muted);font-size:var(--fs-xs)}.inner-weather strong{margin:4px 0;font-size:11px}.timeline-strip{display:grid;grid-template-columns:repeat(4,1fr);padding:10px 14px}.timeline-strip article{position:relative;min-height:72px;padding:12px 13px 12px 20px;border-right:1px solid var(--line);display:flex;gap:9px;align-items:flex-start}.timeline-strip article:nth-child(4n){border:0}.timeline-strip article>i{position:absolute;left:5px;top:18px;width:5px;height:5px;border-radius:50%;background:var(--pink)}.timeline-strip small,.timeline-strip strong{display:block}.timeline-strip small{color:var(--muted);font-size:var(--fs-xs)}.timeline-strip strong{margin-top:4px;font-size:var(--fs-sm);line-height:1.4}.life-core.offline{filter:saturate(.45)}.life-core.attention .core-face{background:linear-gradient(145deg,var(--amber),#d89d3e)}
.quality-panel{min-height:104px;padding:16px;display:grid;grid-template-columns:39px 1fr;gap:10px;align-items:center;border-left:2px solid var(--lime);background:linear-gradient(110deg,rgba(196,246,110,.06),rgba(30,24,38,.75))}.quality-panel.watch{border-color:var(--amber);background:linear-gradient(110deg,rgba(255,201,107,.06),rgba(30,24,38,.75))}.quality-panel.attention{border-color:var(--red);background:linear-gradient(110deg,rgba(255,123,133,.07),rgba(30,24,38,.75))}.quality-icon{width:38px;height:38px;display:grid;place-items:center;background:rgba(196,246,110,.1);color:var(--lime)}.quality-panel.watch .quality-icon{background:rgba(255,201,107,.1);color:var(--amber)}.quality-panel.attention .quality-icon{background:rgba(255,123,133,.1);color:var(--red)}.quality-panel small,.quality-panel strong,.quality-panel>div:nth-child(2)>span{display:block}.quality-panel small{color:var(--muted);font:700 var(--fs-xs) var(--font-mono);letter-spacing:.8px}.quality-panel strong{margin:4px 0;font-size:var(--fs-sm)}.quality-panel>div:nth-child(2)>span{color:var(--muted);font-size:var(--fs-xs)}.quality-signals,.quality-clean{grid-column:1/-1;display:flex;gap:5px;flex-wrap:wrap;font-size:var(--fs-xs)}.quality-signals span{padding:4px 6px;border:1px solid rgba(255,201,107,.25);color:var(--amber)}.quality-clean{color:var(--muted)}
@media(max-width:1150px){.life-grid,.overview-columns{grid-template-columns:1fr}.attention-panel{min-height:auto}.health-rail{grid-template-columns:repeat(3,1fr)}.health-node:nth-child(3){border-right:0}.timeline-strip{grid-template-columns:repeat(2,1fr)}.timeline-strip article:nth-child(2n){border:0}}
@media(max-width:700px){.life-core{display:block;min-height:auto;padding:28px 22px}.life-orbit{width:135px;height:135px;margin:0 0 25px;flex-basis:135px}.core-face{width:84px;height:84px;font-size:34px}.health-rail{grid-template-columns:1fr 1fr}.health-node:nth-child(3){border-right:1px solid var(--line)}.health-node:nth-child(2n){border-right:0}.health-node:last-child{grid-column:1/-1}.timeline-strip{display:block}.timeline-strip article,.timeline-strip article:nth-child(4n){border-right:0;border-bottom:1px solid var(--line)}.overview-columns{display:block}.right-stack{margin-top:14px}}
.trend-panel{margin-top:14px}.trend-chart{height:112px;display:flex;align-items:flex-end;gap:4px;padding:18px 18px 0;border-top:1px solid var(--line);overflow:hidden}.trend-column{position:relative;flex:1;min-width:5px;height:100%;display:flex;align-items:flex-end;justify-content:center}.trend-column i{display:block;width:100%;min-height:4px;border-radius:3px 3px 0 0;background:linear-gradient(180deg,var(--cyan),var(--purple));opacity:.78;transition:height 500ms var(--ease-out)}.trend-column i.bad{background:linear-gradient(180deg,var(--amber),var(--pink))}.trend-column b{position:absolute;bottom:-3px;width:5px;height:5px;border-radius:50%;background:var(--lime);box-shadow:0 0 7px var(--lime)}.trend-column b.bad{background:var(--pink);box-shadow:0 0 7px var(--pink)}.trend-column small{position:absolute;top:calc(100% + 7px);color:var(--muted);font-size:var(--fs-xs);white-space:nowrap}.trend-foot{display:flex;justify-content:space-between;gap:12px;padding:13px 18px;color:var(--muted);font-size:var(--fs-xs)}.trend-foot span:first-child{color:var(--ink-soft)}.trend-signals{display:flex;gap:8px;flex-wrap:wrap;padding:0 18px 15px}.trend-signals span{display:inline-flex;align-items:center;gap:6px;padding:6px 8px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:var(--fs-xs);background:rgba(255,255,255,.018)}.signal-dot{width:5px;height:5px;border-radius:50%;background:var(--cyan);box-shadow:0 0 7px currentColor}.signal-dot.purple{background:var(--purple)}.signal-dot.lime{background:var(--lime)}</style>
