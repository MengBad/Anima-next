<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { api, bestEffort } from "./api";
import { activeView } from "./navigation";
import type { Health, HealthSample, ModuleStatus, ToastMessage, UsageSummary } from "./types";
import AppShell from "./components/AppShell.vue";
import ToastStack from "./components/ToastStack.vue";
import OverviewView from "./views/OverviewView.vue";
import MemoryView from "./views/MemoryView.vue";
import CognitionView from "./views/CognitionView.vue";
import AbilitiesView from "./views/AbilitiesView.vue";
import EngineeringView from "./views/EngineeringView.vue";
import SettingsView from "./views/SettingsView.vue";
import OsView from "./views/OsView.vue";
import LifeView from "./views/LifeView.vue";

const health = ref<Health | null>(null);
const modules = ref<ModuleStatus[]>([]);
const usage = ref<UsageSummary | null>(null);
const replyQuality = ref<any>(null);
const opsQuality = ref<any>(null);
const tasks = ref<any[]>([]);
const approvals = ref<any[]>([]);
const timeline = ref<any[]>([]);
const subject = ref<Record<string, any> | null>(null);
const healthHistory = ref<HealthSample[]>([]);
const loading = ref(true);
const refreshing = ref(false);
const refreshVersion = ref(0);
const toasts = ref<ToastMessage[]>([]);
let pollTimer: number | undefined;
let eventSource: EventSource | undefined;
let eventRefreshTimer: number | undefined;

const connected = computed(() => Boolean(health.value?.running && health.value?.channel_connected));

function notify(tone: ToastMessage["tone"], title: string, detail?: string): void {
  const id = Date.now() + Math.floor(Math.random() * 1000);
  toasts.value.push({ id, tone, title, detail });
  window.setTimeout(() => dismiss(id), tone === "error" ? 7000 : 4300);
}

function dismiss(id: number): void {
  toasts.value = toasts.value.filter((item) => item.id !== id);
}

async function loadOverview(silent = false): Promise<void> {
  if (!silent) loading.value = true;
  try {
    const [healthData, moduleData, usageData, qualityData, opsData, taskData, approvalData, eventData, subjectData, historyData] = await Promise.all([
      bestEffort(api<Health>("/api/health"), null),
      bestEffort(api<any>("/api/modules/status"), { modules: [] }),
      bestEffort(api<UsageSummary>("/api/llm/usage?range=today"), null),
      bestEffort(api<any>("/api/reply-quality"), null),
      bestEffort(api<any>("/api/ops-quality"), null),
      bestEffort(api<any>("/api/tasks?page_size=40"), { items: [] }),
      bestEffort(api<any>("/api/approvals?page_size=40"), { items: [] }),
      bestEffort(api<any>("/api/timeline?limit=40"), { events: [] }),
      bestEffort(api<any>("/api/subject"), null),
      bestEffort(api<{ samples: HealthSample[] }>("/api/health/history?range=24h&limit=48"), { samples: [] })
    ]);
    health.value = healthData;
    modules.value = moduleData.modules || [];
    usage.value = usageData;
    replyQuality.value = qualityData;
    opsQuality.value = opsData;
    tasks.value = taskData.items || [];
    approvals.value = approvalData.items || [];
    timeline.value = eventData.events || [];
    subject.value = subjectData;
    healthHistory.value = historyData.samples || [];
  } catch (error: any) {
    if (!silent) notify("error", "控制台数据读取失败", error.message);
  } finally {
    loading.value = false;
  }
}

async function refresh(): Promise<void> {
  refreshing.value = true;
  refreshVersion.value += 1;
  await loadOverview();
  refreshing.value = false;
  notify("success", "状态已经刷新");
}

function connectEvents(): void {
  eventSource = new EventSource("/api/events");
  eventSource.onmessage = () => {
    window.clearTimeout(eventRefreshTimer);
    eventRefreshTimer = window.setTimeout(() => loadOverview(true), 800);
  };
  eventSource.onerror = () => {
    eventSource?.close();
    window.setTimeout(connectEvents, 5000);
  };
}

onMounted(() => {
  loadOverview();
  connectEvents();
  pollTimer = window.setInterval(() => loadOverview(true), 30000);
});
onBeforeUnmount(() => {
  window.clearInterval(pollTimer);
  window.clearTimeout(eventRefreshTimer);
  eventSource?.close();
});
</script>

<template>
  <AppShell :connected="connected" :refreshing="refreshing" @refresh="refresh">
    <Transition name="view" mode="out-in">
      <OverviewView v-if="activeView === 'overview'" :key="`overview-${refreshVersion}`" :health="health" :modules="modules" :usage="usage" :quality="replyQuality" :ops="opsQuality" :tasks="tasks" :approvals="approvals" :timeline="timeline" :subject="subject" :history="healthHistory" :loading="loading" />
      <MemoryView v-else-if="activeView === 'memory'" :key="`memory-${refreshVersion}`" />
      <CognitionView v-else-if="activeView === 'cognition'" :key="`cognition-${refreshVersion}`" @notify="notify" />
      <AbilitiesView v-else-if="activeView === 'abilities'" :key="`abilities-${refreshVersion}`" @notify="notify" />
      <EngineeringView v-else-if="activeView === 'engineering'" :key="`engineering-${refreshVersion}`" @notify="notify" />
      <OsView v-else-if="activeView === 'os'" :key="`os-${refreshVersion}`" />
      <LifeView v-else-if="activeView === 'life'" :key="`life-${refreshVersion}`" @notify="notify" />
      <SettingsView v-else :key="`settings-${refreshVersion}`" @notify="notify" />
    </Transition>
  </AppShell>
  <ToastStack :items="toasts" @dismiss="dismiss" />
</template>

<style>
.view-enter-active,.view-leave-active{transition:180ms var(--ease-out)}.view-enter-from{opacity:0;transform:translateY(7px)}.view-leave-to{opacity:0;transform:translateY(-4px)}
</style>
