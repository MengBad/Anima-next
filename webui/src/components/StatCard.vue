<script setup lang="ts">
import Card from "./Card.vue";

withDefaults(defineProps<{
  label: string;
  value: string | number;
  tone?: "lime" | "pink" | "cyan" | "amber" | "purple" | "";
  sub?: string;
  enterIndex?: number;
}>(), { tone: "", enterIndex: -1 });
</script>

<template>
  <Card :enter-index="enterIndex" class="stat-card">
    <div class="stat">
      <span class="stat-label">{{ label }}</span>
      <span class="stat-value" :class="tone">{{ value }}</span>
      <span v-if="sub" class="stat-sub">{{ sub }}</span>
    </div>
  </Card>
</template>
