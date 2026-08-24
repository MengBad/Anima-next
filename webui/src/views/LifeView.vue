<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, bestEffort } from "../api";

const events = ref<any[]>([]);
const loading = ref(true);
const archive = ref<any>(null);

onMounted(async () => {
  const [tl, life] = await Promise.all([
    bestEffort(api<any>("/api/life/timeline?limit=30"), { events: [] }),
    bestEffort(api<any>("/api/life"), null),
  ]);
  events.value = tl.events || [];
  archive.value = life;
  loading.value = false;
});
</script>

<template>
  <section>
    <div class="page-head">
      <div>
        <h1>一生时间线</h1>
        <p class="muted">纵1 · 生命线 —— 入站→审议→工具→出站→降级→drive 的 trace 回放（只读）。</p>
      </div>
    </div>
    <div v-if="loading">加载一生时间线…</div>
    <div v-else-if="events.length === 0" class="empty-state">
      一生时间线暂无事件。B2 将接入 /api/life/timeline 只读回放。
    </div>
    <ul v-else class="life-timeline">
      <li v-for="(e, idx) in events" :key="idx" class="tl-item">
        <span class="tl-dot" :class="`stage-${e.stage || 'life'}`"></span>
        <span class="tl-kind">{{ e.kind || "event" }}</span>
        <span class="tl-text">{{ e.summary || e.text || "—" }}</span>
        <span v-if="e.ts" class="muted">{{ e.ts }}</span>
      </li>
    </ul>
    <section v-if="archive" class="life-archive">
      <div class="page-head"><div><h2>一生档案 · 传承</h2><p class="muted">身份快照清点 · 日记/伤痕/世界/议程（只读）。</p></div></div>
      <div class="archive-grid">
        <div class="arch-cell"><small>身份快照</small><strong>{{ archive.snapshot_count ?? 0 }}</strong></div>
        <div class="arch-cell"><small>伤痕</small><strong>{{ archive.scar_count ?? 0 }}</strong></div>
        <div class="arch-cell"><small>世界观条目</small><strong>{{ archive.world_entry_count ?? 0 }}</strong></div>
        <div class="arch-cell"><small>待办议程</small><strong>{{ archive.agenda_pending ?? 0 }}</strong></div>
      </div>
      <p v-if="archive.snapshot?.length" class="snap-line"><small>快照：{{ (archive.snapshot||[]).map((s:any)=>s.name).join("、") }}</small></p>
      <div v-if="archive.diary_tail" class="arch-note"><small>日记</small><p>{{ archive.diary_tail.slice(0, 180) }}</p></div>
      <div v-if="archive.notes_tail" class="arch-note"><small>自注</small><p>{{ archive.notes_tail.slice(0, 120) }}</p></div>
      <div v-if="archive.open_commitments?.length" class="arch-note"><small>开放承诺</small><p>{{ (archive.open_commitments as any[]).slice(0,5).map((c:any)=>c.text||c.title||c.id).join("；") }}</p></div>
    </section>
  </section>
</template>

<style scoped>
.archive-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}
.arch-cell{padding:12px;border:1px solid var(--line);border-radius:10px;background:rgba(255,255,255,.02)}
.arch-cell small,.arch-cell strong{display:block}
.arch-cell small{color:var(--muted);font-size:var(--fs-xs)}
.arch-cell strong{margin-top:5px;font:750 20px Georgia,serif}
.snap-line{margin:6px 0;color:var(--muted);font-size:var(--fs-xs)}
.arch-note{margin:8px 0;padding:9px 11px;border-left:2px solid var(--line-strong);background:rgba(255,255,255,.018)}
.arch-note small{display:block;color:var(--muted);font-size:var(--fs-xs)}
.arch-note p{margin:4px 0 0;color:var(--ink-soft);font-size:var(--fs-xs);line-height:1.5}
.life-archive{margin-top:20px}
</style>