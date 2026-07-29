const bridge = window.AstrBotPluginPage;
await bridge.ready();

let currentPage = 1;
const pageSize = 50;

// ========== Toast ==========
function showToast(msg, type = "") {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = "toast show" + (type ? ` ${type}` : "");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.className = "toast"; }, 2600);
}

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
    bindEvents();
    bindSummaryEvents(); // v0.3 总结功能三 tab 事件绑定
    // 默认进入存储库分区并点亮高光；总结数据延迟到进入消息总结分区时加载
    switchScope("storage");
    await Promise.all([loadStatus(), loadGroups(), loadSettings(), loadDailyStats()]);
}

// ========== 状态 ==========
async function loadStatus() {
    const badge = document.getElementById("dbBadge");
    try {
        const data = await bridge.apiGet("status");
        const db = data.database || {};
        if (db.connected) {
            badge.textContent = `已连接 · ${db.latency_ms}ms`;
            badge.className = "db-badge online";
        } else {
            badge.textContent = "未连接";
            badge.className = "db-badge offline";
        }
        // 连接池信息
        const pool = db.pool || {};
        document.getElementById("poolUsed").textContent = pool.used ?? 0;
        document.getElementById("poolFree").textContent = pool.free ?? 0;
        document.getElementById("poolRange").textContent = `${pool.min_size ?? 1} ~ ${pool.max_size ?? 10}`;
        document.getElementById("poolCreated").textContent = pool.total_created ?? 0;
        document.getElementById("poolRecycled").textContent = pool.total_recycled ?? 0;

        const stats = data.stats || {};
        document.getElementById("todayMessages").textContent = stats.today_messages ?? 0;
        document.getElementById("todayImages").textContent = stats.today_images ?? 0;
        document.getElementById("totalMessages").textContent = formatNum(stats.total_messages ?? 0);
        document.getElementById("enabledGroups").textContent = data.enabled_groups ?? 0;
        document.getElementById("allModeToggle").checked = data.all_mode || false;
    } catch {
        badge.textContent = "请求失败";
        badge.className = "db-badge offline";
    }
}

function formatNum(n) {
    if (n >= 10000) return (n / 10000).toFixed(1) + "w";
    return String(n);
}

// ========== 群管理 ==========
async function loadGroups() {
    const container = document.getElementById("groupList");
    try {
        const data = await bridge.apiGet("groups");
        const groups = data.groups || [];
        document.getElementById("allModeToggle").checked = data.all_mode || false;

        if (groups.length === 0) {
            container.innerHTML = '<div class="loading">暂无配置的群，请添加</div>';
            return;
        }

        container.innerHTML = groups
            .map(
                (g) => `
            <div class="group-item">
                <div class="group-avatar">${String(g.group_id).slice(-4)}</div>
                <div class="group-info">
                    <div class="group-name">${g.group_id}</div>
                    <div class="group-time">添加于 ${g.created_at || "未知"}</div>
                </div>
                <div class="group-actions">
                    <label class="mini-toggle">
                        <input type="checkbox" ${g.enabled ? "checked" : ""}
                               onchange="toggleGroup(${g.group_id})">
                        <span class="mini-track"></span>
                    </label>
                    <button class="btn btn-ghost btn-sm" onclick="removeGroup(${g.group_id})">移除</button>
                </div>
            </div>`
            )
            .join("");
    } catch {
        container.innerHTML = '<div class="loading">加载失败</div>';
    }
}

window.toggleGroup = async function (groupId) {
    try {
        await bridge.apiPost("groups/toggle", { group_id: groupId });
        await loadGroups();
    } catch (e) {
        showToast("操作失败: " + e.message, "error");
    }
};

window.removeGroup = async function (groupId) {
    if (!confirm(`确定要移除群 ${groupId} 吗？`)) return;
    try {
        await bridge.apiPost("groups/remove", { group_id: groupId });
        showToast(`已移除群 ${groupId}`, "success");
        await loadGroups();
    } catch (e) {
        showToast("移除失败: " + e.message, "error");
    }
};

// ========== 设置 ==========
async function loadSettings() {
    try {
        const settings = await bridge.apiGet("settings");
        document.getElementById("retentionDays").value = settings.image_retention_days || 3;
    } catch { /* ignore */ }
}

// ========== 每日统计 ==========
async function loadDailyStats() {
    const container = document.getElementById("dailyStats");
    try {
        const data = await bridge.apiGet("stats/daily", { days: 7 });
        const stats = data.items || [];

        if (stats.length === 0) {
            container.innerHTML = '<div class="loading">暂无数据</div>';
            return;
        }

        const maxVal = Math.max(...stats.map((s) => s.messages + s.images), 1);

        container.innerHTML = stats
            .map((s) => {
                const total = s.messages + s.images;
                const pct = Math.max((total / maxVal) * 100, 1);
                const dateStr = s.date.slice(5); // MM-DD
                return `
                <div class="daily-row">
                    <span class="daily-date">${dateStr}</span>
                    <div class="daily-bar-wrap">
                        <div class="daily-bar" style="width:${pct}%"></div>
                    </div>
                    <span class="daily-count">${total}</span>
                </div>`;
            })
            .join("");
    } catch {
        container.innerHTML = '<div class="loading">加载失败</div>';
    }
}

// ========== 查询 ==========
async function doQuery(page = 1) {
    currentPage = page;
    // _t 时间戳：破除浏览器对 GET 查询响应的缓存，否则相同 URL 的 ajax 会返回旧的 total:0
    const params = { page, page_size: pageSize, _t: Date.now() };

    const groupId = document.getElementById("queryGroupId").value.trim();
    const senderId = document.getElementById("querySenderId").value.trim();
    const keyword = (document.getElementById("queryKeyword")?.value || "").trim();
    const timeStart = document.getElementById("queryTimeStart").value;
    const timeEnd = document.getElementById("queryTimeEnd").value;

    // v0.2 起群号/QQ 号为文本字段，原样透传（不再 parseInt，避免大数精度损失与 NaN 匹配不到）
    if (groupId) params.group_id = groupId;
    if (senderId) params.sender_id = senderId;
    if (keyword) params.keyword = keyword;
    if (timeStart) params.time_start = timeStart.replace("T", " ") + ":00";
    if (timeEnd) params.time_end = timeEnd.replace("T", " ") + ":59";

    try {
        const data = await bridge.apiGet("query", params);
        const total = data.total || 0;
        const rows = data.records || [];

        document.getElementById("queryInfo").textContent = `共 ${total} 条记录`;

        const table = document.getElementById("queryTable");
        const empty = document.getElementById("queryEmpty");
        const tbody = document.getElementById("queryTableBody");

        if (rows.length === 0) {
            table.style.display = "none";
            empty.style.display = "block";
            document.getElementById("pagination").style.display = "none";
            return;
        }

        empty.style.display = "none";
        table.style.display = "table";
        tbody.innerHTML = rows
            .map(
                (r) => `
            <tr>
                <td>${r.timestamp}</td>
                <td>${r.group_id}</td>
                <td>${r.sender_id}</td>
                <td>${r.sender_name || "-"}</td>
                <td><span class="type-badge">${r.message_type}</span></td>
                <td class="content-cell" title="${escapeAttr(r.content || "")}">${escapeHtml(r.content || "")}</td>
            </tr>`
            )
            .join("");

        const totalPages = Math.ceil(total / pageSize);
        const pagination = document.getElementById("pagination");
        pagination.style.display = totalPages > 1 ? "flex" : "none";
        document.getElementById("pageInfo").textContent = `${page} / ${totalPages}`;
        document.getElementById("prevPage").disabled = page <= 1;
        document.getElementById("nextPage").disabled = page >= totalPages;
    } catch (e) {
        document.getElementById("queryInfo").textContent = "查询失败: " + e.message;
    }
}

function escapeHtml(text) {
    const d = document.createElement("div");
    d.textContent = text;
    return d.innerHTML;
}

function escapeAttr(text) {
    return text.replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// ========== 清空所有数据（加减法验证） ==========
const purgeMask = document.getElementById("purgeModal");
let purgeChallengeId = null;

// 拉取新题目并更新弹窗题干（打开弹窗与验证失败后均复用），返回是否成功
async function refreshChallenge() {
    const questionEl = document.getElementById("purgeQuestion");
    questionEl.textContent = "加载中...";
    try {
        const data = await bridge.apiGet("purge/challenge");
        purgeChallengeId = data.challenge_id;
        questionEl.textContent = data.question;
        return true;
    } catch (e) {
        purgeChallengeId = null;
        questionEl.textContent = "题目加载失败";
        // 429 错误时 e.message 已包含"频繁"提示，直接展示
        showToast(e.message || "获取验证题目失败", "error");
        return false;
    }
}

async function openPurgeModal() {
    purgeMask.style.display = "flex";
    document.getElementById("purgeAnswer").value = "";
    document.getElementById("purgeConfirmBtn").disabled = false;
    if (await refreshChallenge()) {
        document.getElementById("purgeAnswer").focus();
    } else {
        closePurgeModal();
    }
}

function closePurgeModal() {
    purgeMask.style.display = "none";
    purgeChallengeId = null;
}

async function confirmPurge() {
    const answer = document.getElementById("purgeAnswer").value.trim();
    if (!answer) { showToast("请先输入答案", "error"); return; }
    const btn = document.getElementById("purgeConfirmBtn");
    btn.disabled = true;
    try {
        const data = await bridge.apiPost("purge", { challenge_id: purgeChallengeId, answer: Number(answer) });
        // TRUNCATE 路径不返回精确条数，统一提示"已清空全部数据"
        if (data.truncated) {
            showToast("已清空全部数据", "success");
        } else {
            showToast(`已清空：${data.deleted_messages} 条消息、${data.deleted_images} 条图片`, "success");
        }
        closePurgeModal();
        await loadStatus();
    } catch (e) {
        showToast(e.message || "清空失败", "error");
        // challenge 已被后端一次性消费（答错同样删除），需换新题重试
        await refreshChallenge();
        const input = document.getElementById("purgeAnswer");
        input.value = "";
        input.focus();
    } finally {
        btn.disabled = false;
    }
}

// ========== 事件绑定 ==========
function bindEvents() {
    document.getElementById("addGroupBtn").addEventListener("click", async () => {
        const input = document.getElementById("newGroupId");
        const gid = input.value.trim();
        if (!gid || isNaN(gid)) { showToast("请输入有效的群号", "error"); return; }
        try {
            await bridge.apiPost("groups/add", { group_id: parseInt(gid) });
            input.value = "";
            showToast(`已添加群 ${gid}`, "success");
            await loadGroups();
        } catch (e) { showToast("添加失败: " + e.message, "error"); }
    });

    document.getElementById("newGroupId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") document.getElementById("addGroupBtn").click();
    });

    document.getElementById("allModeToggle").addEventListener("change", async (e) => {
        try {
            await bridge.apiPost("settings/save", { all_mode: e.target.checked });
            showToast(e.target.checked ? "已开启 ALL 模式" : "已关闭 ALL 模式", "success");
        } catch (err) {
            showToast("保存失败: " + err.message, "error");
            e.target.checked = !e.target.checked;
        }
    });

    document.getElementById("saveSettingsBtn").addEventListener("click", async () => {
        const days = parseInt(document.getElementById("retentionDays").value);
        if (isNaN(days) || days < 1) { showToast("请输入有效天数", "error"); return; }
        try {
            await bridge.apiPost("settings/save", { image_retention_days: days });
            showToast("设置已保存", "success");
        } catch (e) { showToast("保存失败: " + e.message, "error"); }
    });

    document.getElementById("cleanBtn").addEventListener("click", async () => {
        if (!confirm("确定要清理过期图片记录吗？此操作不可撤销。")) return;
        try {
            const data = await bridge.apiPost("clean", {});
            showToast(`已清理 ${data.deleted} 条图片记录`, "success");
            await loadStatus();
        } catch (e) { showToast("清理失败: " + e.message, "error"); }
    });

    // 清空所有数据（加减法验证弹窗）
    document.getElementById("purgeBtn").addEventListener("click", () => openPurgeModal());
    document.getElementById("purgeCancelBtn").addEventListener("click", closePurgeModal);
    purgeMask.addEventListener("click", (e) => {
        if (e.target === purgeMask) closePurgeModal();
    });
    document.getElementById("purgeConfirmBtn").addEventListener("click", () => confirmPurge());
    document.getElementById("purgeAnswer").addEventListener("keydown", (e) => {
        if (e.key === "Enter") confirmPurge();
    });

    document.getElementById("queryBtn").addEventListener("click", () => doQuery(1));
    document.getElementById("prevPage").addEventListener("click", () => {
        if (currentPage > 1) doQuery(currentPage - 1);
    });
    document.getElementById("nextPage").addEventListener("click", () => doQuery(currentPage + 1));
}

// ========== v0.3 总结功能：共用工具 ==========

// 通用节点构建器：所有动态文本一律走 textContent，杜绝 HTML 注入（XSS 防护）
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

// ========== v0.3 Tab 1：总结设置（15 项配置分组表单） ==========

// 15 项总结配置元数据：分组 / 展示名 / 说明 / 控件类型。
// kind 与后端 SUMMARY_TYPES（bool/int/float/list/str）一一对应，
// 请求封装沿用 bridge.apiGet/apiPost（路径相对插件 API 前缀，响应已由桥接解包）
const SUMMARY_FIELD_META = [
    // —— 基础与白名单 ——
    { key: "summary_enabled", group: "基础与白名单", label: "功能总开关", desc: "关闭后总结指令回复「功能未启用」", kind: "bool" },
    { key: "summary_whitelist_mode", group: "基础与白名单", label: "白名单模式", desc: "whitelist=仅白名单群可用；all=所有群可用", kind: "select", options: [["whitelist", "仅白名单群（whitelist）"], ["all", "所有群可用（all）"]] },
    { key: "summary_group_whitelist", group: "基础与白名单", label: "白名单群列表", desc: "群号以逗号分隔输入；mode=all 时忽略此项", kind: "whitelist" },
    { key: "summary_user_cooldown", group: "基础与白名单", label: "用户触发冷却", desc: "同一用户两次触发的最小间隔", kind: "int", unit: "秒", min: 0 },
    { key: "summary_group_cooldown", group: "基础与白名单", label: "群总结冷却", desc: "同一群两次总结的最小间隔", kind: "int", unit: "秒", min: 0 },
    // —— 参数上限 ——
    { key: "summary_max_count", group: "参数上限", label: "最大总结数量", desc: "/消息总结 数量参数上限，超出拒绝", kind: "int", unit: "条", min: 1 },
    { key: "summary_max_hours", group: "参数上限", label: "最大时间跨度", desc: "/消息总结时间 时间跨度上限（小时），超出拒绝", kind: "int", unit: "小时", min: 1 },
    { key: "summary_onebot_max_fetch", group: "参数上限", label: "OneBot 最大拉取", desc: "单次从协议端最多拉取的历史消息条数", kind: "int", unit: "条", min: 1 },
    { key: "summary_min_mysql_ratio", group: "参数上限", label: "MySQL 补齐阈值", desc: "数量模式：MySQL 实得/请求 < 此值才拉 OneBot 补齐（0~1）", kind: "ratio" },
    { key: "summary_gap_tolerance_minutes", group: "参数上限", label: "缺口容忍分钟数", desc: "时间模式的缺口容忍分钟数", kind: "int", unit: "分钟", min: 0 },
    // —— 总结行为 ——
    { key: "summary_provider_id", group: "总结行为", label: "总结专用 LLM 提供商", desc: "留空时回退使用当前会话的 LLM 提供商", kind: "provider" },
    { key: "summary_prompt", group: "总结行为", label: "提示词模板", desc: "占位符：{stats} {messages} {time_range} {group_id} {format_constraint}", kind: "prompt" },
    { key: "summary_output_mode", group: "总结行为", label: "输出形态", desc: "forward=合并转发（剥 Markdown）；image=文转图（保留 Markdown）", kind: "select", options: [["forward", "合并转发（forward）"], ["image", "文转图（image）"]] },
    { key: "summary_rank_top_n", group: "总结行为", label: "活跃排行条数", desc: "活跃排行展示条数", kind: "int", unit: "条", min: 0 },
    // —— 存储 ——
    { key: "summary_retention_days", group: "存储", label: "总结保留天数", desc: "总结 JSON 保留天数，每日定时清理过期文件", kind: "int", unit: "天", min: 1 },
];

let summaryProviders = []; // LLM 提供商列表（可能为空数组 → 下拉仅含回退项）

async function loadSummarySettings() {
    const container = document.getElementById("summarySettingsForm");
    try {
        // 设置与 providers 并行拉取；providers 失败不阻塞表单渲染（降级为空列表）
        const [data, provData] = await Promise.all([
            bridge.apiGet("summary/settings"),
            bridge.apiGet("summary/providers").catch(() => ({ providers: [] })),
        ]);
        summaryProviders = provData.providers || [];
        renderSummaryForm(data.settings || {});
    } catch (e) {
        container.innerHTML = "";
        container.appendChild(el("div", "loading", "总结设置加载失败"));
        showToast("加载总结设置失败: " + (e.message || "未知错误"), "error");
    }
}

function renderSummaryForm(settings) {
    const container = document.getElementById("summarySettingsForm");
    container.innerHTML = "";
    let currentGroup = null;
    let groupEl = null;
    for (const meta of SUMMARY_FIELD_META) {
        if (meta.group !== currentGroup) {
            currentGroup = meta.group;
            groupEl = el("div", "summary-group");
            groupEl.appendChild(el("div", "summary-group-title", meta.group));
            container.appendChild(groupEl);
        }
        groupEl.appendChild(buildSummaryItem(meta, settings[meta.key] ?? ""));
    }
}

function buildSummaryItem(meta, rawValue) {
    const stacked = meta.kind === "prompt" || meta.kind === "whitelist";
    const item = el("div", "s-item" + (stacked ? " s-item-stack" : ""));
    const info = el("div", "setting-info");
    info.appendChild(el("div", "setting-name", meta.label));
    info.appendChild(el("div", "setting-desc", meta.desc));
    item.appendChild(info);

    const control = el("div", "setting-control");
    switch (meta.kind) {
        case "bool": {
            const label = el("label", "all-mode-switch");
            const input = document.createElement("input");
            input.type = "checkbox";
            input.dataset.key = meta.key;
            input.checked = String(rawValue).toLowerCase() === "true";
            const track = el("span", "switch-track");
            track.appendChild(el("span", "switch-thumb"));
            label.appendChild(input);
            label.appendChild(track);
            control.appendChild(label);
            break;
        }
        case "int": {
            const input = document.createElement("input");
            input.type = "number";
            input.className = "input input-sm";
            input.min = String(meta.min ?? 0);
            input.step = "1";
            input.dataset.key = meta.key;
            input.value = String(rawValue);
            control.appendChild(input);
            if (meta.unit) control.appendChild(el("span", "unit", meta.unit));
            break;
        }
        case "ratio": {
            const input = document.createElement("input");
            input.type = "number";
            input.className = "input input-sm";
            input.min = "0";
            input.max = "1";
            input.step = "0.05";
            input.dataset.key = meta.key;
            input.value = String(rawValue);
            control.appendChild(input);
            break;
        }
        case "select": {
            const select = document.createElement("select");
            select.className = "input input-select";
            select.dataset.key = meta.key;
            for (const [val, text] of meta.options) {
                const opt = el("option", null, text);
                opt.value = val;
                select.appendChild(opt);
            }
            select.value = String(rawValue);
            control.appendChild(select);
            break;
        }
        case "provider": {
            const select = document.createElement("select");
            select.className = "input input-select provider-select";
            select.dataset.key = meta.key;
            // 首项固定为空值「回退会话 provider」；providers 为空数组时下拉仅含此项
            const fallback = el("option", null, "（回退会话 provider）");
            fallback.value = "";
            select.appendChild(fallback);
            for (const prov of summaryProviders) {
                const opt = el("option", null, prov.name || prov.id);
                opt.value = prov.id;
                select.appendChild(opt);
            }
            // 当前值不在列表中（provider 已删除）→ 以禁用项保留原值，避免静默清空
            if (rawValue && ![...select.options].some((o) => o.value === String(rawValue))) {
                const ghost = el("option", null, `${rawValue}（已失效）`);
                ghost.value = String(rawValue);
                ghost.disabled = true;
                select.appendChild(ghost);
            }
            select.value = String(rawValue ?? "");
            control.appendChild(select);
            break;
        }
        case "whitelist": {
            control.className = "setting-control control-wide";
            const input = document.createElement("input");
            input.type = "text";
            input.className = "input";
            input.placeholder = "多个群号用逗号分隔，如 123456, 654321";
            input.dataset.key = meta.key;
            const conv = whitelistJsonToText(rawValue);
            input.value = conv.text;
            if (!conv.ok) showToast("白名单格式异常（应为 JSON 数组），已展示原始值，请修正后保存", "error");
            control.appendChild(input);
            break;
        }
        case "prompt": {
            control.className = "setting-control control-wide";
            const textarea = document.createElement("textarea");
            textarea.className = "input prompt-textarea";
            textarea.dataset.key = meta.key;
            textarea.spellcheck = false;
            textarea.value = String(rawValue ?? "");
            control.appendChild(textarea);
            const btnRow = el("div", "prompt-actions");
            const resetBtn = el("button", "btn btn-ghost btn-sm", "恢复默认模板");
            resetBtn.type = "button";
            resetBtn.addEventListener("click", resetSummaryPrompt);
            btnRow.appendChild(resetBtn);
            control.appendChild(btnRow);
            break;
        }
    }
    item.appendChild(control);
    return item;
}

// 用接口返回的全量 settings 回填表单（保存/重置成功后复用）
function fillSummaryForm(settings) {
    for (const meta of SUMMARY_FIELD_META) {
        if (!(meta.key in settings)) continue;
        const ctrl = document.querySelector(`#summarySettingsForm [data-key="${meta.key}"]`);
        if (!ctrl) continue;
        const raw = settings[meta.key];
        if (meta.kind === "bool") ctrl.checked = String(raw).toLowerCase() === "true";
        else if (meta.kind === "whitelist") ctrl.value = whitelistJsonToText(raw).text;
        else ctrl.value = String(raw ?? "");
    }
}

// 收集全部 15 项组装 save 载荷；前端基础校验失败返回 null（后端 400 为最终裁决）
function collectSummarySettings() {
    const settings = {};
    for (const meta of SUMMARY_FIELD_META) {
        const ctrl = document.querySelector(`#summarySettingsForm [data-key="${meta.key}"]`);
        if (!ctrl) continue;
        switch (meta.kind) {
            case "bool":
                settings[meta.key] = ctrl.checked;
                break;
            case "int": {
                const raw = ctrl.value.trim();
                if (!/^\d+$/.test(raw)) {
                    showToast(`「${meta.label}」需为非负整数`, "error");
                    return null;
                }
                const v = parseInt(raw, 10);
                if (v < (meta.min ?? 0)) {
                    showToast(`「${meta.label}」不能小于 ${meta.min}`, "error");
                    return null;
                }
                settings[meta.key] = v;
                break;
            }
            case "ratio": {
                const v = parseFloat(ctrl.value);
                if (isNaN(v) || v < 0 || v > 1) {
                    showToast(`「${meta.label}」需为 0~1 之间的小数`, "error");
                    return null;
                }
                settings[meta.key] = v;
                break;
            }
            case "whitelist":
                try {
                    settings[meta.key] = whitelistTextToArray(ctrl.value);
                } catch (err) {
                    showToast(err.message, "error");
                    return null;
                }
                break;
            default:
                settings[meta.key] = ctrl.value;
        }
    }
    return settings;
}

async function saveSummarySettings() {
    const settings = collectSummarySettings();
    if (!settings) return;
    const btn = document.getElementById("saveSummaryBtn");
    btn.disabled = true;
    try {
        const data = await bridge.apiPost("summary/settings/save", { settings });
        fillSummaryForm(data.settings || settings);
        showToast("总结设置已保存", "success");
    } catch (e) {
        showToast("保存失败: " + (e.message || "未知错误"), "error");
    } finally {
        btn.disabled = false;
    }
}

async function resetSummaryAll() {
    if (!confirm("确定将全部 15 项总结设置恢复为默认值吗？此操作不可撤销。")) return;
    try {
        // 省略 keys = 全部重置（后端约定）
        const data = await bridge.apiPost("summary/settings/reset", {});
        fillSummaryForm(data.settings || {});
        showToast("已恢复全部默认设置", "success");
    } catch (e) {
        showToast("重置失败: " + (e.message || "未知错误"), "error");
    }
}

async function resetSummaryPrompt() {
    try {
        const data = await bridge.apiPost("summary/settings/reset", { keys: ["summary_prompt"] });
        const ctrl = document.querySelector('#summarySettingsForm [data-key="summary_prompt"]');
        if (ctrl && data.settings) ctrl.value = data.settings.summary_prompt ?? "";
        showToast("已恢复默认提示词模板", "success");
    } catch (e) {
        showToast("恢复默认模板失败: " + (e.message || "未知错误"), "error");
    }
}

// 群白名单互转：JSON 数组字符串 ↔ 「群号1, 群号2」逗号分隔文本
function whitelistJsonToText(value) {
    const s = String(value ?? "").trim();
    if (!s) return { ok: true, text: "" };
    try {
        const arr = JSON.parse(s);
        if (!Array.isArray(arr)) return { ok: false, text: s };
        return { ok: true, text: arr.map(String).join(", ") };
    } catch {
        return { ok: false, text: s };
    }
}

function whitelistTextToArray(text) {
    return String(text)
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean)
        .map((s) => {
            if (!/^\d+$/.test(s)) throw new Error(`白名单包含非法群号：「${s}」（仅允许纯数字）`);
            return s;
        });
}

// ========== v0.3 Tab 2：忽略管理（每群忽略发送者） ==========

let currentIgnoreGroup = null; // 当前正在查看的群号（字符串）

async function loadIgnoreGroups() {
    const container = document.getElementById("ignoreGroupChips");
    try {
        const data = await bridge.apiGet("summary/ignore/groups");
        const groups = data.groups || [];
        container.innerHTML = "";
        if (groups.length === 0) {
            container.appendChild(el("div", "chips-empty", "暂无配置忽略名单的群"));
            return;
        }
        for (const gid of groups) {
            const chip = el("button", "chip", String(gid));
            chip.type = "button";
            chip.dataset.gid = String(gid);
            chip.addEventListener("click", () => {
                document.getElementById("ignoreGroupId").value = String(gid);
                loadIgnoreList(String(gid));
            });
            container.appendChild(chip);
        }
        markActiveIgnoreChip();
    } catch (e) {
        container.innerHTML = "";
        container.appendChild(el("div", "chips-empty", "加载失败: " + (e.message || "未知错误")));
    }
}

function markActiveIgnoreChip() {
    document.querySelectorAll("#ignoreGroupChips .chip").forEach((c) => {
        c.classList.toggle("active", c.dataset.gid === currentIgnoreGroup);
    });
}

async function loadIgnoreList(groupId) {
    if (!/^\d+$/.test(groupId)) {
        showToast("请输入有效的群号（纯数字）", "error");
        return;
    }
    currentIgnoreGroup = groupId;
    markActiveIgnoreChip();
    document.getElementById("ignoreListCard").style.display = "block";
    document.getElementById("ignoreListTitle").textContent = `忽略名单 · 群 ${groupId}`;
    const table = document.getElementById("ignoreTable");
    const empty = document.getElementById("ignoreEmpty");
    const emptyText = empty.querySelector("p");
    table.style.display = "none";
    empty.style.display = "block";
    emptyText.textContent = "加载中...";
    try {
        const data = await bridge.apiGet("summary/ignore", { group_id: groupId });
        const senders = data.senders || [];
        const tbody = document.getElementById("ignoreTableBody");
        tbody.innerHTML = "";
        if (senders.length === 0) {
            emptyText.textContent = "该群暂无忽略的发送者";
            return;
        }
        empty.style.display = "none";
        table.style.display = "table";
        for (const s of senders) {
            const tr = document.createElement("tr");
            tr.appendChild(el("td", null, String(s.sender_id)));
            tr.appendChild(el("td", null, String(s.created_at || "-")));
            const tdAct = document.createElement("td");
            const btn = el("button", "btn btn-ghost btn-sm", "移除");
            btn.type = "button";
            btn.addEventListener("click", () => removeIgnoreSender(groupId, String(s.sender_id)));
            tdAct.appendChild(btn);
            tr.appendChild(tdAct);
            tbody.appendChild(tr);
        }
    } catch (e) {
        emptyText.textContent = "加载失败";
        showToast("加载忽略名单失败: " + (e.message || "未知错误"), "error");
    }
}

async function addIgnoreSender() {
    if (!currentIgnoreGroup) {
        showToast("请先输入群号并点击「加载」", "error");
        return;
    }
    const input = document.getElementById("ignoreSenderId");
    const senderId = input.value.trim();
    if (!/^\d+$/.test(senderId)) {
        showToast("请输入有效的 QQ 号（纯数字）", "error");
        return;
    }
    try {
        await bridge.apiPost("summary/ignore/add", { group_id: currentIgnoreGroup, sender_id: senderId });
        input.value = "";
        showToast(`已将 ${senderId} 加入忽略名单`, "success");
        // 刷新名单 + 群芯片列表（新群首次添加时出现在快捷区）
        await Promise.all([loadIgnoreList(currentIgnoreGroup), loadIgnoreGroups()]);
    } catch (e) {
        // 409 → 后端消息「该发送者已在忽略名单中」，直接展示
        showToast(e.message || "添加失败", "error");
    }
}

async function removeIgnoreSender(groupId, senderId) {
    try {
        await bridge.apiPost("summary/ignore/remove", { group_id: groupId, sender_id: senderId });
        showToast(`已将 ${senderId} 移出忽略名单`, "success");
        await Promise.all([loadIgnoreList(groupId), loadIgnoreGroups()]);
    } catch (e) {
        // 404 → 后端消息「该发送者不在忽略名单中」；刷新名单以同步真实状态
        showToast(e.message || "移除失败", "error");
        await loadIgnoreList(groupId);
    }
}

// ========== v0.3 Tab 3：历史总结（按群浏览已存总结） ==========

const HISTORY_PAGE_SIZE = 20;
let historyPage = 1;

// 数据源展示名映射（与 formatter._SOURCE_NAMES 一致）
const SOURCE_NAMES = { mysql: "MySQL", onebot: "OneBot" };

function formatSources(sources) {
    if (!sources || typeof sources !== "object") return "-";
    const parts = Object.entries(sources).map(([k, v]) => `${SOURCE_NAMES[k] || k} ${v}`);
    return parts.length ? parts.join(" + ") : "-";
}

function formatTimeRange(start, end) {
    if (!start && !end) return "-";
    if (!start || !end) return String(start || end);
    const s = String(start);
    const e = String(end);
    const sameDay = s.slice(0, 10) === e.slice(0, 10);
    // "YYYY-MM-DD HH:MM:SS" → 同日 "MM-DD HH:MM ~ HH:MM"，跨日 "MM-DD HH:MM ~ MM-DD HH:MM"
    return `${s.slice(5, 16)} ~ ${sameDay ? e.slice(11, 16) : e.slice(5, 16)}`;
}

async function loadSummaryHistory(page = 1) {
    historyPage = page;
    const groupId = document.getElementById("historyGroupId").value.trim();
    // _t 时间戳破浏览器 GET 缓存（沿用 doQuery 同款做法）
    const params = { page, page_size: HISTORY_PAGE_SIZE, _t: Date.now() };
    if (groupId) params.group_id = groupId;
    const info = document.getElementById("historyInfo");
    try {
        const data = await bridge.apiGet("summary/history", params);
        const total = data.total || 0;
        const items = data.items || [];
        info.textContent = `共 ${total} 条总结记录`;
        const table = document.getElementById("historyTable");
        const empty = document.getElementById("historyEmpty");
        const tbody = document.getElementById("historyTableBody");
        tbody.innerHTML = "";
        if (items.length === 0) {
            table.style.display = "none";
            empty.style.display = "block";
            document.getElementById("historyPagination").style.display = "none";
            return;
        }
        empty.style.display = "none";
        table.style.display = "table";
        for (const it of items) {
            const tr = document.createElement("tr");
            tr.appendChild(el("td", null, String(it.generated_at || "-")));
            tr.appendChild(el("td", null, String(it.group_id)));
            tr.appendChild(el("td", null, String(it.scope_desc || "-")));
            tr.appendChild(el("td", null, String(it.provider_id || "会话回退")));
            tr.appendChild(el("td", null, String(it.messages_used ?? 0)));
            const tdAct = document.createElement("td");
            const btn = el("button", "btn btn-ghost btn-sm", "详情");
            btn.type = "button";
            btn.addEventListener("click", () => openSummaryDetail(String(it.group_id), String(it.filename)));
            tdAct.appendChild(btn);
            tr.appendChild(tdAct);
            tbody.appendChild(tr);
        }
        const totalPages = Math.max(1, Math.ceil(total / HISTORY_PAGE_SIZE));
        const cur = data.page || page;
        document.getElementById("historyPagination").style.display = totalPages > 1 ? "flex" : "none";
        document.getElementById("historyPageInfo").textContent = `${cur} / ${totalPages}`;
        document.getElementById("historyPrevBtn").disabled = cur <= 1;
        document.getElementById("historyNextBtn").disabled = cur >= totalPages;
    } catch (e) {
        info.textContent = "加载失败: " + (e.message || "未知错误");
        showToast("加载历史总结失败: " + (e.message || "未知错误"), "error");
    }
}

function openSummaryDetail(groupId, filename) {
    const mask = document.getElementById("historyDetailModal");
    const body = document.getElementById("historyDetailBody");
    document.getElementById("historyDetailTitle").textContent = `📄 总结详情 · 群 ${groupId}`;
    mask.style.display = "flex";
    body.innerHTML = "";
    body.appendChild(el("div", "loading", "加载中..."));
    bridge
        .apiGet("summary/history/detail", { group_id: groupId, filename })
        .then((data) => renderSummaryDetail(body, data.detail || {}))
        .catch((e) => {
            // 404 → 后端消息「总结记录不存在」；记录可能已被定时清理
            closeSummaryDetail();
            showToast(e.message || "总结记录不存在或已被清理", "error");
        });
}

function closeSummaryDetail() {
    document.getElementById("historyDetailModal").style.display = "none";
}

// 详情渲染：全部文本经 el()/textContent/pre.textContent 纯文本插入，防 XSS
function renderSummaryDetail(container, detail) {
    container.innerHTML = "";

    // 元信息行：生成时间 / 范围 / provider
    const metaLine = el("div", "detail-meta");
    metaLine.appendChild(el("span", null, `🕐 ${detail.generated_at || "-"}`));
    metaLine.appendChild(el("span", null, `📐 ${detail.scope_desc || "-"}`));
    metaLine.appendChild(el("span", null, `🤖 ${detail.provider_id || "会话回退"}`));
    container.appendChild(metaLine);

    // 统计块
    const stats = detail.stats || {};
    const statsGrid = el("div", "detail-stats");
    statsGrid.appendChild(detailStatTile(String(stats.total ?? 0), "消息总数"));
    statsGrid.appendChild(detailStatTile(String(stats.participant_count ?? 0), "参与者"));
    statsGrid.appendChild(detailStatTile(formatTimeRange(stats.time_start, stats.time_end), "时间跨度"));
    statsGrid.appendChild(detailStatTile(formatSources(detail.sources), "数据源构成"));
    statsGrid.appendChild(detailStatTile(String(detail.messages_used ?? 0), "送入 LLM"));
    container.appendChild(statsGrid);

    // 截断标记
    if (stats.truncated) {
        container.appendChild(el("div", "detail-truncated", "⚠️ 消息过多已被截断，统计基于截断前全量"));
    }

    // 活跃排行 Top N（[sender_id, sender_name, count]）
    const topSenders = Array.isArray(stats.top_senders) ? stats.top_senders : [];
    if (topSenders.length > 0) {
        const rankWrap = el("div", "detail-rank");
        rankWrap.appendChild(el("div", "detail-subtitle", "🏆 活跃排行"));
        const ol = el("ol", "rank-list");
        for (const row of topSenders) {
            const sid = row?.[0] ?? "";
            const sname = row?.[1] ?? "";
            const count = row?.[2] ?? 0;
            const li = el("li", "rank-item");
            li.appendChild(el("span", "rank-name", String(sname || sid || "未知用户")));
            if (sname && sid) li.appendChild(el("span", "rank-id", String(sid)));
            li.appendChild(el("span", "rank-count", `${count} 条`));
            ol.appendChild(li);
        }
        rankWrap.appendChild(ol);
        container.appendChild(rankWrap);
    }

    // 各板块（[标题, 内容]，内容按换行渲染：white-space: pre-wrap + textContent）
    const sections = Array.isArray(detail.sections) ? detail.sections : [];
    if (sections.length > 0) {
        const secs = el("div", "detail-sections");
        for (const sec of sections) {
            const block = el("div", "detail-section");
            block.appendChild(el("div", "detail-section-title", String(sec?.[0] ?? "")));
            block.appendChild(el("div", "detail-section-content", String(sec?.[1] ?? "")));
            secs.appendChild(block);
        }
        container.appendChild(secs);
    }

    // 可折叠的 LLM 原始输出（textContent 赋值，绝不 innerHTML）
    if (detail.raw_llm_text) {
        const fold = document.createElement("details");
        fold.className = "detail-raw";
        fold.appendChild(el("summary", null, "查看 LLM 原始输出"));
        const pre = el("pre", "detail-raw-text");
        pre.textContent = String(detail.raw_llm_text);
        fold.appendChild(pre);
        container.appendChild(fold);
    }
}

function detailStatTile(value, label) {
    const tile = el("div", "detail-stat-tile");
    tile.appendChild(el("div", "detail-stat-value", value));
    tile.appendChild(el("div", "detail-stat-label", label));
    return tile;
}

// ========== v0.3 事件绑定 ==========
function bindSummaryEvents() {
    // 总结设置
    document.getElementById("saveSummaryBtn").addEventListener("click", saveSummarySettings);
    document.getElementById("resetSummaryAllBtn").addEventListener("click", resetSummaryAll);

    // 忽略管理
    document.getElementById("ignoreLoadBtn").addEventListener("click", () => {
        loadIgnoreList(document.getElementById("ignoreGroupId").value.trim());
    });
    document.getElementById("ignoreGroupId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") loadIgnoreList(e.target.value.trim());
    });
    document.getElementById("ignoreAddBtn").addEventListener("click", addIgnoreSender);
    document.getElementById("ignoreSenderId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") addIgnoreSender();
    });

    // 历史总结
    document.getElementById("historyRefreshBtn").addEventListener("click", () => loadSummaryHistory(1));
    document.getElementById("historyGroupId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") loadSummaryHistory(1);
    });
    document.getElementById("historyPrevBtn").addEventListener("click", () => {
        if (historyPage > 1) loadSummaryHistory(historyPage - 1);
    });
    document.getElementById("historyNextBtn").addEventListener("click", () => loadSummaryHistory(historyPage + 1));

    // 详情弹窗：点击遮罩或「关闭」按钮关闭
    const detailMask = document.getElementById("historyDetailModal");
    detailMask.addEventListener("click", (e) => {
        if (e.target === detailMask) closeSummaryDetail();
    });
    document.getElementById("historyDetailCloseBtn").addEventListener("click", closeSummaryDetail);
}

init();
