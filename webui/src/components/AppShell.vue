<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  Bot, BrainCircuit, Boxes, Command, ChevronRight, Gauge, History, Menu, LogOut,
  MessageSquareText, Puzzle, RadioTower, RefreshCw, Settings2, ShieldCheck, Sparkles, X
} from "@lucide/vue";
import { activeAbilityTab, activeView, navigate, type AbilityTab, type ViewId } from "../navigation";
import CommandPalette, { type CommandItem } from "./CommandPalette.vue";
import ThemeToggle from "./ThemeToggle.vue";
import { api, bestEffort, json } from "../api";

defineProps<{ connected: boolean; refreshing: boolean }>();
const emit = defineEmits<{ refresh: [] }>();
const drawer = ref(false);
const commandOpen = ref(false);
const loggingOut = ref(false);

async function doLogout(): Promise<void> {
  if (loggingOut.value) return;
  loggingOut.value = true;
  try {
    await bestEffort(api<void>("/api/auth/logout", json("POST", {})), null);
  } catch {
    // 即使接口失败也强制回到登录页，避免卡在已失效的会话
  } finally {
    window.location.assign("/login");
  }
}

type NavItem = {
  id: string;
  label: string;
  hint: string;
  icon: any;
  view: ViewId;
  abilityTab?: AbilityTab;
  /** 移动底栏是否展示（侧栏始终展示） */
  mobile?: boolean;
};

const items: NavItem[] = [
  { id: "overview", view: "overview", label: "生命总览", hint: "健康与待办", icon: Gauge, mobile: true },
  { id: "life", view: "life", label: "一生时间线", hint: "生命线·快照·传承", icon: History, mobile: true },
  { id: "memory", view: "memory", label: "对话与记忆", hint: "连续性与群体", icon: MessageSquareText, mobile: true },
  { id: "cognition", view: "cognition", label: "认知状态", hint: "欲望、关系与 Sylann", icon: BrainCircuit, mobile: true },
  { id: "abilities", view: "abilities", abilityTab: "skills", label: "能力与自动化", hint: "Skills、表情与插件", icon: Puzzle, mobile: true },
  { id: "exploration", view: "abilities", abilityTab: "exploration", label: "自主探索", hint: "B站热点、新闻与知识笔记", icon: Sparkles, mobile: true },
  { id: "subscriptions", view: "abilities", abilityTab: "automation", label: "订阅监控", hint: "B站直播与网页状态", icon: RadioTower, mobile: true },
  { id: "stickers", view: "abilities", abilityTab: "stickers", label: "表情资产", hint: "审核池、图库与发送策略", icon: Bot, mobile: false },
  { id: "engineering", view: "engineering", label: "审批与工程", hint: "证据、测试与回滚", icon: ShieldCheck, mobile: true },
  { id: "os", view: "os", label: "ANIMA OS", hint: "健康证明·自治·进化", icon: Boxes, mobile: true },
  { id: "settings", view: "settings", label: "设置与权限", hint: "模型、行为与安全", icon: Settings2, mobile: true },
];

const mobileItems = computed(() => items.filter((item) => item.mobile !== false));

const gotoMap: Record<string, () => void> = {
  o: () => navigate("overview"),
  l: () => navigate("life"),
  m: () => navigate("memory"),
  c: () => navigate("cognition"),
  a: () => navigate("abilities", "skills"),
  e: () => navigate("engineering"),
  s: () => navigate("settings"),
  p: () => navigate("os"),
};
let gPending = false;

const commands: CommandItem[] = [
  { id: "overview", label: "生命总览", hint: "健康、待办与最近活动", keywords: "dashboard health 状态" },
  { id: "life", label: "一生时间线", hint: "生命线回放、身份快照、传承档案", keywords: "life 一生 时间线 生命 快照 传承" },
  { id: "memory", label: "对话与记忆", hint: "会话连续性与群体感知", keywords: "memory chat 记忆" },
  { id: "cognitive", label: "认知状态", hint: "人格、欲望、关系与 Sylann", keywords: "cognitive sylann 人格" },
  { id: "skills", label: "能力与自动化", hint: "Skills、表情与插件", keywords: "skill 能力" },
  { id: "subscriptions", label: "订阅监控", hint: "B站直播、网页状态与自动化", keywords: "subscription bili bilibili 直播 监控 订阅" },
  { id: "stickers", label: "表情资产工作室", hint: "审核池、图库、分类与策略", keywords: "sticker 表情 审核 图片" },
  { id: "engineering", label: "审批与工程", hint: "工程证据、测试、回滚与审批", keywords: "approval engineering diff 工程" },
  { id: "os", label: "ANIMA OS 治理", hint: "健康证明、长期稳定、观测、影响评分、社会、记忆、维护、能力演进", keywords: "os autonomy governance 自治进化 健康证明 稳定 记忆 维护" },
  { id: "llm", label: "模型控制中心", hint: "LLM 路由、Token 与连接测试", keywords: "llm token model 模型 api" },
  { id: "behavior", label: "自主行为与群策略", hint: "主动行为、逐群策略与紧急暂停", keywords: "group policy autonomy 暂停 群策略" },
  { id: "diagnostics", label: "NapCat 与模块诊断", hint: "QQ action/echo 回路与运行模块", keywords: "napcat qq diagnostic 诊断" },
  { id: "permissions", label: "权限管理", hint: "Owner 与管理员", keywords: "owner admin 权限" },
];

function isActive(item: NavItem): boolean {
  if (activeView.value !== item.view) return false;
  if (item.view !== "abilities") return true;
  if (item.abilityTab) return activeAbilityTab.value === item.abilityTab;
  return true;
}

const current = computed(() => items.find((item) => isActive(item)) || items[0]);

function go(item: NavItem): void {
  if (item.view === "abilities") {
    navigate("abilities", item.abilityTab || "skills");
  } else {
    navigate(item.view);
  }
  drawer.value = false;
}

function selectCommand(item: CommandItem): void {
  commandOpen.value = false;
  if (item.id === "skills") return navigate("abilities", "skills");
  if (item.id === "subscriptions") return navigate("abilities", "automation");
  if (item.id === "stickers") return navigate("abilities", "stickers");
  if (["llm", "behavior", "diagnostics", "permissions"].includes(item.id)) {
    navigate("settings");
    window.setTimeout(() => window.dispatchEvent(new CustomEvent("anima:settings-section", { detail: item.id })), 0);
    return;
  }
  if (item.id === "cognitive") return navigate("cognition");
  if (item.id === "os") return navigate("os");
  navigate(item.id as ViewId);
}

function onGlobalKeydown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null;
  const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    commandOpen.value = !commandOpen.value;
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key.toLowerCase() === "g") { gPending = true; window.setTimeout(() => (gPending = false), 900); return; }
  if (gPending && gotoMap[event.key.toLowerCase()]) {
    gPending = false;
    event.preventDefault();
    gotoMap[event.key.toLowerCase()]();
  }
}
onMounted(() => window.addEventListener("keydown", onGlobalKeydown));
onBeforeUnmount(() => window.removeEventListener("keydown", onGlobalKeydown));
</script>

<template>
  <div class="app-shell">
    <div class="aurora aurora-one" /><div class="aurora aurora-two" />
    <aside class="sidebar" :class="{ open: drawer }">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true"><span>H</span><i /></div>
        <div><strong>Anima</strong><small>生命控制台 · RC2</small></div>
        <button class="icon-button sidebar-close" aria-label="关闭导航" @click="drawer = false"><X :size="19" /></button>
      </div>
      <div class="life-badge">
        <span class="mini-pulse"><i /></span>
        <div><small>运行状态</small><strong>{{ connected ? "正在感知世界" : "连接中断" }}</strong></div>
      </div>
      <nav aria-label="主导航">
        <button
          v-for="item in items"
          :key="item.id"
          :class="{ active: isActive(item), nested: item.id === 'subscriptions' }"
          :aria-current="isActive(item) ? 'page' : undefined"
          @click="go(item)"
        >
          <component :is="item.icon" :size="19" aria-hidden="true" />
          <span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span>
          <ChevronRight :size="15" class="nav-chevron" />
        </button>
      </nav>
      <div class="sidebar-foot">
        <a href="/source"><Bot :size="17" />对应源码</a>
        <span>AGPL-3.0-only</span>
      </div>
    </aside>
    <button v-if="drawer" class="drawer-scrim" aria-label="关闭导航" @click="drawer = false" />

    <main class="main-stage">
      <header class="topbar">
        <button class="icon-button menu-button" aria-label="打开导航" @click="drawer = true"><Menu :size="21" /></button>
        <div class="page-identity">
          <component :is="current.icon" :size="18" aria-hidden="true" />
          <div><small>{{ current.hint }}</small><strong>{{ current.label }}</strong></div>
        </div>
        <div class="topbar-actions">
          <span class="connection-state" :class="{ offline: !connected }"><i />{{ connected ? "实时同步" : "连接中断" }}</span>
          <ThemeToggle />
          <button class="command-trigger" title="快速命令 (Ctrl/⌘ K)" @click="commandOpen=true"><Command :size="15"/><span>命令</span><kbd>⌘K</kbd></button>
          <button class="action-button subtle" :disabled="refreshing" @click="emit('refresh')"><RefreshCw :size="16" :class="{ spin: refreshing }" />刷新</button>
          <button class="action-button subtle danger" :disabled="loggingOut" @click="doLogout"><LogOut :size="16" />{{ loggingOut ? "登出中" : "登出" }}</button>
        </div>
      </header>
      <div class="content-frame"><slot /></div>
    </main>

    <nav class="mobile-nav" aria-label="移动端主导航">
      <button v-for="item in mobileItems" :key="item.id" :class="{ active: isActive(item) }" @click="go(item)">
        <component :is="item.icon" :size="20" /><span>{{ item.label.replace('与自动化', '').replace('与工程', '').replace('订阅监控', '订阅') }}</span>
      </button>
    </nav>
  </div>
  <CommandPalette :open="commandOpen" :items="commands" @close="commandOpen=false" @select="selectCommand" />
</template>
