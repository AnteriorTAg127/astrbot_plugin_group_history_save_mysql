// 入口 bootstrap：加载各功能模块 + 分区/Tab 切换 + 首屏初始化
// 业务代码按功能拆分：storage.js（存储库）/ summary-settings.js（总结设置）/
// summary-ignore.js（忽略管理）/ summary-history.js（历史总结）
import { loadStatus, loadGroups, loadSettings, loadDailyStats, bindStorageEvents } from "./storage.js";
import { loadSummarySettings, bindSummarySettingsEvents } from "./summary-settings.js";
import { loadIgnoreGroups, bindIgnoreEvents } from "./summary-ignore.js";
import { loadSummaryHistory, bindHistoryEvents } from "./summary-history.js";

const bridge = window.AstrBotPluginPage;
await bridge.ready();

// ========== 分区 / Tab 切换 ==========
const lazyLoadedTabs = new Set();
const TAB_LAZY_LOAD = {
    "summary-history": () => loadSummaryHistory(1),
};

// 统一激活某个子 tab：切换高亮、显示对应页面、首次进入触发惰性加载
function activateTab(name) {
    document.querySelectorAll(".tab").forEach((t) =>
        t.classList.toggle("active", t.dataset.tab === name),
    );
    document.querySelectorAll(".page").forEach((p) =>
        p.classList.toggle("active", p.id === `page-${name}`),
    );
    if (TAB_LAZY_LOAD[name] && !lazyLoadedTabs.has(name)) {
        lazyLoadedTabs.add(name);
        TAB_LAZY_LOAD[name]();
    }
}

document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

// 顶层功能分区：存储库 / 消息总结，各自独立子 tab，顶栏标题随分区联动
const SCOPE_META = {
    storage: { icon: "💬", title: "群聊记录存储", nav: ".tabs-storage" },
    summary: { icon: "🧠", title: "消息总结", nav: ".tabs-summary" },
};
let summaryBootstrapped = false;

// 滑动高光对齐到当前激活的分区按钮
function moveScopeGlow(activeBtn) {
    const glow = document.querySelector(".scope-glow");
    if (!glow || !activeBtn) return;
    glow.style.width = `${activeBtn.offsetWidth}px`;
    glow.style.transform = `translateX(${activeBtn.offsetLeft}px)`;
    glow.style.opacity = "1";
}

function switchScope(scope) {
    const meta = SCOPE_META[scope];
    if (!meta) return;
    document.querySelectorAll(".scope-btn").forEach((b) => {
        const on = b.dataset.scope === scope;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
        if (on) moveScopeGlow(b);
    });
    document
        .querySelector(".tabs-storage")
        .classList.toggle("hidden", scope !== "storage");
    document
        .querySelector(".tabs-summary")
        .classList.toggle("hidden", scope !== "summary");
    // 顶栏标题 / 图标随分区变化
    const icon = document.getElementById("headerIcon");
    const title = document.getElementById("headerTitle");
    if (icon) icon.textContent = meta.icon;
    if (title) title.textContent = meta.title;
    // 激活该分区首个子 tab，保证始终有一个页面可见
    const first = document.querySelector(`${meta.nav} .tab`);
    if (first) activateTab(first.dataset.tab);
    // 消息总结分区首次进入才拉取设置与忽略群（存储区不再预触总结接口）
    if (scope === "summary" && !summaryBootstrapped) {
        summaryBootstrapped = true;
        loadSummarySettings();
        loadIgnoreGroups();
    }
}

document.querySelectorAll(".scope-btn").forEach((b) => {
    b.addEventListener("click", () => switchScope(b.dataset.scope));
});
window.addEventListener("resize", () => {
    const on = document.querySelector(".scope-btn.active");
    if (on) moveScopeGlow(on);
});

// ========== 初始化 ==========
async function init() {
    bindStorageEvents();
    bindSummarySettingsEvents(); // 总结设置（含备用模型弹窗）
    bindIgnoreEvents(); // 忽略管理
    bindHistoryEvents(); // 历史总结 + 总结详情弹窗
    // 默认进入存储库分区并点亮高光；总结数据延迟到进入消息总结分区时加载
    switchScope("storage");
    await Promise.all([loadStatus(), loadGroups(), loadSettings(), loadDailyStats()]);
}

init();
