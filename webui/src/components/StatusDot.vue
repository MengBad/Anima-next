<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ status: string; label?: string }>();
const tone = computed(() => {
  if (["running", "active", "healthy", "ready", "enabled", "ok"].includes(props.status)) return "good";
  if (["degraded", "waiting", "pending", "shadow"].includes(props.status)) return "warn";
  if (["failed", "error", "stopped", "inactive"].includes(props.status)) return "bad";
  return "quiet";
});
</script>

<template><span class="status-dot" :class="`tone-${tone}`"><i />{{ label || status }}</span></template>
