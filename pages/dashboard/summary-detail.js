const bridge = window.AstrBotPluginPage;

function showToast(msg, type = "") {
    const toast = document.getElementById("toast");
    toast.textContent = msg;
    toast.className = "toast show" + (type ? ` ${type}` : "");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toast.className = "toast"; }, 2600);
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

function formatSources(sources) {
    if (!sources || typeof sources !== "object") return "-";
    return Object.entries(sources).map(([key, value]) => `${key}: ${value}`).join(" / ") || "-";
}

function formatTimeRange(start, end) {
    if (!start && !end) return "-";
    return `${start || "-"} ~ ${end || "-"}`;
}

export function openSummaryDetail(groupId, filename) {
    const mask = document.getElementById("historyDetailModal");
    const body = document.getElementById("historyDetailBody");
    document.getElementById("historyDetailTitle").textContent = `📄 总结详情 · 群 ${groupId}`;
    // 导出参数：群号 + 总结文件名（如 1751234567_a1b2c3.json）
    document.getElementById("historyDetailExportBtn").dataset.filename = filename;
    document.getElementById("historyDetailExportBtn").dataset.groupId = groupId;
    mask.style.display = "flex";
    body.innerHTML = "";
    body.appendChild(el("div", "loading", "加载中..."));
    bridge
        .apiGet("summary/history/detail", { group_id: groupId, filename })
        .then((data) => renderSummaryDetail(body, data.detail || {}))
        .catch((e) => {
            closeSummaryDetail();
            showToast(e.message || "总结记录不存在或已被清理", "error");
        });
}

function closeSummaryDetail() {
    document.getElementById("historyDetailModal").style.display = "none";
}

function renderSummaryDetail(container, detail) {
    container.innerHTML = "";
    const metaLine = el("div", "detail-meta");
    metaLine.appendChild(el("span", null, `🕐 ${detail.generated_at || "-"}`));
    metaLine.appendChild(el("span", null, `📐 ${detail.scope_desc || "-"}`));
    metaLine.appendChild(el("span", null, `🤖 ${detail.provider_id || "会话回退"}`));
    container.appendChild(metaLine);

    const stats = detail.stats || {};
    const statsGrid = el("div", "detail-stats");
    statsGrid.appendChild(detailStatTile(String(stats.total ?? 0), "消息总数"));
    statsGrid.appendChild(detailStatTile(String(stats.participant_count ?? 0), "参与者"));
    statsGrid.appendChild(detailStatTile(formatTimeRange(stats.time_start, stats.time_end), "时间跨度"));
    statsGrid.appendChild(detailStatTile(formatSources(detail.sources), "数据源构成"));
    statsGrid.appendChild(detailStatTile(String(detail.messages_used ?? 0), "送入 LLM"));
    container.appendChild(statsGrid);

    if (stats.truncated) {
        container.appendChild(el("div", "detail-truncated", "⚠️ 消息过多已被截断，统计基于截断前全量"));
    }

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

    const sections = Array.isArray(detail.sections) ? detail.sections : [];
    if (sections.length > 0) {
        const secs = el("div", "detail-sections");
        for (const sec of sections) {
            const block = el("div", "detail-section");
            block.appendChild(el("div", "detail-section-title", String(sec?.[0] ?? "")));
            const content = el("div", "detail-section-content markdown-body");
            const markdown = String(sec?.[1] ?? "");
            if (window.marked && window.DOMPurify) {
                // LLM 原文绝不直接拼 innerHTML：先 marked 再 DOMPurify 消毒
                const html = window.marked.parse(markdown, { breaks: true, gfm: true });
                content.innerHTML = window.DOMPurify.sanitize(html);
            } else {
                // CDN 缺库 → textContent 纯文本展示，杜绝注入
                content.textContent = markdown;
            }
            block.appendChild(content);
            secs.appendChild(block);
        }
        container.appendChild(secs);
    }

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

export function bindSummaryDetailEvents() {
    document.getElementById("historyDetailCloseBtn").addEventListener("click", closeSummaryDetail);
    // 导出图片：后端 T2I 流水线渲染（与聊天端图片同模板同配置），bridge.download 触发浏览器下载。
    // T2I 渲染耗时可能达数十秒，期间以 toast 提示；失败（如渲染服务不可用）明确报错
    document.getElementById("historyDetailExportBtn").addEventListener("click", async () => {
        const btn = document.getElementById("historyDetailExportBtn");
        const filename = btn.dataset.filename || "";
        const groupId = btn.dataset.groupId || "";
        if (!filename) {
            showToast("缺少导出参数", "error");
            return;
        }
        btn.disabled = true;
        showToast("正在渲染图片，请稍候…（约 10~60 秒）");
        try {
            await bridge.download("summary/history/export", { group_id: groupId, filename }, `${groupId}_summary.png`);
            showToast("导出完成，已开始下载", "success");
        } catch (e) {
            showToast(e.message || "导出失败，请稍后重试", "error");
        } finally {
            btn.disabled = false;
        }
    });
    // 详情弹窗只能通过「关闭」按钮关闭，点击遮罩不执行任何操作。
    document.getElementById("historyDetailModal").addEventListener("click", (event) => {
        event.stopPropagation();
    });
}
