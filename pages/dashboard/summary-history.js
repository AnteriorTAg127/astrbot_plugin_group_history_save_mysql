// 历史总结分区：按群浏览已存总结记录（分页表格 + 详情入口）
import { bridge, showToast, el } from "./common.js";
import { openSummaryDetail, bindSummaryDetailEvents } from "./summary-detail.js";

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

// ========== 事件绑定：历史总结 ==========
function bindHistoryEvents() {
    document.getElementById("historyRefreshBtn").addEventListener("click", () => loadSummaryHistory(1));
    document.getElementById("historyGroupId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") loadSummaryHistory(1);
    });
    document.getElementById("historyPrevBtn").addEventListener("click", () => {
        if (historyPage > 1) loadSummaryHistory(historyPage - 1);
    });
    document.getElementById("historyNextBtn").addEventListener("click", () => loadSummaryHistory(historyPage + 1));

    bindSummaryDetailEvents();
}

export { loadSummaryHistory, bindHistoryEvents };
