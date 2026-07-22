<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  BookHeart, BrainCircuit, Clock3, Database, Layers3, MessageCircleMore,
  NotebookPen, ScanSearch, UsersRound, Waves
} from "@lucide/vue";
import { api, bestEffort, formatNumber, formatTime, timeAgo } from "../api";
import EmptyState from "../components/EmptyState.vue";
import StatusDot from "../components/StatusDot.vue";
import type { ModuleStatus } from "../types";

const life = ref<any>(null);
const subject = ref<any>(null);
const timeline = ref<any[]>([]);
const health = ref<any>(null);
const loading = ref(true);

async function load() {
  loading.value = true;
  try {
    const [lifeData, subjectData, events, memoryHealth] = await Promise.all([
      bestEffort(api<any>("/api/life"), null),
      bestEffort(api<any>("/api/subject"), null),
      bestEffort(api<any>("/api/timeline?limit=40"), { events: [] }),
      bestEffort(api<any>("/api/memory/health"), null),
    ]);
    life.value = lifeData;
    subject.value = subjectData;
    timeline.value = events.events || [];
    health.value = memoryHealth;
  } finally {
    loading.value = false;
  }
}

const modules = computed<ModuleStatus[]>(() => health.value?.modules || []);
const signals = computed(() => health.value?.signals || []);
const recentVector = computed(() => health.value?.recent_vector || []);
const chatSessions = computed(() => health.value?.chat_sessions || []);
const crossgroup = computed(() => health.value?.crossgroup || {});
const gates = computed(() => health.value?.gates || {});
const healthTone = computed(() => health.value?.status || "healthy");
const healthTitle = computed(() => {
  if (healthTone.value === "attention") return "记忆召回需要处理";
  if (healthTone.value === "watch") return "记忆子系统基本可用，有可观察信号";
  return "记忆召回链路健康";
});

function metricLine(item: ModuleStatus): string {
  const m = item.metrics || {};
  const parts: string[] = [];
  for (const [key, value] of Object.entries(m)) {
    if (value === null || value === undefined || value === "") continue;
    if (typeof value === "boolean") {
      parts.push(`${key}=${value ? "是" : "否"}`);
      continue;
    }
    if (typeof value === "number" || typeof value === "string") {
      parts.push(`${key} ${value}`);
    }
  }
  return parts.slice(0, 4).join(" · ") || item.message;
}

onMounted(load);
</script>

<template>
  <section class="page memory-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">continuity</span>
        <h1>对话留下了什么？</h1>
        <p>连续性摘要 + 记忆模块健康：看得到盘上计数与召回状态，不把完整聊天原文摊开。</p>
      </div>
      <StatusDot :status="healthTone" :label="loading ? '读取中' : healthTitle" />
    </div>

    <div class="memory-hero panel">
      <div>
        <BookHeart :size="28" />
        <span>连续性状态</span>
        <strong>{{ loading ? "正在整理…" : healthTitle }}</strong>
        <p>短期会话、向量、跨群、情节与自我笔记在生成前统一组装；各阶段失败隔离，不拖死主回复。</p>
        <div v-if="signals.length" class="signal-row">
          <article v-for="item in signals.slice(0, 4)" :key="item.key" :class="item.level">
            <small>{{ item.level }}</small>
            <strong>{{ item.label }}</strong>
            <span>{{ item.detail }}</span>
          </article>
        </div>
      </div>
      <dl>
        <div><dt>世界条目</dt><dd>{{ formatNumber(life?.world_entry_count ?? modules.find(m => m.id === 'cognitive_memory')?.metrics?.world_entries) }}</dd></div>
        <div><dt>伤痕记忆</dt><dd>{{ formatNumber(life?.scar_count ?? modules.find(m => m.id === 'cognitive_memory')?.metrics?.scars) }}</dd></div>
        <div><dt>待办意图</dt><dd>{{ formatNumber(life?.agenda_pending) }}</dd></div>
        <div><dt>近期互动者</dt><dd>{{ formatNumber(subject?.recent_interactions) }}</dd></div>
        <div><dt>情节记忆</dt><dd>{{ formatNumber(life?.episodic_memory?.episodes ?? modules.find(m => m.id === 'episodes')?.metrics?.episodes) }}</dd></div>
        <div><dt>未完成约定</dt><dd>{{ formatNumber(life?.episodic_memory?.commitments_open ?? modules.find(m => m.id === 'episodes')?.metrics?.commitments_open) }}</dd></div>
        <div><dt>向量条目</dt><dd>{{ formatNumber(modules.find(m => m.id === 'vector_memory')?.metrics?.count) }}</dd></div>
        <div><dt>跨群事实</dt><dd>{{ formatNumber(crossgroup.facts) }}</dd></div>
      </dl>
    </div>

    <div class="section-title">
      <div>
        <h2>记忆模块工作状态</h2>
        <p>运行态 / 盘上计数 / 注入闸门。跨群会显示懒加载与磁盘画像，避免「假空」。</p>
      </div>
    </div>
    <div v-if="modules.length" class="module-grid">
      <article v-for="item in modules" :key="item.id" class="panel module-card">
        <header>
          <div>
            <small>{{ item.id }}</small>
            <strong>{{ item.label }}</strong>
          </div>
          <StatusDot :status="item.status" :label="item.status" />
        </header>
        <p>{{ item.message }}</p>
        <span>{{ metricLine(item) }}</span>
      </article>
    </div>
    <EmptyState v-else-if="!loading" title="记忆健康接口暂不可用" detail="后端 /api/memory/health 未返回模块列表。" />

    <div class="memory-columns health-columns">
      <section class="panel">
        <div class="panel-head">
          <div><strong>最近向量写入</strong><span>按时间序摘要，非语义命中分数；打开页面不触发 embedding</span></div>
          <ScanSearch :size="18" />
        </div>
        <div v-if="recentVector.length" class="vector-list">
          <article v-for="item in recentVector" :key="`${item.rowid}-${item.ts}`">
            <span>#{{ item.rowid }}</span>
            <div>
              <strong>{{ item.preview || "（空文本）" }}</strong>
              <small>
                {{ item.role || "unknown" }}
                · salience {{ Number(item.salience || 0).toFixed(2) }}
                · {{ item.ts ? timeAgo(item.ts) : "未知时间" }}
              </small>
            </div>
          </article>
        </div>
        <EmptyState v-else title="还没有向量记忆条目" detail="完成带回复的对话后，用户消息会写入长期向量库。" />
      </section>

      <section class="panel">
        <div class="panel-head">
          <div><strong>短期会话桶</strong><span>按 (群,用户) 隔离；@ 闸门会显著影响数量</span></div>
          <Database :size="18" />
        </div>
        <div class="gate-chips">
          <span>仅 @ 回复：{{ gates.reply_only_when_mentioned ? "开" : "关" }}</span>
          <span>跨群观察全量：{{ gates.crossgroup_observe_all ? "开" : "关" }}</span>
          <span>向量：{{ gates.vector_enabled ? "开" : "关" }}</span>
          <span>图描述入库：{{ gates.vision_store_desc_to_memory ? "开" : "关" }}</span>
        </div>
        <div v-if="chatSessions.length" class="session-list">
          <article v-for="item in chatSessions" :key="item.key">
            <Layers3 :size="15" />
            <div>
              <strong>{{ item.key }}</strong>
              <small>{{ item.messages }} 条 · {{ item.rounds }} 轮 · {{ item.last_ts ? timeAgo(item.last_ts) : "无时间" }}</small>
            </div>
          </article>
        </div>
        <EmptyState v-else title="暂无短期会话桶" detail="群聊在仅 @ 模式下，只有被呼叫的对话会进入完整记忆流水线。" />
      </section>
    </div>

    <div class="memory-columns">
      <section class="panel">
        <div class="panel-head">
          <div><strong>跨群画像（盘上）</strong><span>用户事实与群氛围；运行时未挂载时仍可读盘</span></div>
          <UsersRound :size="18" />
        </div>
        <div class="cg-summary">
          <div><dt>用户</dt><dd>{{ formatNumber(crossgroup.users) }}</dd></div>
          <div><dt>事实</dt><dd>{{ formatNumber(crossgroup.facts) }}</dd></div>
          <div><dt>群氛围</dt><dd>{{ formatNumber(crossgroup.groups) }}</dd></div>
          <div><dt>运行时</dt><dd>{{ crossgroup.runtime_loaded ? "已挂载" : "懒加载" }}</dd></div>
        </div>
        <div v-if="crossgroup.top_users?.length" class="presence-list">
          <article v-for="item in crossgroup.top_users.slice(0, 6)" :key="item.user_id">
            <span><BrainCircuit :size="16" /></span>
            <div>
              <strong>{{ item.name || item.user_id }}</strong>
              <small>{{ item.facts }} 条事实 · {{ item.user_id }}</small>
            </div>
          </article>
        </div>
        <div v-if="crossgroup.atmospheres?.length" class="atm-list">
          <article v-for="item in crossgroup.atmospheres.slice(0, 4)" :key="item.group_id">
            <Waves :size="14" />
            <div>
              <strong>群 {{ item.group_id }} · {{ item.style || "unknown" }}</strong>
              <small>
                {{ (item.descriptors || []).join("、") || "无描述" }}
                · {{ item.last_updated ? timeAgo(item.last_updated) : "未知" }}
              </small>
            </div>
          </article>
        </div>
        <EmptyState v-if="!crossgroup.users" title="还没有跨群画像" detail="开启 observe_all 后，群消息会逐步沉淀用户事实与氛围。" />
      </section>

      <section class="panel">
        <div class="panel-head">
          <div><strong>群体感知</strong><span>她最近在哪些群体环境里活动</span></div>
          <MessageCircleMore :size="18" />
        </div>
        <div v-if="subject?.group_presence?.length" class="presence-list">
          <article v-for="item in subject.group_presence" :key="item.group_id">
            <span><MessageCircleMore :size="16" /></span>
            <div>
              <strong>群 {{ item.group_id }}</strong>
              <small>{{ item.total_messages || item.message_count || 0 }} 条观察 · {{ timeAgo(item.last_seen_at || item.last_seen || item.updated_at) }}</small>
            </div>
          </article>
        </div>
        <EmptyState v-else title="还没有形成群体画像" detail="接收到群消息后，这里会出现活动程度与最近观察。" />
      </section>

      <section class="panel">
        <div class="panel-head">
          <div><strong>自我笔记</strong><span>经过整理的尾部摘要，而非原始消息</span></div>
          <NotebookPen :size="18" />
        </div>
        <div class="note-preview">
          <p>{{ life?.notes_tail || health?.notes_tail || "尚未形成可展示的自我笔记。" }}</p>
        </div>
      </section>

      <section class="panel" v-if="life?.open_commitments?.length">
        <div class="panel-head">
          <div><strong>未完成约定</strong><span>仅摘要，不展示原始聊天正文</span></div>
          <Clock3 :size="18" />
        </div>
        <div class="presence-list">
          <article v-for="item in life.open_commitments.slice(0, 6)" :key="item.id">
            <span><NotebookPen :size="16" /></span>
            <div>
              <strong>{{ item.text }}</strong>
              <small>{{ item.scope }} · {{ timeAgo(item.created_at) }}</small>
            </div>
          </article>
        </div>
      </section>
    </div>

    <div class="section-title">
      <div>
        <h2>连续性事件</h2>
        <p>消息、记忆和认知相关活动的可读时间线。</p>
      </div>
    </div>
    <div v-if="timeline.length" class="memory-timeline panel">
      <article v-for="item in timeline.slice(0, 16)" :key="`${item.ts}-${item.type}`">
        <span><i /></span>
        <div>
          <strong>{{ String(item.type || "memory_event").replaceAll("_", " ") }}</strong>
          <small><Clock3 :size="12" />{{ timeAgo(item.ts) }} · {{ formatTime(item.ts) }}</small>
        </div>
      </article>
    </div>
    <EmptyState v-else title="时间线是空的" detail="Bot 运行后，经过脱敏的事件会显示在这里。" />
  </section>
</template>

<style scoped>
.page-heading{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
.memory-hero{padding:clamp(25px,4vw,42px);display:grid;grid-template-columns:1.15fr 1fr;gap:35px;background:linear-gradient(120deg,rgba(76,56,91,.65),rgba(24,20,34,.92))}
.memory-hero>div>svg{color:var(--pink)}
.memory-hero span,.memory-hero strong{display:block}
.memory-hero span{margin-top:14px;color:var(--muted);font-size:9px}
.memory-hero strong{margin-top:6px;font:730 21px Georgia,"Songti SC",serif}
.memory-hero p{margin:8px 0 0;color:var(--muted);font-size:11px;line-height:1.7}
.memory-hero dl{margin:0;display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
.memory-hero dl div{padding:17px;background:#1a1524}
.memory-hero dt{color:var(--muted);font-size:9px}
.memory-hero dd{margin:7px 0 0;font:730 20px Georgia,serif;color:var(--lime)}
.signal-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:16px}
.signal-row article{padding:10px 12px;border:1px solid var(--line);background:rgba(255,255,255,.03)}
.signal-row article.attention{border-color:rgba(255,120,120,.35);background:rgba(255,120,120,.06)}
.signal-row article.watch{border-color:rgba(255,201,107,.35);background:rgba(255,201,107,.06)}
.signal-row small,.signal-row strong,.signal-row span{display:block}
.signal-row small{color:var(--muted);font-size:7px;text-transform:uppercase;letter-spacing:.08em}
.signal-row strong{margin-top:4px;font-size:11px}
.signal-row span{margin-top:4px;color:var(--muted);font-size:9px;line-height:1.4}
.module-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
.module-card{padding:14px 16px;min-height:118px}
.module-card header{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.module-card small,.module-card strong,.module-card p,.module-card>span{display:block}
.module-card small{color:var(--muted);font:740 7px ui-monospace,monospace;letter-spacing:.06em}
.module-card strong{margin-top:4px;font-size:12px}
.module-card p{margin:10px 0 0;color:var(--ink-soft);font-size:10px;line-height:1.45}
.module-card>span{margin-top:10px;color:var(--muted);font-size:8px;line-height:1.4}
.memory-columns{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.health-columns{margin-top:0}
.panel-head svg{color:var(--cyan)}
.presence-list,.vector-list,.session-list,.atm-list{padding:5px 17px}
.presence-list article,.vector-list article,.session-list article,.atm-list article{min-height:62px;display:flex;align-items:center;gap:11px;border-bottom:1px solid var(--line)}
.presence-list article>span,.vector-list article>span{width:31px;height:31px;display:grid;place-items:center;background:rgba(123,219,223,.1);color:var(--cyan);font:740 8px ui-monospace,monospace}
.session-list article>svg,.atm-list article>svg{color:var(--purple);flex:0 0 auto}
.presence-list strong,.presence-list small,.vector-list strong,.vector-list small,.session-list strong,.session-list small,.atm-list strong,.atm-list small{display:block}
.presence-list strong,.vector-list strong,.session-list strong,.atm-list strong{font-size:11px;line-height:1.4}
.presence-list small,.vector-list small,.session-list small,.atm-list small{margin-top:4px;color:var(--muted);font-size:9px}
.gate-chips{display:flex;flex-wrap:wrap;gap:6px;padding:12px 17px 4px}
.gate-chips span{padding:4px 8px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--muted);font-size:8px}
.cg-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);margin:0}
.cg-summary div{padding:12px;background:#1a1524}
.cg-summary dt{color:var(--muted);font-size:8px}
.cg-summary dd{margin:6px 0 0;font:730 16px Georgia,serif;color:var(--lime)}
.note-preview{padding:22px;min-height:230px;background:linear-gradient(transparent 26px,rgba(255,255,255,.035) 27px);background-size:100% 27px}
.note-preview p{white-space:pre-wrap;margin:0;color:var(--ink-soft);font:11px/27px ui-monospace,"SFMono-Regular",Consolas,monospace}
.memory-timeline{display:grid;grid-template-columns:1fr 1fr;padding:15px}
.memory-timeline article{min-height:62px;display:grid;grid-template-columns:25px 1fr;gap:8px}
.memory-timeline article>span{position:relative}
.memory-timeline article>span::after{content:"";position:absolute;left:7px;top:18px;bottom:-8px;width:1px;background:var(--line)}
.memory-timeline article>span i{position:absolute;left:4px;top:10px;width:7px;height:7px;border-radius:50%;background:var(--purple)}
.memory-timeline strong,.memory-timeline small{display:block}
.memory-timeline strong{font-size:10px}
.memory-timeline small{display:flex;align-items:center;gap:5px;margin-top:5px;color:var(--muted);font-size:8px}
@media(max-width:980px){.module-grid{grid-template-columns:1fr 1fr}}
@media(max-width:750px){.memory-hero,.memory-columns,.module-grid,.signal-row,.cg-summary,.memory-timeline{grid-template-columns:1fr}.page-heading{flex-direction:column}}
</style>
