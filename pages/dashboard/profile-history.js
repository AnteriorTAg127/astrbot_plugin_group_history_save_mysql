// 人物分析 · 历史分析 tab（v0.4.0）
// 分页列表 GET profile/history（目标昵称/QQ/范围/时间/provider）
// 详情弹层 GET profile/history/detail?filename=（复用 profile-launch.js 的结果渲染器，
// 含活动图表 + 板块 Markdown + 免责声明，与 summary-detail 渲染范式一致）
// 删除走 PRD §3.8 的 profile/history 端点（二次确认）；桥接仅暴露 apiGet/apiPost，
// 无 apiDelete，故优先尝试 apiDelete（向前兼容），回退 apiPost 同路径
import { bridge, showToast, el, confirmDialog } from "./common.js";
import { renderProfileResult, disposeCharts } from "./profile-launch.js";
import { exportSectionAsPng } from "./capture.js";

const PROFILE_HISTORY_PAGE_SIZE = 15;
let profileHistoryPage = 1;
let profileHistoryLoaded = false; // 首次进入 tab 才请求（惰性加载由 app.js TAB_LAZY_LOAD 触发）

async function loadProfileHistory(page = 1) {
    profileHistoryPage = page;
    const info = document.getElementById("profileHistoryInfo");
    try {
        // _t 时间戳破浏览器 GET 缓存（沿用 summary-history 同款做法）
        const data = await bridge.apiGet("profile/history", {
            page,
            page_size: PROFILE_HISTORY_PAGE_SIZE,
            _t: Date.now(),
        });
        profileHistoryLoaded = true;
        const total = data.total || 0;
        // 顶层列表键按 K 端点契约为 profiles（避撞桥接解包），防御性兼容 records
        const items = data.profiles || data.records || [];
        info.textContent = `共 ${total} 条人物分析记录`;

        const table = document.getElementById("profileHistoryTable");
        const empty = document.getElementById("profileHistoryEmpty");
        const tbody = document.getElementById("profileHistoryTableBody");
        tbody.innerHTML = "";

        if (items.length === 0) {
            table.style.display = "none";
            empty.style.display = "block";
            document.getElementById("profileHistoryPagination").style.display = "none";
            // 删除导致本页清空且不在首页 → 自动回退上一页
            if (page > 1 && profileHistoryLoaded) loadProfileHistory(page - 1);
            return;
        }
        empty.style.display = "none";
        table.style.display = "table";

        for (const it of items) {
            const tr = document.createElement("tr");
            const filename = String(it.filename || "");
            tr.appendChild(el("td", null, String(it.target_name || it.sender_id || "未知用户")));
            tr.appendChild(el("td", "td-mono", String(it.sender_id || "-")));
            tr.appendChild(el("td", null, String(it.scope_desc || (it.scope === "all" ? "全局" : "单群"))));
            tr.appendChild(el("td", null, String(it.created_at || "-")));
            tr.appendChild(el("td", null, String(it.provider_id || "会话回退")));
            const tdAct = document.createElement("td");
            const detailBtn = el("button", "btn btn-ghost btn-sm", "详情");
            detailBtn.type = "button";
            detailBtn.addEventListener("click", () => openProfileDetail(filename));
            tdAct.appendChild(detailBtn);
            const delBtn = el("button", "btn btn-ghost btn-sm profile-del-btn", "删除");
            delBtn.type = "button";
            delBtn.addEventListener("click", () => deleteProfileItem(filename, it));
            tdAct.appendChild(delBtn);
            tr.appendChild(tdAct);
            tbody.appendChild(tr);
        }

        const totalPages = Math.max(1, Math.ceil(total / PROFILE_HISTORY_PAGE_SIZE));
        const cur = data.page || page;
        document.getElementById("profileHistoryPagination").style.display = totalPages > 1 ? "flex" : "none";
        document.getElementById("profileHistoryPageInfo").textContent = `${cur} / ${totalPages}`;
        document.getElementById("profileHistoryPrevBtn").disabled = cur <= 1;
        document.getElementById("profileHistoryNextBtn").disabled = cur >= totalPages;
    } catch (e) {
        info.textContent = "加载失败: " + (e.message || "未知错误");
        showToast("加载历史分析失败: " + (e.message || "未知错误"), "error");
    }
}

// ========== 详情弹层（复用发起分析的结果渲染器） ==========
function openProfileDetail(filename) {
    const mask = document.getElementById("profileDetailModal");
    const body = document.getElementById("profileDetailBody");
    document.getElementById("profileDetailTitle").textContent = "👤 人物分析详情";
    // 导出文件名：记录文件名（如 profile_20260730_1230_12345）
    document.getElementById("profileDetailExportBtn").dataset.filename = filename;
    mask.style.display = "flex";
    disposeCharts(body);
    body.innerHTML = "";
    body.appendChild(el("div", "loading", "加载中..."));
    bridge
        .apiGet("profile/history/detail", { filename, _t: Date.now() })
        .then((data) => {
            const detail = (data && data.detail) || data;
            body.innerHTML = "";
            if (!detail || !detail.stats) {
                body.appendChild(el("div", "loading", "记录内容为空或已损坏"));
                return;
            }
            renderProfileResult(body, detail, { savedHint: false });
        })
        .catch((e) => {
            closeProfileDetail();
            showToast(e.message || "分析记录不存在或已被清理", "error");
        });
}

function closeProfileDetail() {
    const body = document.getElementById("profileDetailBody");
    disposeCharts(body);
    document.getElementById("profileDetailModal").style.display = "none";
}

// ========== 删除（二次确认） ==========
// 桥接 SDK 仅暴露 apiGet/apiPost（无 apiDelete）；PRD §3.8 删除端点路径为 profile/history。
// 优先 bridge.apiDelete（未来桥接支持即生效），否则以 apiPost 发同路径 ——
// 后端需在 profile/history 路由同时注册 POST/DELETE 方法（已在报告中标注给 Module K）。
async function apiDeleteProfileHistory(filename) {
    if (typeof bridge.apiDelete === "function") {
        return bridge.apiDelete("profile/history", { filename });
    }
    return bridge.apiPost("profile/history", { filename });
}

async function deleteProfileItem(filename, item) {
    if (!filename) {
        showToast("记录缺少文件标识，无法删除", "error");
        return;
    }
    const who = item?.target_name || item?.sender_id || "该";
    const ok = await confirmDialog(
        `确定删除「${who}」的这条人物分析记录吗？\n删除后不可恢复。`,
        { title: "🗑️ 删除分析记录", okText: "删除" },
    );
    if (!ok) return;
    try {
        await apiDeleteProfileHistory(filename);
        showToast("已删除该分析记录", "success");
        await loadProfileHistory(profileHistoryPage);
    } catch (e) {
        showToast("删除失败: " + (e.message || "未知错误"), "error");
    }
}

// ========== 事件绑定：历史分析 + 详情弹窗 ==========
function bindProfileHistoryEvents() {
    document.getElementById("profileHistoryRefreshBtn").addEventListener("click", () => loadProfileHistory(1));
    document.getElementById("profileHistoryPrevBtn").addEventListener("click", () => {
        if (profileHistoryPage > 1) loadProfileHistory(profileHistoryPage - 1);
    });
    document.getElementById("profileHistoryNextBtn").addEventListener("click", () =>
        loadProfileHistory(profileHistoryPage + 1),
    );

    const closeBtn = document.getElementById("profileDetailCloseBtn");
    closeBtn.addEventListener("click", closeProfileDetail);
    // 导出图片：把详情正文（不含按钮行）渲染为 PNG 并触发下载
    document.getElementById("profileDetailExportBtn").addEventListener("click", async () => {
        const filename = document.getElementById("profileDetailExportBtn").dataset.filename || "";
        const safe = String(filename || "").replace(/[^\w.-]+/g, "_") || `人物分析_${Date.now()}`;
        await exportSectionAsPng(document.getElementById("profileDetailBody"), safe);
    });
    // 详情弹窗仅「关闭」按钮关闭（与总结详情范式一致），点击遮罩不执行任何操作
    document.getElementById("profileDetailModal").addEventListener("click", (event) => {
        event.stopPropagation();
    });
}

export { loadProfileHistory, bindProfileHistoryEvents };
