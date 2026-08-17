<script setup lang="ts">
defineProps<{ title: string; hint?: string }>();
</script>

<template>
  <div class="section-head">
    <div>
      <h2>{{ title }}</h2>
      <p v-if="hint">{{ hint }}</p>
    </div>
    <slot name="actions" />
  </div>
</template>
