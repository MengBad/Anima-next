import { ref, watch } from "vue";

export type ThemeName = "dark" | "light" | "contrast";
export type DensityName = "comfortable" | "compact";

const STORE_KEY = "anima.webui.theme";
const DENSITY_KEY = "anima.webui.density";

const theme = ref<ThemeName>("dark");
const density = ref<DensityName>("comfortable");

function apply(): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme.value);
  root.setAttribute("data-density", density.value);
}

export function useTheme() {
  const rawTheme = localStorage.getItem(STORE_KEY) as ThemeName | null;
  if (rawTheme === "light" || rawTheme === "contrast") theme.value = rawTheme;
  const rawDensity = localStorage.getItem(DENSITY_KEY) as DensityName | null;
  if (rawDensity === "compact") density.value = rawDensity;
  apply();

  watch(theme, () => { localStorage.setItem(STORE_KEY, theme.value); apply(); });
  watch(density, () => { localStorage.setItem(DENSITY_KEY, density.value); apply(); });

  function setTheme(next: ThemeName): void { theme.value = next; }
  function setDensity(next: DensityName): void { density.value = next; }

  return { theme, density, setTheme, setDensity };
}
