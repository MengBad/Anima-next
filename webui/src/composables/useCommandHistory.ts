import { ref } from "vue";

const STORE_KEY = "anima.webui.commandHistory";
const MAX = 20;

const history = ref<string[]>([]);

function load(): void {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) history.value = JSON.parse(raw);
  } catch { history.value = []; }
}

export function useCommandHistory() {
  load();
  function push(command: string): void {
    const trimmed = command.trim();
    if (!trimmed) return;
    history.value = [trimmed, ...history.value.filter((item) => item !== trimmed)].slice(0, MAX);
    localStorage.setItem(STORE_KEY, JSON.stringify(history.value));
  }
  function clear(): void { history.value = []; localStorage.removeItem(STORE_KEY); }
  return { history, push, clear };
}
