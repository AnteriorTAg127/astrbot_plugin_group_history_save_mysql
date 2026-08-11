// 存储库分区：状态 / 群管理 / 设置 / 每日统计 / 查询 / 查询日志（v0.7.0） / 清空（加减法验证）
import { bridge, showToast, el, confirmDialog } from "./common.js";

let currentPage = 1;
const pageSize = 50;

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

// 生成 HTML 通过 onclick 内联调用，必须挂载到 window（与拆包前一致）
window.toggleGroup = async function (groupId) {
    try {
        await bridge.apiPost("groups/toggle", { group_id: groupId });
        await loadGroups();
    } catch (e) {
        showToast("操作失败: " + e.message, "error");
    }
};

window.removeGroup = async function (groupId) {
    const ok = await confirmDialog(`确定要移除群 ${groupId} 吗？移除后将停止记录该群的消息。`);
    if (!ok) return;
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
        document.getElementById("backfillEnabled").checked = settings.backfill_enabled !== "false";
        document.getElementById("backfillHours").value = settings.backfill_hours || 12;
        document.getElementById("backfillRoundCap").value = settings.backfill_round_cap || 200;
        document.getElementById("backfillMaxRounds").value = settings.backfill_max_rounds || 5;
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
                <td>${escapeHtml(r.sender_name || "-")}</td>
                <td><span class="type-badge">${r.message_type}</span></td>
                <td class="content-cell" title="${escapeAttr(r.content || "")}">${escapeHtml(r.content || "")}</td>
                <td class="rel-cell">${renderRelations(r)}</td>
            </tr>`
            )
            .join("");
        // 给每条关联记录绑定点击反查弹层（事件委托，避免重复绑定）
        tbody.querySelectorAll(".rel-item").forEach((el) => {
            el.addEventListener("click", () => showRelatedMessage(el.dataset));
        });

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

// ========== 回复关联展示（v0.4.1） ==========
// 渲染本条消息的回复目标（点击查看被引用消息内容弹层）
// 数据来自后端 api_query 的 reply_message 关联字段；
// at_list（被 @ 的 QQ）仅存储、不反查展示（@ ID 无法可靠反查消息）
function renderRelations(r) {
    const reply = r.reply_message;
    if (reply && reply.sender_id) {
        return (
            `<span class="rel-item rel-reply" data-kind="回复" data-sender="${escapeAttr(reply.sender_id)}" data-name="${escapeAttr(reply.sender_name || "")}" data-content="${escapeAttr(reply.content || "")}" title="查看被回复的消息">↩️ 回复了 ${escapeHtml(reply.sender_name || reply.sender_id)}</span>`
        );
    }
    return '<span class="rel-none">-</span>';
}

// 点击回复关联 → 弹层展示被引用消息内容（纯 textContent 防 XSS）
function showRelatedMessage(d) {
    const mask = document.getElementById("relatedModal");
    const title = document.getElementById("relatedModalTitle");
    const body = document.getElementById("relatedModalBody");
    if (!mask || !body) return;
    const who = d.name || d.sender || "未知用户";
    title.textContent = `↩️ 回复了 ${who}`;
    body.innerHTML = "";
    body.appendChild(el("div", "rel-modal-meta", `发送者: ${d.sender}${d.name ? " · " + d.name : ""}`));
    body.appendChild(el("div", "rel-modal-content", d.content || "(无文本内容)"));
    mask.style.display = "flex";
}

function closeRelatedModal() {
    const mask = document.getElementById("relatedModal");
    if (mask) mask.style.display = "none";
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

// ========== 查询日志（v0.7.0） ==========
// 对外公共 API 的调用日志（core/db_config/query_log.py，内置 SQLite config.db），
// 端点 GET query_log/list；默认最近 100 条（后端 page_size 默认 100，上限 200），
// 分页 + 调用方/方法/时间筛选；保留天数经 query_log/settings 读写。
let queryLogPage = 1;
const queryLogPageSize = 100;
// 渲染序号：快速切换筛选/分页时丢弃过期响应，防止旧数据覆盖新界面（data-analysis renderSeq 范式）
let queryLogSeq = 0;

// 加载保留天数设置（进入 tab 首次请求时顺带填充；失败静默，不阻断日志列表）
async function loadQueryLogSettings() {
    try {
        const data = await bridge.apiGet("query_log/settings", { _t: Date.now() });
        const input = document.getElementById("qlRetentionDays");
        if (data && data.retention_days != null) input.value = data.retention_days;
    } catch (e) {
        document.getElementById("qlSettingsInfo").textContent = "读取设置失败: " + e.message;
    }
}

// 保存保留天数设置：夹取 [1, 3650]（后端兜底夹取），成功后提示并刷新列表
async function saveQueryLogSettings() {
    const input = document.getElementById("qlRetentionDays");
    const raw = parseInt(input.value, 10);
    if (Number.isNaN(raw) || raw < 1) {
        document.getElementById("qlSettingsInfo").textContent = "保留天数必须为 1–3650 的整数";
        return;
    }
    const info = document.getElementById("qlSettingsInfo");
    try {
        const data = await bridge.apiPost("query_log/settings/save", { retention_days: raw });
        if (data && data.success) {
            info.textContent = `已保存：日志保留 ${data.retention_days} 天（新日志写入时按 5% 概率顺带清理）`;
            loadQueryLog(queryLogPage); // 保留天数变化不影响列表，但刷新保持数据新鲜
        } else {
            info.textContent = "保存失败: " + (data && data.message ? data.message : "未知错误");
        }
    } catch (e) {
        info.textContent = "保存失败: " + e.message;
    }
}

async function loadQueryLog(page = 1) {
    queryLogPage = page;
    const seq = ++queryLogSeq;
    // _t 时间戳：破除浏览器对 GET 查询响应的缓存（同 doQuery 范式）
    const params = { page, page_size: queryLogPageSize, _t: Date.now() };

    const caller = document.getElementById("qlCaller").value.trim();
    const method = document.getElementById("qlMethod").value.trim();
    const timeStart = document.getElementById("qlTimeStart").value;
    const timeEnd = document.getElementById("qlTimeEnd").value;

    // 空串不传（后端空串归一 None）；datetime-local 无秒，起止补秒对齐整分钟
    if (caller) params.caller = caller;
    if (method) params.method = method;
    if (timeStart) params.time_start = timeStart.replace("T", " ") + ":00";
    if (timeEnd) params.time_end = timeEnd.replace("T", " ") + ":59";

    try {
        // v0.2 教训：响应顶层 data 键会被插件页桥接解包剥层，列表键 records 直接可用
        const data = await bridge.apiGet("query_log/list", params);
        if (seq !== queryLogSeq) return; // 已有更新的查询，丢弃过期结果
        // 首次进入（page=1）顺带加载保留天数设置（幂等，失败静默）
        if (page === 1) await loadQueryLogSettings();
        const total = data.total || 0;
        const rows = data.records || [];

        document.getElementById("qlInfo").textContent = `共 ${total} 条记录`;

        const table = document.getElementById("qlTable");
        const empty = document.getElementById("qlEmpty");
        const tbody = document.getElementById("qlTableBody");

        if (rows.length === 0) {
            table.style.display = "none";
            empty.style.display = "block";
            document.getElementById("qlPagination").style.display = "none";
            return;
        }

        empty.style.display = "none";
        table.style.display = "table";
        tbody.textContent = ""; // 清空旧行（textContent 而非 innerHTML，防注入一致）
        for (const r of rows) tbody.appendChild(renderQueryLogRow(r));

        const totalPages = Math.ceil(total / queryLogPageSize);
        const pagination = document.getElementById("qlPagination");
        pagination.style.display = totalPages > 1 ? "flex" : "none";
        document.getElementById("qlPageInfo").textContent = `${page} / ${totalPages}`;
        document.getElementById("qlPrevPage").disabled = page <= 1;
        document.getElementById("qlNextPage").disabled = page >= totalPages;
    } catch (e) {
        if (seq !== queryLogSeq) return;
        document.getElementById("qlInfo").textContent = "查询失败: " + e.message;
    }
}

// 参数 JSON 摘要：截断约 80 字符（title 属性带全文，悬浮可见）
function summarizeParams(raw) {
    if (raw == null || raw === "") return "-";
    const s = String(raw);
    return s.length > 80 ? s.slice(0, 80) + "…" : s;
}

// 渲染单行：全部动态文本走 textContent / title 属性，杜绝 HTML 注入（XSS 防护）
function renderQueryLogRow(r) {
    const tr = document.createElement("tr");

    const tdTime = document.createElement("td");
    tdTime.textContent = r.created_at || "-";

    const tdCaller = document.createElement("td");
    tdCaller.textContent = r.caller || "-";

    const tdMethod = document.createElement("td");
    tdMethod.textContent = r.method || "-";

    const paramsFull = r.params == null ? "" : String(r.params);
    const tdParams = document.createElement("td");
    tdParams.className = "ql-params-cell";
    tdParams.textContent = summarizeParams(paramsFull);
    tdParams.title = paramsFull; // 全文悬浮可见（title 由浏览器按纯文本渲染）

    const tdCount = document.createElement("td");
    tdCount.textContent = r.result_count ?? 0;

    const tdCost = document.createElement("td");
    tdCost.textContent = r.cost_ms != null ? `${r.cost_ms} ms` : "-";

    // 状态：后端 success 存 TINYINT(1)，序列化后可能是 1/0 或 true/false
    const ok = r.success === true || r.success === 1;
    const tdStatus = document.createElement("td");
    tdStatus.textContent = ok ? "✅" : "❌";
    tdStatus.className = ok ? "ql-status-ok" : "ql-status-fail";
    if (!ok && r.error_msg) tdStatus.title = String(r.error_msg); // 失败原因悬浮可见

    tr.append(tdTime, tdCaller, tdMethod, tdParams, tdCount, tdCost, tdStatus);
    return tr;
}

// ========== 事件绑定 ==========
function bindStorageEvents() {
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
        const hours = parseInt(document.getElementById("backfillHours").value);
        if (isNaN(hours) || hours < 1 || hours > 168) { showToast("补库时长需在 1–168 小时之间", "error"); return; }
        const roundCap = parseInt(document.getElementById("backfillRoundCap").value);
        if (isNaN(roundCap) || roundCap < 1 || roundCap > 5000) { showToast("单轮补库条数需在 1–5000 之间", "error"); return; }
        const maxRounds = parseInt(document.getElementById("backfillMaxRounds").value);
        if (isNaN(maxRounds) || maxRounds < 1 || maxRounds > 50) { showToast("补库最大轮数需在 1–50 之间", "error"); return; }
        try {
            await bridge.apiPost("settings/save", {
                image_retention_days: days,
                backfill_enabled: document.getElementById("backfillEnabled").checked,
                backfill_hours: hours,
                backfill_round_cap: roundCap,
                backfill_max_rounds: maxRounds,
            });
            showToast("设置已保存", "success");
        } catch (e) { showToast("保存失败: " + e.message, "error"); }
    });

    document.getElementById("cleanBtn").addEventListener("click", async () => {
        const ok = await confirmDialog("确定要清理过期图片记录吗？此操作不可撤销。");
        if (!ok) return;
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

    // 查询日志（v0.7.0）：筛选提交 / 重置 / 分页，输入框 Enter 快捷提交
    document.getElementById("qlQueryBtn").addEventListener("click", () => loadQueryLog(1));
    document.getElementById("qlSaveSettingsBtn").addEventListener("click", () => saveQueryLogSettings());
    document.getElementById("qlRetentionDays").addEventListener("keydown", (e) => {
        if (e.key === "Enter") saveQueryLogSettings();
    });
    document.getElementById("qlResetBtn").addEventListener("click", () => {
        for (const id of ["qlCaller", "qlMethod", "qlTimeStart", "qlTimeEnd"]) {
            document.getElementById(id).value = "";
        }
        loadQueryLog(1);
    });
    document.getElementById("qlPrevPage").addEventListener("click", () => {
        if (queryLogPage > 1) loadQueryLog(queryLogPage - 1);
    });
    document.getElementById("qlNextPage").addEventListener("click", () => loadQueryLog(queryLogPage + 1));
    for (const id of ["qlCaller", "qlMethod"]) {
        document.getElementById(id).addEventListener("keydown", (e) => {
            if (e.key === "Enter") loadQueryLog(1);
        });
    }

    // @ / 回复关联弹层关闭（关闭按钮 + 点击遮罩）
    const relatedMask = document.getElementById("relatedModal");
    if (relatedMask) {
        document.getElementById("relatedCloseBtn").addEventListener("click", closeRelatedModal);
        relatedMask.addEventListener("click", (e) => {
            if (e.target === relatedMask) closeRelatedModal();
        });
    }
}

export {
    loadStatus,
    loadGroups,
    loadSettings,
    loadDailyStats,
    doQuery,
    loadQueryLog,
    openPurgeModal,
    confirmPurge,
    bindStorageEvents,
};
