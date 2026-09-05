// 入口 bootstrap：加载各功能模块 + 分区/Tab 切换 + 首屏初始化
// 业务代码按功能拆分：storage.js（存储库）/ summary-settings.js（总结设置）/
// summary-ignore.js（忽略管理）/ summary-history.js（历史总结）/
// profile-settings.js / profile-launch.js / profile-history.js（v0.4.0 人物分析）/
// data-analysis.js（v0.5.0 数据分析）
import { loadStatus, loadGroups, loadSettings, loadDailyStats, loadQueryLog, bindStorageEvents } from "./storage.js";
import { loadSummarySettings, bindSummarySettingsEvents } from "./summary-settings.js";
import { loadIgnoreGroups, bindIgnoreEvents } from "./summary-ignore.js";
import { loadSummaryHistory, bindHistoryEvents } from "./summary-history.js";
import { loadProfileSettings, bindProfileSettingsEvents } from "./profile-settings.js";
import { loadProfileGroups, bindProfileLaunchEvents } from "./profile-launch.js";
import { loadProfileHistory, bindProfileHistoryEvents } from "./profile-history.js";
import { loadDataAnalysis, bindDataAnalysisEvents, enterDataAnalysis } from "./data-analysis.js";
import { el, showToast } from "./common.js";

const bridge = window.AstrBotPluginPage;
await bridge.ready();

// ========== v0.9.0 存储后端信息（顶部危险横幅 / 功能可用性判定） ==========
// storage/info 由 app.js 启动时拉取一次；provider 数据源在 bootstrap（纯内存
// dict，无 I/O 阻塞）。拉取失败保持 null：不渲染横幅、不做可用性拦截，
// 面板其余功能与改动前完全一致（横幅不阻塞主功能）。
let storageInfo = null;

// 清空弹窗静态说明原文（首次读取后作为追加基底，避免重复拼接）
const PURGE_DESC_BASE = (document.getElementById("purgeModalDesc") || {}).textContent || "";

async function loadStorageInfo() {
    try {
        const info = await bridge.apiGet("storage/info");
        storageInfo = info && typeof info === "object" ? info : null;
    } catch {
        storageInfo = null; // 静默：旧版本后端无该端点也不影响面板使用
        return;
    }
    renderStorageBanner(storageInfo);
    applySqlitePurgeHints(storageInfo);
}

// 三分支渲染（PRD F2 三重提醒③）：
// ① backend=sqlite → 红色常驻横幅（无关闭按钮），history_db_path 非空时小字第二行显示路径；
// ② backend=mysql 且 lock_mismatch → 黄色可关闭横幅；
// ③ 其余（mysql 正常）→ 不渲染（容器保持隐藏）。
// 全部文本走 textContent / el()，零 HTML 注入面。
function renderStorageBanner(info) {
    const banner = document.getElementById("storageBanner");
    if (!banner) return;
    banner.textContent = "";
    banner.className = "storage-banner hidden";
    if (!info) return;

    if (info.backend === "sqlite") {
        banner.appendChild(
            el(
                "div",
                "storage-banner-text",
                "⚠️ 当前为 SQLite 备用存储（危险选项已锁定）。数据分析与 /群统计 不可用；" +
                    "请勿随意切换存储后端，MySQL / SQLite 两侧数据完全独立、互不迁移。",
            ),
        );
        const path = typeof info.history_db_path === "string" ? info.history_db_path : "";
        if (path) banner.appendChild(el("div", "storage-banner-sub", `数据文件：${path}`));
        banner.classList.add("danger");
        banner.classList.remove("hidden");
        return;
    }

    if (info.backend === "mysql" && info.lock_mismatch) {
        const cfg = info.config_backend == null ? "?" : String(info.config_backend);
        banner.appendChild(
            el(
                "div",
                "storage-banner-text",
                `⚠️ 检测到 storage_backend=${cfg} 配置被更改但未生效，已锁定为 MySQL 继续运行；` +
                    "切换存储后端请按插件配置页警告说明操作。",
            ),
        );
        const closeBtn = el("button", "storage-banner-close", "✕");
        closeBtn.type = "button";
        closeBtn.title = "关闭提示";
        closeBtn.addEventListener("click", () => banner.classList.add("hidden"));
        banner.appendChild(closeBtn);
        banner.classList.add("warn");
        banner.classList.remove("hidden");
    }
}

// SQLite 模式下的清空类文案补充（数据表同名，但目标库是本地 history.db）
function applySqlitePurgeHints(info) {
    if (!info || info.backend !== "sqlite") return;
    const desc = document.getElementById("purgeModalDesc");
    if (desc) {
        desc.textContent = `${PURGE_DESC_BASE}目标：SQLite history.db 全部聊天记录。`;
    }
    const zone = document.getElementById("purgeZoneDesc");
    if (zone) {
        zone.textContent =
            "清空 SQLite（history.db）中全部聊天记录与图片记录，不可恢复。需要通过随机加减法验证";
    }
}

// 数据分析在 SQLite 模式不可用（bootstrap 不构造 stats_service，stats/* 端点 503）
function statsUnavailable() {
    return !!storageInfo && storageInfo.stats_available === false;
}

// ========== 分区 / Tab 切换 ==========
const lazyLoadedTabs = new Set();
const TAB_LAZY_LOAD = {
    "summary-history": () => loadSummaryHistory(1),
    // v0.4.0：人物分析两 tab 惰性加载（群列表/历史列表仅在进入对应 tab 时请求）
    "profile-launch": () => loadProfileGroups(),
    "profile-history": () => loadProfileHistory(1),
    // v0.5.0：数据分析 tab 惰性加载（进入才首次请求 stats/data 与推送设置）
    "data-analysis": () => loadDataAnalysis(),
    // v0.7.0：查询日志 tab 惰性加载（进入才首次请求 query_log/list，默认最近 100 条）
    "query-log": () => loadQueryLog(1),
};

// v0.5.0：每次进入 tab 都会触发的钩子（区别于上方仅首次的惰性加载）；
// 数据分析图表实例切 tab 不销毁只隐藏，重新可见时需 resize 对齐容器尺寸
const TAB_ENTER_HOOKS = {
    "data-analysis": () => enterDataAnalysis(),
};

// 统一激活某个子 tab：切换高亮、显示对应页面、首次进入触发惰性加载
function activateTab(name) {
    // v0.9.0：SQLite 后端下数据分析不可用（stats_service 未构造，stats/* 全 503），
    // 在激活入口拦截：仅提示，不切页也不发任何数据请求
    if (name === "data-analysis" && statsUnavailable()) {
        showToast("数据分析在 SQLite 存储模式下不可用（需要 MySQL 聚合性能）", "error");
        return;
    }
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
    if (TAB_ENTER_HOOKS[name]) TAB_ENTER_HOOKS[name]();
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
    bindDataAnalysisEvents(); // v0.5.0 数据分析（过滤栏 / 排行交互 / 推送设置表单）
    // 默认进入存储库分区并点亮高光；总结/人物分析数据延迟到进入对应分区时加载
    switchScope("storage");
    // v0.9.0 存储后端横幅：与首屏数据并行拉取，失败静默（不阻塞其余初始化）
    loadStorageInfo();
    await Promise.all([loadStatus(), loadGroups(), loadSettings(), loadDailyStats()]);
}

init();
