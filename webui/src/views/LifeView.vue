<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, bestEffort } from "../api";

const events = ref<any[]>([]);
const loading = ref(true);

onMounted(async () => {
  const data = await bestEffort(api<any>("/api/life/timeline?limit=30"), { events: [] });
  events.value = data.events || [];
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
    <div class="note">缺失（B2/B6 落位）：身份快照/恢复卡片 · 遗忘保连续 · 传承档案。</div>
  </section>
</template>