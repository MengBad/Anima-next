import { createApp } from "vue";
import App from "./App.vue";
import "./styles.css";

// Apply persisted theme/density before first paint to avoid flash.
const theme = localStorage.getItem("anima.webui.theme");
const density = localStorage.getItem("anima.webui.density");
if (theme) document.documentElement.setAttribute("data-theme", theme);
if (density) document.documentElement.setAttribute("data-density", density);

createApp(App).mount("#app");
