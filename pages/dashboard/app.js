// 入口 bootstrap：加载各功能模块 + 分区/Tab 切换 + 首屏初始化
// 业务代码按功能拆分：storage.js（存储库）/ summary-settings.js（总结设置）/
// summary-ignore.js（忽略管理）/ summary-history.js（历史总结）/
// profile-settings.js / profile-launch.js / profile-history.js（v0.4.0 人物分析）
import { loadStatus, loadGroups, loadSettings, loadDailyStats, bindStorageEvents } from "./storage.js";
import { loadSummarySettings, bindSummarySettingsEvents } from "./summary-settings.js";
import { loadIgnoreGroups, bindIgnoreEvents } from "./summary-ignore.js";
import { loadSummaryHistory, bindHistoryEvents } from "./summary-history.js";
import { loadProfileSettings, bindProfileSettingsEvents } from "./profile-settings.js";
import { loadProfileGroups, bindProfileLaunchEvents } from "./profile-launch.js";
import { loadProfileHistory, bindProfileHistoryEvents } from "./profile-history.js";

const bridge = window.AstrBotPluginPage;
await bridge.ready();

// ========== 分区 / Tab 切换 ==========
const lazyLoadedTabs = new Set();
const TAB_LAZY_LOAD = {
    "summary-history": () => loadSummaryHistory(1),
    // v0.4.0：人物分析两 tab 惰性加载（群列表/历史列表仅在进入对应 tab 时请求）
    "profile-launch": () => loadProfileGroups(),
    "profile-history": () => loadProfileHistory(1),
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

// 顶层功能分区：存储库 / 消息总结 / 人物分析（v0.4.0），各自独立子 tab，顶栏标题随分区联动
const SCOPE_META = {
    storage: { icon: "💬", title: "群聊记录存储", nav: ".tabs-storage" },
    summary: { icon: "🧠", title: "消息总结", nav: ".tabs-summary" },
    profile: { icon: "👤", title: "人物分析", nav: ".tabs-profile" },
};
let summaryBootstrapped = false;
let profileBootstrapped = false;

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
    // 各分区子 tab 行：仅当前分区可见（三分区统一遍历，新增分区零改动）
    for (const [key, meta] of Object.entries(SCOPE_META)) {
        const nav = document.querySelector(meta.nav);
        if (nav) nav.classList.toggle("hidden", key !== scope);
    }
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
    // v0.4.0：人物分析分区首次进入才拉取设置（启动不预触 profile 端点）；
    // 发起分析群列表 / 历史列表由 TAB_LAZY_LOAD 在各自 tab 首次激活时加载
    if (scope === "profile" && !profileBootstrapped) {
        profileBootstrapped = true;
        loadProfileSettings();
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
    bindProfileSettingsEvents(); // v0.4.0 人物分析设置（复用备用模型弹窗组件）
    bindProfileLaunchEvents(); // v0.4.0 发起分析
    bindProfileHistoryEvents(); // v0.4.0 历史分析 + 详情弹窗
    // 默认进入存储库分区并点亮高光；总结/人物分析数据延迟到进入对应分区时加载
    switchScope("storage");
    await Promise.all([loadStatus(), loadGroups(), loadSettings(), loadDailyStats()]);
}

init();
