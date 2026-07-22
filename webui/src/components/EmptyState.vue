<script setup lang="ts">
import { Inbox } from "@lucide/vue";
defineProps<{ title: string; detail: string }>();
</script>

<template>
  <div class="empty-state">
    <Inbox :size="23" aria-hidden="true" />
    <strong>{{ title }}</strong>
    <span>{{ detail }}</span>
    <slot />
  </div>
</template>
