<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Activity, Archive, Check, ChevronRight, CircleAlert, Code2, FileCheck2, GitCompareArrows, Play, RotateCcw, ShieldCheck, TestTube2, X, Sparkles } from "@lucide/vue";
import { api, bestEffort, json, timeAgo, getEvolutionProposals, proposeEvolution, approveEvolution, rejectEvolution } from "../api";
import type { EvolutionProposal } from "../types";
import EmptyState from "../components/EmptyState.vue";
import StatusDot from "../components/StatusDot.vue";

const emit = defineEmits<{ notify: [tone: "success"|"error"|"info", title: string, detail?: string] }>();
const tasks = ref<any[]>([]); const approvals = ref<any[]>([]); const selected = ref<any>(null); const loading = ref(false); const reason = ref(""); const processing = ref(false); const evaluation = ref<any>(null); const backups = ref<any[]>([]); const backupMeta = ref<any>({}); const replays = ref<any[]>([]); const snapshotting = ref(false); const replaying = ref(false); const showHistory = ref(false);
const selfcodeTools = ref<any[]>([]); const selfcodeBackups = ref<any[]>([]); const selfcodeEnabled = ref(false); const selfcodeBusy = ref(""); const selfcodeSuggest = ref<any[]>([]);
const evolutionProposals = ref<EvolutionProposal[]>([]); const evolutionBusy = ref(false); const evolutionGoal = ref(""); const evolutionFiles = ref(""); const evolutionError = ref("");
const pendingTasks = computed(() => tasks.value.filter((item) => item.status === "awaiting_approval"));
const pendingApprovals = computed(() => approvals.value.filter((item) => item.status === "pending"));
const actionableTasks = computed(() => tasks.value.filter((item) => ["awaiting_approval", "planned", "running", "approved_pending_apply"].includes(item.status)));
const visibleTasks = computed(() => showHistory.value ? tasks.value : actionableTasks.value);
const pendingGroups = computed(() => new Set(pendingTasks.value.map(reviewGroup)).size);
const SIGNAL_COPY: Record<string, { title: string; purpose: string }> = {
  empty_should_reply: { title: "有一条该回复的消息没有发出", purpose: "检查为什么消息进入回复流程后没有形成可发送的内容，避免用户被无声忽略。" },
  tool_spins: { title: "工具在重复尝试，可能卡住了", purpose: "限制同一工具和同一参数的无效重复调用，避免机器人在原地兜圈。" },
  react_max_rounds: { title: "回答推理次数过多，可能在兜圈", purpose: "让 ReAct 在拿不到新信息时及时结束并给出正常回复。" },
  space_link_fail: { title: "B 站主页链接处理失败", purpose: "修复 B 站主页链接的解析或兼容性，避免链接请求失败。" },
  outbound_failures: { title: "QQ 消息发送曾失败", purpose: "检查出站消息的格式和失败处理，降低消息发不出去的概率。" },
  identity_leaks: { title: "人格/内部身份泄漏被拦截", purpose: "加固回复护栏，避免机器人暴露内部工具、模型或权限信息。" },
};
function taskSignals(item:any): string[]{ const p=item?.proposal||{}; return Array.isArray(p.signals)&&p.signals.length?p.signals:(p.signal?[p.signal]:[]); }
function reviewGroup(item:any){ const p=item?.proposal||{}; if(p.ticket_group)return p.ticket_group; const s=taskSignals(item); return s.some((key)=>key==="tool_spins"||key==="react_max_rounds")?"react_stability":item.id; }
function taskTitle(item:any){ const signals=taskSignals(item); if(reviewGroup(item)==="react_stability")return "ReAct 稳定性：避免工具空转和无效兜圈"; return SIGNAL_COPY[signals[0]]?.title || (item.kind==="selfcode"?"新增一个独立工具候选":String(item.request||"工程任务").split("\n")[0]); }
function taskPurpose(item:any){ const signals=taskSignals(item); if(reviewGroup(item)==="react_stability")return "把“工具重复尝试”和“推理轮次触顶”合并治理；这是同一类稳定性问题，不会直接改生产。"; return SIGNAL_COPY[signals[0]]?.purpose || "等待工程代理补充清晰的用途说明。"; }
function statusLabel(status:string){ return ({awaiting_approval:"等待你决定",planned:"准备中",running:"处理中",approved_pending_apply:"已通过，等待受控应用",completed:"已完成",failed:"生成失败",cancelled:"已取消",rejected:"已驳回"} as Record<string,string>)[status] || status; }
function hasPatch(item:any){ const p=item?.proposal||{}; return !!((Array.isArray(p.unified_diffs)&&p.unified_diffs.length)||(Array.isArray(p.file_writes)&&p.file_writes.length)); }
function needsPatchPreparation(item:any){ return item?.kind==="fault_ticket" || (item?.kind==="core_patch"&&!hasPatch(item)); }
async function load(){loading.value=true;try{const [t,a,e,b,r,sc,ev]=await Promise.all([bestEffort(api<any>("/api/tasks?page_size=100"),{items:[]}),bestEffort(api<any>("/api/approvals?page_size=100"),{items:[]}),bestEffort(api<any>("/api/evaluation"),null),bestEffort(api<any>("/api/backups"),{items:[],meta:{}}),bestEffort(api<any>("/api/replays"),{items:[]}),bestEffort(api<any>("/api/selfcode/tools"),{items:[],backups:[],enabled:false,suggest_uninstall:[]}),bestEffort(api<any>("/api/evolution/proposals"),{items:[]})]);tasks.value=t.items||[];approvals.value=a.items||[];evaluation.value=e;backups.value=b.items||[];backupMeta.value=b.meta||{};replays.value=r.items||[];selfcodeTools.value=sc.items||[];selfcodeBackups.value=sc.backups||[];selfcodeEnabled.value=!!sc.enabled;selfcodeSuggest.value=sc.suggest_uninstall||[];evolutionProposals.value=ev.items||[];if(selected.value)selected.value=tasks.value.find(i=>i.id===selected.value.id)||null;}finally{loading.value=false}}
async function taskAction(action:"prepare"|"approve"|"apply"|"reject"|"redo"){
  if(!selected.value)return;
  if(action==="apply"&&!window.confirm("确认把已审核补丁写入运行目录并执行受控测试吗？失败会自动回滚。"))return;
  processing.value=true;
  try{
    const id=selected.value.id;
    const iterationAction=action==="prepare"||action==="apply"||(action==="approve"&&selected.value.kind==="core_patch");
    const url=iterationAction?`/api/iterate/${id}/${action}`:`/api/tasks/${id}/${action}`;
    await api(url,json("POST",reason.value?{reason:reason.value}:{}));
    const title=({prepare:"修复方案已生成，等待你审核",approve:"工程候选已通过",apply:"补丁已受控应用",reject:"候选已驳回",redo:"已要求工程代理重做"} as Record<string,string>)[action];
    emit("notify","success",title);
    reason.value="";
    await load();
    if(action==="reject"||action==="redo"||action==="apply")selected.value=null;
  }catch(e:any){emit("notify","error","操作失败",e.message)}finally{processing.value=false}
}
async function approvalAction(item:any, action:"approve"|"reject"){processing.value=true;try{await api(`/api/approvals/${item.id}/${action}`,json("POST",{}));emit("notify","success",action==="approve"?"能力调用已批准":"能力调用已驳回");await load();}catch(e:any){emit("notify","error","审批失败",e.message)}finally{processing.value=false}}
async function createSnapshot(){snapshotting.value=true;try{await api("/api/backups",json("POST",{reason:"owner_console"}));emit("notify","success","状态快照已创建","未包含聊天正文、图片、配置或密钥");await load()}catch(e:any){emit("notify","error","创建快照失败",e.message)}finally{snapshotting.value=false}}
async function runReplay(){replaying.value=true;try{const result=await api<any>("/api/replays/run",json("POST",{}));emit("notify",result.passed===result.total?"success":"error","行为回放完成",`${result.passed}/${result.total} 项通过；全程隔离，不会调用真实 QQ 或 LLM`);await load()}catch(e:any){emit("notify","error","行为回放失败",e.message)}finally{replaying.value=false}}
async function selfcodeAction(name:string, action:"enable"|"disable"|"delete"){const key=`${action}:${name}`;selfcodeBusy.value=key;try{if(action==="delete"){if(!window.confirm(`删除已装工具 ${name}？删除前会自动备份。`))return;await api(`/api/selfcode/tools/${encodeURIComponent(name)}`,{method:"DELETE"});emit("notify","success",`已删除 ${name}`);}else{await api(`/api/selfcode/tools/${encodeURIComponent(name)}/${action}`,json("POST",{}));emit("notify","success",action==="enable"?`已启用 ${name}`:`已禁用 ${name}`);}await load();}catch(e:any){emit("notify","error","SelfCode 运维失败",e.message)}finally{selfcodeBusy.value=""}}
async function evaluateSelfcode(){selfcodeBusy.value="eval";try{const r=await api<any>("/api/selfcode/evaluate",json("POST",{}));const n=(r.suggestions||[]).length;emit("notify",n?"info":"success",n?`评估完成：${n} 个建议卸载`:"评估完成：暂无建议卸载");await load()}catch(e:any){emit("notify","error","评估失败",e.message)}finally{selfcodeBusy.value=""}}
async function dismissSuggest(name:string){selfcodeBusy.value=`keep:${name}`;try{await api(`/api/selfcode/tools/${encodeURIComponent(name)}/dismiss-suggest`,json("POST",{}));emit("notify","success",`已驳回对 ${name} 的卸载建议`);await load()}catch(e:any){emit("notify","error","驳回失败",e.message)}finally{selfcodeBusy.value=""}}
function statsLine(item:any){const s=item?.stats||{};const fr=s.fail_rate!=null?Math.round(Number(s.fail_rate)*100):0;const flag=s.suggest_uninstall?` · 建议卸：${s.suggest_reason||""}`:"";return `调 ${s.calls||0} · 成 ${s.ok||0} · 败 ${s.fail||0}（${fr}%）${flag}`}
async function restoreSelfcodeBackup(backupId:string){if(!window.confirm(`从备份 ${backupId} 恢复全部 SelfCode 工具？当前目录会先再备份一次。`))return;selfcodeBusy.value=`restore:${backupId}`;try{const result=await api<any>(`/api/selfcode/backups/${encodeURIComponent(backupId)}/restore`,json("POST",{}));emit("notify","success","备份已恢复",`重新加载 ${result.loaded||0} 个工具`);await load()}catch(e:any){emit("notify","error","恢复失败",e.message)}finally{selfcodeBusy.value=""}}
function proposalValue(key:string, fallback="暂无"){const value=selected.value?.proposal?.[key];if(Array.isArray(value))return value.join("、")||fallback;return value?String(value):fallback}
function evolutionStatusLabel(p:EvolutionProposal){ if(p.decision==="rejected"||p.status==="rejected")return "已被安全围栏/你驳回"; if(p.decision==="approved"||p.status==="approved")return "已批准（仅提案）"; if(p.touches_core)return "触核心·需人工 PR"; return "等待你审批"; }
async function proposeEvolutionAction(){ evolutionError.value=""; const goal=evolutionGoal.value.trim(); if(!goal){evolutionError.value="请填写优化目标";return;} const files=evolutionFiles.value.split(",").map(s=>s.trim()).filter(Boolean); evolutionBusy.value=true; try{ const r=await proposeEvolution(goal, files); if(r.decision.decision==="rejected"){emit("notify","error","提案被安全围栏拒绝",(r.decision.issues||[]).join("；")||"触及受保护核心文件，需人工 PR");} else {emit("notify","success","已生成自我进化提案","等待你在下方审批（仅批准提案，不自动部署）");} evolutionGoal.value=""; evolutionFiles.value=""; await load(); }catch(e:any){evolutionError.value=e.message;emit("notify","error","发起提案失败",e.message)}finally{evolutionBusy.value=false} }
async function evolutionApprove(p:EvolutionProposal){ evolutionBusy.value=true; try{ await approveEvolution(p.id); emit("notify","success","已批准提案","仅放行提案；生产更新需另走受控部署"); await load(); }catch(e:any){emit("notify","error","批准失败",e.message)}finally{evolutionBusy.value=false} }
async function evolutionReject(p:EvolutionProposal){ evolutionBusy.value=true; try{ await rejectEvolution(p.id); emit("notify","success","已驳回提案"); await load(); }catch(e:any){emit("notify","error","驳回失败",e.message)}finally{evolutionBusy.value=false} }
onMounted(load);
</script>

<template><section class="page engineering-page">
  <div class="page-heading"><div><span class="eyebrow">human in the loop</span><h1>先看明白，再让它发生。</h1><p>工程作用、代码证据、测试和回滚在同一条审批链里；未通过的候选不会部署。</p></div></div>
  <div class="approval-summary"><article><span class="summary-icon pink"><Code2 :size="20"/></span><div><small>工程问题组</small><strong>{{ pendingGroups }}</strong></div><p>{{ pendingTasks.length }} 条信号已按关联问题合并展示</p></article><article><span class="summary-icon amber"><ShieldCheck :size="20"/></span><div><small>高危能力</small><strong>{{ pendingApprovals.length }}</strong></div><p>工具参数已脱敏，仅展示摘要</p></article><article><span class="summary-icon lime"><FileCheck2 :size="20"/></span><div><small>流程边界</small><strong>3 层</strong></div><p>生成、测试部署、生产部署分离</p></article></div>
  <section class="panel acceptance-panel"><div class="panel-head"><div><strong>上线验收快照</strong><span>只读检查，不触发 QQ、LLM 或自动化动作</span></div><button class="action-button subtle" :disabled="snapshotting" @click="createSnapshot"><Archive :size="15"/>{{ snapshotting?'创建中…':'创建状态备份' }}</button></div><div class="acceptance-grid"><article v-for="item in evaluation?.checks||[]" :key="item.id" :class="item.status"><span><Activity :size="15"/></span><div><small>{{ item.title }}</small><strong>{{ item.detail }}</strong></div></article></div><p class="snapshot-note">最近快照：{{ backups.length ? `${timeAgo(backups[0].created_at)} · ${backups[0].size_bytes} bytes` : '尚未创建' }} · 自动每日维护，保留最近 {{ backupMeta.retention||30 }} 份。快照只含意图、工作流、日历、表情索引、订阅和健康趋势。</p></section>
  <section class="panel replay-panel"><div class="panel-head"><div><strong>真实行为回放</strong><span>用隔离假模型重跑生产 Pipeline，验证不会误回群、不会泄漏上游身份。</span></div><button class="action-button" :disabled="replaying" @click="runReplay"><Play :size="15"/>{{ replaying?'回放中…':'运行验收' }}</button></div><div v-if="replays.length" class="replay-result"><div><small>最近一次</small><strong :class="replays[0].passed===replays[0].total?'pass':'fail'">{{ replays[0].passed }}/{{ replays[0].total }} 通过</strong><span>{{ timeAgo(replays[0].created_at) }}</span></div><article v-for="item in replays[0].cases" :key="item.id" :class="{fail:!item.passed}"><Check v-if="item.passed" :size="14"/><X v-else :size="14"/><span><strong>{{ item.title }}</strong><small>{{ item.error||item.detail }} · {{ item.duration_ms }} ms</small></span></article></div><EmptyState v-else title="还没有行为验收记录" detail="运行一次后，会留下私聊、群聊克制和人格护栏的可追踪结果。"/></section>
  <div class="engineering-layout">
    <section class="panel task-queue"><div class="panel-head"><div><strong>工程任务</strong><span>默认只显示仍需处理的候选；历史记录不会干扰审核。</span></div><StatusDot :status="pendingTasks.length?'pending':'ready'" :label="pendingGroups?`${pendingGroups} 组待审`:'已清空'"/></div>
      <div v-if="visibleTasks.length" class="task-list"><button v-for="item in visibleTasks" :key="item.id" :class="{active:selected?.id===item.id}" @click="selected=item"><span class="task-state" :class="item.status"><CircleAlert v-if="item.status==='awaiting_approval'" :size="16"/><Check v-else-if="item.status==='completed'" :size="16"/><Code2 v-else :size="16"/></span><span><small>{{ item.kind==='selfcode'?'SELF-CODE':'工程诊断' }} · {{ timeAgo(item.updated_at) }}</small><strong>{{ taskTitle(item) }}</strong><em>{{ statusLabel(item.status) }}</em></span><ChevronRight :size="16"/></button></div><EmptyState v-else title="没有需要处理的工程任务" detail="已完成、失败或取消的历史默认隐藏，不影响你审核新候选。"/>
      <button v-if="tasks.length>actionableTasks.length" class="history-toggle" @click="showHistory=!showHistory">{{ showHistory ? '隐藏历史记录' : `查看 ${tasks.length-actionableTasks.length} 条历史记录` }}</button>
    </section>
    <section class="panel evidence-panel" :class="{empty:!selected}">
      <template v-if="selected"><div class="evidence-head"><div><span>{{ selected.id }}</span><h2>{{ taskTitle(selected) }}</h2></div><StatusDot :status="selected.status" :label="statusLabel(selected.status)"/></div>
        <div class="evidence-sections">
          <article><span class="evidence-icon"><FileCheck2 :size="18"/></span><div><small>这项工程解决什么</small><p>{{ proposalValue('purpose', taskPurpose(selected)) }}</p></div></article>
          <article><span class="evidence-icon"><GitCompareArrows :size="18"/></span><div><small>影响范围</small><p>{{ proposalValue('affected_modules', proposalValue('suggested_files', proposalValue('files'))) }}</p></div></article>
          <article><span class="evidence-icon"><TestTube2 :size="18"/></span><div><small>测试证据</small><p>{{ proposalValue('tests', selected.evidence?.slice(-2).join(' · ') || '尚未提交测试结果') }}</p></div></article>
          <article><span class="evidence-icon"><RotateCcw :size="18"/></span><div><small>回滚边界</small><p>{{ proposalValue('rollback', proposalValue('rollback_point', '批准前不会触碰生产环境')) }}</p></div></article>
        </div>
        <div v-if="selected.status==='awaiting_approval'" class="review-box">
          <label for="reviewReason">给工程代理的反馈（驳回或重做时使用）</label>
          <textarea id="reviewReason" v-model="reason" placeholder="例如：补充移动端测试，并说明失败回滚路径"/>
          <p v-if="needsPatchPreparation(selected)" class="review-hint">当前只有问题诊断，还没有代码变更。生成方案只会写入待审补丁，不会修改运行文件。</p>
          <p v-else-if="selected.kind==='core_patch'" class="review-hint">补丁已经生成。批准后仍不会改文件，还需要下一步明确点击“受控应用”。</p>
          <div><button class="action-button danger" :disabled="processing" @click="taskAction('reject')"><X :size="16"/>驳回</button><button v-if="selected.kind==='selfcode'" class="action-button subtle" :disabled="processing" @click="taskAction('redo')"><RotateCcw :size="16"/>要求重做</button><button v-if="needsPatchPreparation(selected)" class="action-button" :disabled="processing" @click="taskAction('prepare')"><Code2 :size="16"/>生成修复方案</button><button v-else class="action-button" :disabled="processing" @click="taskAction('approve')"><Check :size="16"/>{{ selected.kind==='core_patch'?'批准补丁候选':'批准并安装工具' }}</button></div>
        </div>
        <div v-else-if="selected.status==='approved_pending_apply'" class="review-box apply-ready"><p class="review-hint">你已经批准这份补丁，但它还没有写入磁盘。点击后会先备份、写入、运行定向测试；失败自动回滚。</p><div><button class="action-button danger" :disabled="processing" @click="taskAction('apply')"><Play :size="16"/>确认受控应用</button></div></div>
      </template><EmptyState v-else title="选择一项工程任务" detail="右侧会用人能读懂的方式展示作用、影响、测试和回滚证据。"/>
    </section>
  </div>
  <div class="section-title"><div><h2>已装 SelfCode 工具</h2><p>装得上也能卸得下；效果评估只建议卸载，删除须你确认。</p></div><div class="section-actions"><button class="action-button subtle" :disabled="!!selfcodeBusy" @click="evaluateSelfcode">重算建议</button><StatusDot :status="selfcodeEnabled?'running':'stopped'" :label="selfcodeEnabled?'SelfCode 已启用':'SelfCode 未启用'"/></div></div>
  <section class="panel selfcode-ops">
    <div v-if="selfcodeSuggest.length" class="selfcode-suggest">
      <small>建议卸载（不会自动删）</small>
      <article v-for="s in selfcodeSuggest" :key="'sug-'+s.name">
        <strong>{{ s.name }}</strong>
        <span>{{ s.reason || (s.stats&&s.stats.suggest_reason) || '效果不佳' }}</span>
        <div class="selfcode-actions">
          <button class="action-button subtle" :disabled="!!selfcodeBusy" @click="dismissSuggest(s.name)">保留</button>
          <button class="action-button danger" :disabled="!!selfcodeBusy" @click="selfcodeAction(s.name,'delete')">确认删除</button>
        </div>
      </article>
    </div>
    <div v-if="selfcodeTools.length" class="selfcode-list">
      <article v-for="item in selfcodeTools" :key="item.name" :class="{warn: item.stats?.suggest_uninstall}">
        <span class="cap-icon"><Code2 :size="18"/></span>
        <div>
          <small>{{ item.file }} · {{ item.enabled ? (item.registered ? '已热注册' : '已启用未注册') : '已禁用' }} · {{ item.bytes }} bytes</small>
          <strong>{{ item.name }}</strong>
          <p>{{ item.description || '无描述' }}</p>
          <p class="stats">{{ statsLine(item) }}</p>
        </div>
        <div class="selfcode-actions">
          <button v-if="item.stats?.suggest_uninstall" class="action-button subtle" :disabled="!!selfcodeBusy" @click="dismissSuggest(item.name)">保留</button>
          <button v-if="!item.enabled" class="action-button subtle" :disabled="!!selfcodeBusy" @click="selfcodeAction(item.name,'enable')">启用</button>
          <button v-else class="action-button subtle" :disabled="!!selfcodeBusy" @click="selfcodeAction(item.name,'disable')">禁用</button>
          <button class="action-button danger" :disabled="!!selfcodeBusy" @click="selfcodeAction(item.name,'delete')">删除</button>
        </div>
      </article>
    </div>
    <EmptyState v-else title="还没有已装 SelfCode 工具" detail="Owner 批准工程候选后，会出现在这里，并可随时禁用或删除。" />
    <div v-if="selfcodeBackups.length" class="selfcode-backups">
      <small>最近备份</small>
      <button v-for="item in selfcodeBackups.slice(0,6)" :key="item.id" class="action-button subtle" :disabled="!!selfcodeBusy" @click="restoreSelfcodeBackup(item.id)">
        <RotateCcw :size="14"/> {{ item.id }} · {{ item.files }} 文件
      </button>
    </div>
  </section>
  <div class="section-title"><div><h2>受控自我进化</h2><p>R9：ANIMA 可以提出自身优化，但绝不自我授权生产更新；批准只放行提案，落地需另走受控部署。</p></div><StatusDot :status="evolutionProposals.some(p=>p.decision==='pending_owner')?'pending':'ready'" :label="evolutionProposals.some(p=>p.decision==='pending_owner')?'有待审提案':'已清空'"/></div>
  <section class="panel evolution-ops">
    <div class="evolution-compose">
      <label for="evoGoal">让 ANIMA 优化什么？</label>
      <textarea id="evoGoal" v-model="evolutionGoal" placeholder="例如：降低长对话的回复延迟，或新增一个观测类工具"/>
      <label for="evoFiles">拟改动文件（可选，逗号分隔；触及核心文件会被围栏拦截）</label>
      <input id="evoFiles" v-model="evolutionFiles" placeholder="anima/tools/foo.py"/>
      <p v-if="evolutionError" class="review-hint">{{ evolutionError }}</p>
      <div><button class="action-button" :disabled="evolutionBusy" @click="proposeEvolutionAction"><Sparkles :size="16"/>{{ evolutionBusy?'生成中…':'发起自我进化提案' }}</button></div>
    </div>
    <div v-if="evolutionProposals.length" class="evolution-list">
      <article v-for="p in evolutionProposals" :key="p.id" :class="{core:p.touches_core, rejected:(p.decision==='rejected'||p.status==='rejected')}">
        <span class="cap-icon"><Sparkles :size="18"/></span>
        <div>
          <small>{{ p.id }} · {{ timeAgo(p.created_at) }} · {{ evolutionStatusLabel(p) }}</small>
          <strong>{{ p.title }}</strong>
          <p v-if="p.target_files.length">目标文件：{{ p.target_files.join('、') }}<span v-if="p.touches_core" class="core-tag"> · 触核心·需人工 PR</span></p>
          <p>{{ p.diff_summary || p.rationale || '暂无摘要' }}</p>
        </div>
        <div v-if="p.decision==='pending_owner' && !p.touches_core" class="evolution-actions">
          <button class="action-button danger" :disabled="evolutionBusy" @click="evolutionReject(p)"><X :size="16"/>驳回</button>
          <button class="action-button" :disabled="evolutionBusy" @click="evolutionApprove(p)"><Check :size="16"/>批准提案</button>
        </div>
        <div v-else-if="p.touches_core && p.decision!=='rejected'" class="evolution-actions"><span class="core-note">需 Owner 走人工 PR</span></div>
      </article>
    </div>
    <EmptyState v-else title="还没有自我进化提案" detail="发起一次后，ANIMA 会生成受安全围栏约束的提案，等你审批。"/>
  </section>
  <div class="section-title"><div><h2>能力调用审批</h2><p>高风险工具必须由 Owner 明确批准。</p></div></div>
  <div v-if="pendingApprovals.length" class="capability-approvals"><article v-for="item in pendingApprovals" :key="item.id" class="panel"><span class="cap-icon"><ShieldCheck :size="18"/></span><div><small>{{ item.id }} · {{ timeAgo(item.created_at) }}</small><strong>{{ item.tool }}</strong><p>参数摘要 {{ item.args_digest }} · 到期 {{ timeAgo(item.expires_at) }}</p></div><div><button class="icon-button reject" aria-label="驳回" @click="approvalAction(item,'reject')"><X :size="17"/></button><button class="icon-button approve" aria-label="批准" @click="approvalAction(item,'approve')"><Check :size="17"/></button></div></article></div><EmptyState v-else title="没有待批能力" detail="高风险工具调用出现时，会在这里显示脱敏摘要和有效期限。"/>
</section></template>

<style scoped>
.approval-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.approval-summary article{min-height:110px;padding:17px;border-top:1px solid var(--line-strong);border-bottom:1px solid var(--line);display:grid;grid-template-columns:39px 1fr;gap:11px;align-items:center;background:rgba(255,255,255,.025)}.approval-summary p{grid-column:1/-1;margin:0;color:var(--muted);font-size:9px}.summary-icon{width:38px;height:38px;display:grid;place-items:center}.summary-icon.pink{background:rgba(255,143,184,.12);color:var(--pink)}.summary-icon.amber{background:rgba(255,201,107,.12);color:var(--amber)}.summary-icon.lime{background:rgba(196,246,110,.12);color:var(--lime)}.approval-summary small,.approval-summary strong{display:block}.approval-summary small{color:var(--muted);font-size:8px}.approval-summary strong{margin-top:2px;font:750 22px Georgia,serif}.engineering-layout{display:grid;grid-template-columns:minmax(320px,.75fr) minmax(500px,1.25fr);gap:14px;margin-top:14px}.task-list{padding:6px 10px}.task-list button{width:100%;min-height:74px;padding:10px;border:0;border-bottom:1px solid var(--line);background:transparent;display:grid;grid-template-columns:33px 1fr 18px;gap:9px;align-items:center;text-align:left;cursor:pointer}.task-list button:hover,.task-list button.active{background:rgba(196,246,110,.045)}.task-state{width:30px;height:30px;display:grid;place-items:center;background:rgba(255,255,255,.05);color:var(--muted)}.task-state.awaiting_approval{background:rgba(255,201,107,.12);color:var(--amber)}.task-state.completed{background:rgba(196,246,110,.1);color:var(--lime)}.task-list small,.task-list strong,.task-list em{display:block}.task-list small{color:var(--muted);font-size:7px}.task-list strong{margin-top:5px;font-size:10px;line-height:1.35}.task-list em{margin-top:4px;color:var(--purple);font:normal 8px ui-monospace,monospace}.task-list>button>svg{color:var(--muted)}.evidence-panel{min-height:480px}.evidence-head{display:flex;justify-content:space-between;gap:15px;padding:24px;border-bottom:1px solid var(--line)}.evidence-head>div{min-width:0}.evidence-head span{color:var(--pink);font:740 8px ui-monospace,monospace;letter-spacing:1px}.evidence-head h2{margin:6px 0 0;font:720 17px/1.4 Georgia,"Songti SC",serif}.evidence-sections{padding:7px 22px}.evidence-sections article{min-height:78px;padding:15px 0;display:grid;grid-template-columns:35px 1fr;gap:11px;border-bottom:1px solid var(--line)}.evidence-icon{width:31px;height:31px;display:grid;place-items:center;background:rgba(123,219,223,.09);color:var(--cyan)}.evidence-sections small{color:var(--muted);font-size:8px}.evidence-sections p{margin:6px 0 0;color:var(--ink-soft);font-size:10px;line-height:1.55}.review-box{padding:19px 22px;background:rgba(255,201,107,.035);border-top:1px solid rgba(255,201,107,.18)}.review-box label{display:block;color:var(--ink-soft);font-size:9px}.review-box textarea{width:100%;min-height:78px;margin-top:8px;padding:10px;border:1px solid var(--line-strong);background:#100d16;color:var(--ink);resize:vertical}.review-box>div{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}.capability-approvals{display:grid;grid-template-columns:1fr 1fr;gap:10px}.capability-approvals article{min-height:90px;padding:15px;display:grid;grid-template-columns:36px 1fr auto;gap:11px;align-items:center}.cap-icon{width:34px;height:34px;display:grid;place-items:center;background:rgba(255,201,107,.1);color:var(--amber)}.capability-approvals small,.capability-approvals strong{display:block}.capability-approvals small{color:var(--muted);font-size:7px}.capability-approvals strong{margin-top:4px;font-size:11px}.capability-approvals p{margin:5px 0 0;color:var(--muted);font-size:8px}.capability-approvals article>div:last-child{display:flex}.icon-button.approve{color:var(--lime)}.icon-button.reject{color:var(--red)}@media(max-width:920px){.engineering-layout{grid-template-columns:1fr}.evidence-panel.empty{display:none}}@media(max-width:680px){.approval-summary{grid-template-columns:1fr}.capability-approvals{grid-template-columns:1fr}.review-box>div{display:grid;grid-template-columns:1fr}.review-box .action-button{width:100%}}
.review-hint{margin:10px 0 0;padding:9px 10px;border-left:2px solid var(--amber);background:rgba(255,201,107,.05);color:var(--ink-soft);font-size:8px;line-height:1.5}.review-box.apply-ready{background:rgba(255,112,127,.045);border-top-color:rgba(255,112,127,.24)}.review-box.apply-ready .review-hint{border-left-color:var(--red)}
.acceptance-panel{margin-top:14px}.acceptance-panel .panel-head{padding:16px 18px;border-bottom:1px solid var(--line)}.acceptance-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line)}.acceptance-grid article{min-height:74px;padding:13px;background:rgba(16,13,22,.92);display:grid;grid-template-columns:27px 1fr;gap:8px;align-items:start}.acceptance-grid article>span{width:25px;height:25px;display:grid;place-items:center;background:rgba(123,219,223,.1);color:var(--cyan)}.acceptance-grid article.attention>span{background:rgba(255,201,107,.12);color:var(--amber)}.acceptance-grid article.unknown>span{background:rgba(255,255,255,.06);color:var(--muted)}.acceptance-grid small,.acceptance-grid strong{display:block}.acceptance-grid small{color:var(--muted);font-size:7px}.acceptance-grid strong{margin-top:4px;color:var(--ink-soft);font-size:9px;line-height:1.35}.snapshot-note{margin:0;padding:10px 18px;color:var(--muted);font-size:8px;background:rgba(255,255,255,.018)}.section-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.selfcode-ops{margin:0 0 14px;padding:8px 12px 14px}.selfcode-suggest{display:grid;gap:8px;margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid var(--line)}.selfcode-suggest>small{color:var(--amber);font-size:8px}.selfcode-suggest article{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;padding:10px;border:1px solid rgba(255,201,107,.28);background:rgba(255,201,107,.06)}.selfcode-suggest strong{font-size:11px}.selfcode-suggest span{display:block;color:var(--muted);font-size:8px;margin-top:3px}.selfcode-list{display:grid;gap:8px}.selfcode-list article{min-height:78px;padding:12px;border:1px solid var(--line);display:grid;grid-template-columns:36px 1fr auto;gap:11px;align-items:center;background:rgba(255,255,255,.02)}.selfcode-list article.warn{border-color:rgba(255,201,107,.35)}.selfcode-list small,.selfcode-list strong{display:block}.selfcode-list small{color:var(--muted);font-size:7px}.selfcode-list strong{margin-top:4px;font-size:11px}.selfcode-list p{margin:5px 0 0;color:var(--muted);font-size:8px;line-height:1.4}.selfcode-list p.stats{color:var(--cyan)}.selfcode-actions{display:flex;gap:6px;flex-wrap:wrap}.selfcode-backups{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}.selfcode-backups>small{color:var(--muted);font-size:8px;margin-right:4px}@media(max-width:680px){.acceptance-grid{grid-template-columns:1fr}.acceptance-panel .panel-head{align-items:flex-start;gap:10px;flex-direction:column}.selfcode-list article{grid-template-columns:1fr}.selfcode-actions{justify-content:flex-start}}
.replay-panel{margin-top:14px}.replay-panel .panel-head{padding:16px 18px;border-bottom:1px solid var(--line)}.replay-result{display:grid;grid-template-columns:170px repeat(4,1fr);gap:1px;background:var(--line)}.replay-result>div,.replay-result article{min-height:82px;padding:13px;background:rgba(16,13,22,.92)}.replay-result>div small,.replay-result>div strong,.replay-result>div span{display:block}.replay-result>div small,.replay-result article small{color:var(--muted);font-size:7px}.replay-result>div strong{margin:5px 0;font:700 17px Georgia,serif}.replay-result>div strong.pass{color:var(--lime)}.replay-result>div strong.fail{color:var(--red)}.replay-result>div span{font-size:8px;color:var(--muted)}.replay-result article{display:grid;grid-template-columns:20px 1fr;gap:7px}.replay-result article>svg{color:var(--lime)}.replay-result article.fail>svg{color:var(--red)}.replay-result article strong,.replay-result article small{display:block}.replay-result article strong{color:var(--ink-soft);font-size:9px;line-height:1.35}.replay-result article small{margin-top:4px;line-height:1.3}@media(max-width:900px){.replay-result{grid-template-columns:1fr 1fr}.replay-result>div{grid-column:1/-1}}@media(max-width:680px){.acceptance-grid{grid-template-columns:1fr}.acceptance-panel .panel-head,.replay-panel .panel-head{align-items:flex-start;gap:10px;flex-direction:column}.replay-result{grid-template-columns:1fr}.replay-result>div{grid-column:auto}}
.evolution-ops{margin:0 0 14px;padding:8px 12px 14px}.evolution-compose{display:grid;gap:8px;padding-bottom:12px;border-bottom:1px solid var(--line)}.evolution-compose label{color:var(--muted);font-size:8px}.evolution-compose textarea,.evolution-compose input{min-height:54px;padding:9px;border:1px solid var(--line);background:rgba(255,255,255,.02);color:var(--ink);font:inherit;font-size:9px;resize:vertical}.evolution-compose input{min-height:0}.evolution-list{display:grid;gap:8px;margin-top:12px}.evolution-list article{min-height:78px;padding:12px;border:1px solid var(--line);display:grid;grid-template-columns:36px 1fr auto;gap:11px;align-items:center;background:rgba(255,255,255,.02)}.evolution-list article.core{border-color:rgba(255,112,127,.4)}.evolution-list article.rejected{border-color:rgba(255,255,255,.08);opacity:.7}.evolution-list article small,.evolution-list article strong,.evolution-list article p{display:block}.evolution-list article small{color:var(--muted);font-size:7px}.evolution-list article strong{margin-top:5px;font-size:10px;line-height:1.35}.evolution-list article p{margin-top:4px;color:var(--ink-soft);font-size:8px;line-height:1.5}.evolution-list .core-tag{color:var(--red)}.evolution-list .core-note{color:var(--amber);font-size:8px}.evolution-actions{display:flex;gap:8px;align-items:center}.cap-icon{width:36px;height:36px;display:grid;place-items:center;background:rgba(196,246,110,.1);color:var(--lime)}
</style>
