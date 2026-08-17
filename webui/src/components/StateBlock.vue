<script setup lang="ts">
import { AlertTriangle, Inbox, Loader2 } from "@lucide/vue";
import EmptyState from "./EmptyState.vue";

withDefaults(defineProps<{
  state: "loading" | "empty" | "error" | "ready";
  loadingRows?: number;
  emptyTitle?: string;
  emptyDetail?: string;
  errorTitle?: string;
  errorDetail?: string;
}>(), {
  loadingRows: 3,
  emptyTitle: "这里还没有内容",
  emptyDetail: "相关数据会在系统运行后逐渐累积。",
  errorTitle: "读取失败",
  errorDetail: "请稍后重试，或检查网络连接。",
});
</script>

<template>
  <div v-if="state === 'loading'" class="state-skeleton" aria-busy="true">
    <div v-for="n in loadingRows" :key="n" class="skeleton" />
  </div>
  <EmptyState v-else-if="state === 'empty'" :title="emptyTitle" :detail="emptyDetail">
    <slot name="empty-actions" />
  </EmptyState>
  <div v-else-if="state === 'error'" class="state-error">
    <AlertTriangle :size="22" />
    <strong>{{ errorTitle }}</strong>
    <span>{{ errorDetail }}</span>
    <slot name="error-actions" />
  </div>
  <slot v-else />
</template>

<style scoped>
.state-skeleton { display: grid; gap: var(--space-grid); }
.state-skeleton .skeleton { min-height: 64px; border-radius: var(--radius-md); }
.state-error { min-height: 190px; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; gap: 7px; padding: 24px; border: 1px solid rgba(255,123,133,.28); border-radius: var(--radius-card); background: rgba(255,123,133,.05); color: var(--muted); }
.state-error svg { color: var(--red); }
.state-error strong { color: var(--ink-soft); font-size: var(--fs-title); }
.state-error span { max-width: 340px; font-size: var(--fs-sm); line-height: 1.55; }
</style>
