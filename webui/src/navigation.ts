import { ref } from "vue";

export type ViewId = "overview" | "memory" | "cognition" | "abilities" | "engineering" | "settings" | "os";
export type AbilityTab = "skills" | "exploration" | "automation" | "stickers" | "plugins";

const routeMap: Record<string, ViewId> = {
  "/": "overview",
  "/memory": "memory",
  "/cognitive": "cognition",
  "/abilities": "abilities",
  "/exploration": "abilities",
  "/subscriptions": "abilities",
  "/stickers": "abilities",
  "/engineering": "engineering",
  "/settings": "settings",
  "/os": "os"
};

function abilityTabFromPath(pathname: string): AbilityTab {
  if (pathname === "/exploration") return "exploration";
  if (pathname === "/stickers") return "stickers";
  if (pathname === "/subscriptions") return "automation";
  return "skills";
}

export const activeView = ref<ViewId>(routeMap[window.location.pathname] || "overview");
export const activeAbilityTab = ref<AbilityTab>(abilityTabFromPath(window.location.pathname));

export function navigate(view: ViewId, subview?: string): void {
  activeView.value = view;
  if (view === "abilities") {
    activeAbilityTab.value = (subview as AbilityTab) || "skills";
  }
  let path = `/${view}`;
  if (view === "overview") path = "/";
  else if (view === "cognition") path = "/cognitive";
  else if (view === "os") path = "/os";
  else if (view === "abilities") {
    if (activeAbilityTab.value === "stickers") path = "/stickers";
    else if (activeAbilityTab.value === "exploration") path = "/exploration";
    else if (activeAbilityTab.value === "automation") path = "/subscriptions";
    else path = "/abilities";
  }
  window.history.pushState({}, "", path);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

window.addEventListener("popstate", () => {
  activeView.value = routeMap[window.location.pathname] || "overview";
  if (activeView.value === "abilities") {
    activeAbilityTab.value = abilityTabFromPath(window.location.pathname);
  }
});

export function viewFromPath(pathname: string): ViewId {
  return routeMap[pathname] || "overview";
}
