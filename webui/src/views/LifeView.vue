<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, bestEffort, restoreSnapshot, getStagedSnapshots, rollbackSnapshot } from "../api";

const events = ref<any[]>([]);
const loading = ref(true);
const archive = ref<any>(null);
const staged = ref<any[]>([]);
const notice = ref("");
const busy = ref("");

async function loadStaged(): Promise<void> {
  const r = (await bestEffort(getStagedSnapshots(), { staged: [] })) as any;
  staged.value = (r?.staged || []) as any[];
}

async function doRestore(name: string): Promise<void> {
  if (!window.confirm(`将快照「${name}」暂存恢复？将自动备份当前并原子替换到 data/life_restore/，可回滚。`)) return;
  busy.value = name;
  notice.value = "";
  try {
    const r = (await restoreSnapshot(name)) as any;
    notice.value = r.staged ? `已暂存恢复 ${name}（备份 ${r.backup_path}）` : `暂存失败：${r.error || "未知"}`;
    await loadStaged();
  } catch (e: any) {
    notice.value = `恢复失败：${e?.message || e}`;
  } finally {
    busy.value = "";
  }
}

async function doRollback(name: string): Promise<void> {
  if (!window.confirm(`回滚「${name}」到最近备份？`)) return;
  busy.value = "@" + name;
  notice.value = "";
  try {
    const r = (await rollbackSnapshot(name)) as any;
    notice.value = r.rolled_back ? `已回滚 ${name}` : `回滚失败：${r.error || "未知"}`;
    await loadStaged();
  } catch (e: any) {
    notice.value = `回滚失败：${e?.message || e}`;
  } finally {
    busy.value = "";
  }
}

onMounted(async () => {
  const [tl, life] = await Promise.all([
    bestEffort(api<any>("/api/life/timeline?limit=30"), { events: [] }),
    bestEffort(api<any>("/api/life"), null),
  ]);
  events.value = tl.events || [];
  archive.value = life;
  await loadStaged();
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

      <div class="restore-panel">
        <div class="page-head"><div><h3>快照恢复</h3><p class="muted">Owner 专用 · 可逆暂存（自动备份 + 原子替换 + 可回滚）；不触碰 live 身份/记忆/chat_memory。</p></div></div>
        <div v-if="notice" class="restore-notice">{{ notice }}</div>
        <div v-if="(archive.snapshot || []).length" class="restore-list">
          <div class="restore-row" v-for="s in (archive.snapshot as any[])" :key="s.name">
            <span class="mono">{{ s.name }}</span><small>{{ s.bytes }}B · {{ s.age_days }}d</small>
            <button class="action-button subtle" :disabled="busy!==''" @click="doRestore(s.name)">{{ busy === s.name ? "暂存中…" : "暂存恢复" }}</button>
          </div>
        </div>
        <div v-else class="muted-note">暂无身份/人格快照文件。待系统积累历史快照后，可在此安全恢复并回滚。</div>
        <div v-if="staged.length" class="staged-box">
          <strong>已暂存待生效</strong>
          <div class="restore-row" v-for="n in staged" :key="n">
            <span class="mono">{{ n }}</span>
            <button class="action-button subtle danger" :disabled="busy!==''" @click="doRollback(n)">{{ busy === '@' + n ? "回滚中…" : "回滚" }}</button>
          </div>
        </div>
      </div>
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
.restore-panel{margin-top:16px;padding:14px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.02)}
.restore-notice{margin:8px 0;padding:8px 10px;border-left:2px solid var(--cyan);background:rgba(120,220,255,.06);font-size:var(--fs-xs);color:var(--ink-soft)}
.restore-list{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.restore-row{display:flex;align-items:center;gap:10px;font-size:var(--fs-sm)}
.restore-row .mono{font:600 var(--fs-sm) ui-monospace,monospace}
.restore-row small{color:var(--muted)}
.restore-row .action-button{margin-left:auto}
.staged-box{margin-top:12px;border-top:1px solid var(--line);padding-top:10px;display:flex;flex-direction:column;gap:6px}
.staged-box strong{font-size:var(--fs-sm);color:var(--muted)}
.action-button.danger{color:var(--red)}
.muted-note{font-size:var(--fs-xs);color:var(--muted)}
</style>