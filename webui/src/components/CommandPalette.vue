<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { ArrowRight, Command, Search, X } from "@lucide/vue";

export interface CommandItem {
  id: string;
  label: string;
  hint: string;
  keywords: string;
}

const props = defineProps<{ open: boolean; items: CommandItem[] }>();
const emit = defineEmits<{ close: []; select: [item: CommandItem] }>();
const query = ref("");
const searchInput = ref<HTMLInputElement | null>(null);
const visible = computed(() => {
  const normalized = query.value.trim().toLowerCase();
  if (!normalized) return props.items;
  return props.items.filter((item) => `${item.label} ${item.hint} ${item.keywords}`.toLowerCase().includes(normalized));
});

function select(item: CommandItem): void {
  emit("select", item);
  query.value = "";
}

function close(): void {
  query.value = "";
  emit("close");
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") close();
  if (event.key === "Enter" && visible.value[0]) select(visible.value[0]);
}

watch(() => props.open, async (open) => {
  if (!open) return;
  await nextTick();
  searchInput.value?.focus();
});
</script>

<template>
  <Teleport to="body">
    <Transition name="command-palette">
      <div v-if="open" class="command-scrim" @click.self="close">
        <section class="command-palette" role="dialog" aria-modal="true" aria-label="快速命令">
          <header><Command :size="18"/><label><Search :size="16"/><input ref="searchInput" v-model="query" placeholder="搜索功能，例如：LLM、群策略、表情、诊断" @keydown="onKeydown"/></label><kbd>ESC</kbd><button aria-label="关闭命令面板" @click="close"><X :size="17"/></button></header>
          <div class="command-caption"><span>QUICK JUMP</span><small>回车打开第一项</small></div>
          <div v-if="visible.length" class="command-list"><button v-for="item in visible" :key="item.id" @click="select(item)"><span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span><ArrowRight :size="16"/></button></div>
          <div v-else class="command-empty">没有匹配的功能</div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.command-scrim{position:fixed;inset:0;z-index:90;display:grid;place-items:start center;padding-top:min(18vh,180px);background:rgba(5,4,9,.66);backdrop-filter:blur(12px)}.command-palette{width:min(620px,calc(100vw - 28px));overflow:hidden;border:1px solid rgba(196,246,110,.32);background:linear-gradient(145deg,#1c1625,#110e17);box-shadow:0 32px 100px rgba(0,0,0,.55)}.command-palette header{display:grid;grid-template-columns:25px 1fr auto 30px;gap:8px;align-items:center;padding:15px;border-bottom:1px solid var(--line);color:var(--lime)}.command-palette header>label{display:flex;align-items:center;gap:8px;color:var(--muted)}.command-palette input{width:100%;border:0;outline:0;background:transparent;color:var(--ink);font-size:11px}.command-palette kbd{padding:3px 5px;border:1px solid var(--line);color:var(--muted);font:var(--fs-xs) ui-monospace,monospace}.command-palette header button{border:0;background:transparent;color:var(--muted);cursor:pointer}.command-caption{display:flex;justify-content:space-between;padding:10px 15px;color:var(--muted);font-size:var(--fs-xs)}.command-caption span{color:var(--lime);font:700 var(--fs-xs) ui-monospace,monospace;letter-spacing:1px}.command-list{padding:0 7px 7px}.command-list button{width:100%;min-height:57px;padding:10px;border:0;background:transparent;color:var(--ink);display:flex;justify-content:space-between;align-items:center;text-align:left;cursor:pointer}.command-list button:hover{background:rgba(196,246,110,.07)}.command-list button span>*{display:block}.command-list strong{font-size:var(--fs-sm)}.command-list small{margin-top:4px;color:var(--muted);font-size:var(--fs-xs)}.command-list svg{color:var(--lime)}.command-empty{padding:28px;text-align:center;color:var(--muted);font-size:var(--fs-sm)}.command-palette-enter-active,.command-palette-leave-active{transition:160ms ease}.command-palette-enter-from,.command-palette-leave-to{opacity:0}.command-palette-enter-from .command-palette,.command-palette-leave-to .command-palette{transform:translateY(-10px) scale(.98)}
</style>
