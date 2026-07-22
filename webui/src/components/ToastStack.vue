<script setup lang="ts">
import { CheckCircle2, CircleAlert, Info, X } from "@lucide/vue";
import type { ToastMessage } from "../types";

defineProps<{ items: ToastMessage[] }>();
defineEmits<{ dismiss: [id: number] }>();
</script>

<template>
  <div class="toast-stack" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast">
      <article v-for="item in items" :key="item.id" class="toast" :class="`toast-${item.tone}`">
        <CheckCircle2 v-if="item.tone === 'success'" :size="20" />
        <CircleAlert v-else-if="item.tone === 'error'" :size="20" />
        <Info v-else :size="20" />
        <div><strong>{{ item.title }}</strong><span v-if="item.detail">{{ item.detail }}</span></div>
        <button class="icon-button" aria-label="关闭通知" @click="$emit('dismiss', item.id)"><X :size="17" /></button>
      </article>
    </TransitionGroup>
  </div>
</template>
