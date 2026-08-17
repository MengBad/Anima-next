<script setup lang="ts">
import { Monitor, Moon, Sun } from "@lucide/vue";
import { useTheme, type ThemeName, type DensityName } from "../composables/useTheme";

const { theme, density, setTheme, setDensity } = useTheme();

const themes: { id: ThemeName; icon: any; label: string }[] = [
  { id: "dark", icon: Moon, label: "暗色极光（默认）" },
  { id: "light", icon: Sun, label: "亮色" },
  { id: "contrast", icon: Monitor, label: "高对比" },
];
const densities: { id: DensityName; label: string }[] = [
  { id: "comfortable", label: "舒适" },
  { id: "compact", label: "紧凑" },
];
</script>

<template>
  <div class="pref-menu" role="group" aria-label="外观偏好">
    <div class="pref-group">
      <button
        v-for="t in themes" :key="t.id"
        class="pref-dot" :class="{ active: theme === t.id }"
        :title="t.label" :aria-pressed="theme === t.id" @click="setTheme(t.id)"
      ><component :is="t.icon" :size="15" /></button>
    </div>
    <div class="pref-divider" />
    <div class="pref-group">
      <button
        v-for="d in densities" :key="d.id"
        class="pref-text" :class="{ active: density === d.id }"
        :aria-pressed="density === d.id" @click="setDensity(d.id)"
      >{{ d.label }}</button>
    </div>
  </div>
</template>

<style scoped>
.pref-menu { display: flex; align-items: center; gap: 7px; padding: 4px 6px; border: 1px solid var(--line); border-radius: var(--radius-pill); background: rgba(255,255,255,.02); }
.pref-group { display: flex; align-items: center; gap: 3px; }
.pref-divider { width: 1px; height: 18px; background: var(--line); }
.pref-dot { width: 30px; height: 30px; border: 0; border-radius: 50%; display: grid; place-items: center; background: transparent; color: var(--muted); cursor: pointer; transition: background var(--dur-fast), color var(--dur-fast); }
.pref-dot:hover { color: var(--ink); background: rgba(255,255,255,.06); }
.pref-dot.active { color: #17210f; background: var(--lime); }
.pref-text { min-height: 30px; padding: 0 9px; border: 0; border-radius: var(--radius-pill); background: transparent; color: var(--muted); font-size: var(--fs-xs); cursor: pointer; transition: background var(--dur-fast), color var(--dur-fast); }
.pref-text.active { color: #17210f; background: var(--lime); font-weight: 700; }
@media (max-width: 767px) { .pref-menu { display: none; } }
</style>
