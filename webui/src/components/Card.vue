<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(defineProps<{
  interactive?: boolean;
  as?: string;
  enterIndex?: number;
}>(), { interactive: false, as: "article", enterIndex: -1 });

const cls = computed(() => ({
  card: true,
  "card-pad": true,
  interactive: props.interactive,
  enter: props.enterIndex >= 0,
}));
const style = computed(() => props.enterIndex >= 0 ? { "--enter-i": String(props.enterIndex) } : undefined);
</script>

<template>
  <component :is="as" :class="cls" :style="style">
    <slot />
  </component>
</template>
