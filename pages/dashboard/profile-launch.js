// 人物分析 · 发起分析 tab（v0.4.0）
// QQ号 + 范围下拉（全局/已保存群）→ POST profile/analyze（长耗时 loading）
// → 结果渲染：统计卡 + 活动时间图表（24h/7weekday，ECharts 惰性加载 + 纯 CSS 兜底）
// + 互动排行 + 板块 Markdown（marked + DOMPurify，缺库降级 textContent 防 XSS）
// renderProfileResult 导出供 profile-history.js 详情弹层复用（同款渲染范式）
import { bridge, showToast, el } from "./common.js";

// weekday_dist 索引约定：Mon=0..Sun=6（与 Module E 统计锚定一致）
const WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

// 数字千分位（非法值归 0），与 data-analysis.js fmtInt 同口径
function fmtInt(n) {
    const v = Number(n);
    if (!isFinite(v)) return "0";
    return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// ========== ECharts 惰性加载（CDN 失败保留纯 CSS 柱图兜底） ==========
let echartsPromise = null;
function ensureECharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    if (!echartsPromise) {
        echartsPromise = new Promise((resolve) => {
            const s = document.createElement("script");
            s.src = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js";
            s.async = true;
            s.onload = () => resolve(window.echarts || null);
            // CDN 全挂 → resolve(null)，CSS 柱图兜底永远可用，绝不 reject
            s.onerror = () => resolve(null);
            document.head.appendChild(s);
        });
    }
    return echartsPromise;
}

// 全局唯一 resize 监听：统一缩放页面上存活的 ECharts 实例（实例挂节点 _ec）
let chartResizeBound = false;
function bindChartResize() {
    if (chartResizeBound) return;
    chartResizeBound = true;
    window.addEventListener("resize", () => {
        document.querySelectorAll(".pc-echart").forEach((node) => {
            if (node._ec) node._ec.resize();
        });
    });
}

// 释放容器内所有 ECharts 实例（重渲染/关弹窗前调用，防内存泄漏）
function disposeCharts(root) {
    if (!root) return;
    root.querySelectorAll(".pc-echart").forEach((node) => {
        if (node._ec) {
            try { node._ec.dispose(); } catch { /* 忽略销毁异常 */ }
            node._ec = null;
        }
    });
}

// ========== 范围下拉：全局 + /profile/groups 已保存群列表（模式感知，v0.5.6） ==========
async function loadProfileGroups() {
    const select = document.getElementById("profileGroupSelect");
    try {
        const data = await bridge.apiGet("profile/groups", { _t: Date.now() });
        const groups = Array.isArray(data.groups) ? data.groups : [];
        select.innerHTML = "";
        const all = el("option", null, "全局（所有已保存的群）");
        all.value = "all";
        select.appendChild(all);
        for (const g of groups) {
            const gid = String(g.group_id ?? "");
            if (!gid) continue;
            const cntTxt = g.count != null ? `（${fmtInt(g.count)} 条）` : "";
            const opt = el(
                "option",
                null,
                `群 ${gid}${cntTxt}${g.enabled ? "" : "（未启用记录）"}`
            );
            opt.value = gid;
            select.appendChild(opt);
        }
    } catch (e) {
        // 群列表加载失败不阻断：至少保留「全局」选项
        showToast("加载群列表失败: " + (e.message || "未知错误"), "error");
        if (!select.querySelector('option[value="all"]')) {
            const all = el("option", null, "全局（所有已保存的群）");
            all.value = "all";
            select.appendChild(all);
        }
    }
}

// ========== 触发分析（长耗时含 LLM，全程 loading 态） ==========
async function launchProfileAnalysis() {
    const input = document.getElementById("profileSenderId");
    const select = document.getElementById("profileGroupSelect");
    const btn = document.getElementById("profileLaunchBtn");
    const out = document.getElementById("profileLaunchResult");

    const qq = input.value.trim();
    if (!/^\d+$/.test(qq)) {
        showToast("QQ 号需为纯数字", "error");
        input.focus();
        return;
    }
    const scope = select.value || "all";

    btn.disabled = true;
    input.disabled = true;
    select.disabled = true;
    disposeCharts(out);
    out.innerHTML = "";
    const loading = el("div", "profile-loading");
    loading.appendChild(el("span", "profile-spinner"));
    loading.appendChild(
        el("div", "profile-loading-text", "正在分析中，包含 LLM 调用，可能需要数十秒，请勿关闭页面…"),
    );
    out.appendChild(loading);

    try {
        // PRD §3.8：POST /profile/analyze {sender_id, group_id}，group_id 为空/all = 全局
        const data = await bridge.apiPost("profile/analyze", {
            sender_id: qq,
            group_id: scope === "all" ? "all" : scope,
        });
        // 防御性兼容：K 端点直接返回 ProfileResult JSON 或 {result: ...} 包装
        const result = (data && data.result) || data;
        out.innerHTML = "";
        if (!result || !result.stats) {
            renderProfileError(out, "服务端返回了空结果，请重试或查看插件日志（[Profile] 前缀）");
            return;
        }
        renderProfileResult(out, result, { savedHint: true });
        showToast("人物分析完成", "success");
    } catch (e) {
        out.innerHTML = "";
        renderProfileError(out, e.message || "未知错误");
        showToast("分析失败: " + (e.message || "未知错误"), "error");
    } finally {
        btn.disabled = false;
        input.disabled = false;
        select.disabled = false;
    }
}

function renderProfileError(container, message) {
    const box = el("div", "profile-error");
    box.appendChild(el("div", "profile-error-title", "❌ 分析失败"));
    box.appendChild(el("div", "profile-error-msg", String(message)));
    const hints = el("ul", "profile-error-hints");
    [
        "确认目标 QQ 号在所选范围内有过发言记录（全局模式仅统计已保存的群）",
        "检查「分析设置」中人物分析总开关已开启、主选/备用模型可用",
        "LLM 调用耗时较长，若为超时失败可稍后重试；多次失败请查看插件日志",
    ].forEach((t) => hints.appendChild(el("li", null, t)));
    box.appendChild(hints);
    container.appendChild(box);
}

// ========== 结果渲染器（发起分析内联 + 历史详情弹层共用） ==========
function formatTimeRange(start, end) {
    if (!start && !end) return "-";
    if (!start || !end) return String(start || end);
    const s = String(start);
    const e = String(end);
    const sameDay = s.slice(0, 10) === e.slice(0, 10);
    return `${s.slice(0, 16)} ~ ${sameDay ? e.slice(11, 16) : e.slice(0, 16)}`;
}

function statTile(value, label) {
    const tile = el("div", "detail-stat-tile");
    tile.appendChild(el("div", "detail-stat-value", String(value)));
    tile.appendChild(el("div", "detail-stat-label", label));
    return tile;
}

function renderProfileResult(container, result, opts = {}) {
    disposeCharts(container);
    container.innerHTML = "";
    const target = result.target || {};
    const stats = result.stats || {};

    // —— 报告头：目标昵称 / QQ / 范围 / 时间跨度 / provider / 生成时间 ——
    const head = el("div", "profile-result-head");
    const titleRow = el("div", "profile-result-title");
    titleRow.appendChild(
        el("div", "profile-result-name", `人物画像 · ${target.sender_name || target.sender_id || "未知用户"}`),
    );
    if (target.sender_id) titleRow.appendChild(el("span", "profile-result-qq", `QQ ${target.sender_id}`));
    head.appendChild(titleRow);
    const meta = el("div", "profile-result-meta");
    meta.appendChild(el("span", null, `📐 ${result.scope_desc || (target.scope === "all" ? "全局" : "单群")}`));
    meta.appendChild(el("span", null, `🕐 ${formatTimeRange(stats.time_start, stats.time_end)}`));
    meta.appendChild(el("span", null, `🤖 ${result.provider_id || "会话回退"}`));
    if (result.created_at) meta.appendChild(el("span", null, `📅 生成于 ${result.created_at}`));
    head.appendChild(meta);
    container.appendChild(head);

    // —— 统计卡片行：总数 / 涉及群数 / 活跃天数 / 平均长度 ——
    const grid = el("div", "detail-stats");
    grid.appendChild(statTile(stats.total ?? 0, "消息总数"));
    grid.appendChild(statTile(stats.group_count ?? (target.scope === "all" ? 0 : 1), "涉及群数"));
    grid.appendChild(statTile(stats.active_days ?? 0, "活跃天数"));
    grid.appendChild(statTile(Number(stats.avg_length ?? 0).toFixed(1), "平均长度(字)"));
    container.appendChild(grid);

    // —— 状态提示条 ——
    if (stats.truncated) {
        container.appendChild(el("div", "detail-truncated", "⚠️ 消息过多已被截断，统计基于截断前全量"));
    }
    if (result.relation_context_complete === false) {
        container.appendChild(
            el("div", "detail-truncated", "⚠️ 关系上下文采集不完整，人物关系板块为浅层推断"),
        );
    }

    // —— 活动时间图表（24h + 7weekday） ——
    renderActivityCharts(container, stats);

    // —— 互动对象排行 ——
    const partners = Array.isArray(stats.top_partners) ? stats.top_partners : [];
    renderPartners(container, partners);

    // —— 各板块内容（Markdown 渲染，防 XSS） ——
    const sections = Array.isArray(result.sections) ? result.sections : [];
    if (sections.length > 0) {
        const secs = el("div", "detail-sections");
        for (const sec of sections) {
            appendMarkdownBlock(secs, String(sec?.[0] ?? ""), String(sec?.[1] ?? ""));
        }
        container.appendChild(secs);
    } else {
        container.appendChild(
            el("div", "profile-note-text", "（LLM 叙述板块为空 —— 可能生成失败，以上为确定性统计结果）"),
        );
    }

    // —— LLM 原始输出折叠 ——
    if (result.raw_llm_text) {
        const fold = document.createElement("details");
        fold.className = "detail-raw";
        fold.appendChild(el("summary", null, "查看 LLM 原始输出"));
        const pre = el("pre", "detail-raw-text");
        pre.textContent = String(result.raw_llm_text);
        fold.appendChild(pre);
        container.appendChild(fold);
    }

    // —— 免责声明页脚 ——
    const disc = el("div", "profile-disclaimer");
    disc.appendChild(el("span", null, "⚠️ 本报告基于公开群聊记录由 AI 推测生成，仅供参考，非事实结论。"));
    disc.appendChild(el("span", null, `模型：${result.provider_id || "会话回退"}`));
    container.appendChild(disc);

    // —— 已自动落盘提示（仅发起分析页展示） ——
    if (opts.savedHint) {
        const hint = el("div", "profile-saved-hint");
        hint.appendChild(el("span", null, "💾 分析结果已自动保存，可在「历史分析」中随时回看"));
        const goBtn = el("button", "btn btn-ghost btn-sm", "查看历史 ›");
        goBtn.type = "button";
        goBtn.addEventListener("click", () => {
            const tab = document.querySelector('.tabs-profile .tab[data-tab="profile-history"]');
            if (tab) tab.click();
        });
        hint.appendChild(goBtn);
        container.appendChild(hint);
    }
}

// ========== 活动时间图表：ECharts 升级 + 纯 CSS 柱图兜底 ==========
function renderActivityCharts(container, stats) {
    const hourDist = Array.isArray(stats.hour_dist) ? stats.hour_dist : [];
    const weekdayDist = Array.isArray(stats.weekday_dist) ? stats.weekday_dist : [];
    if (hourDist.length === 0 && weekdayDist.length === 0) return;
    const wrap = el("div", "profile-charts");
    if (hourDist.length) {
        wrap.appendChild(
            buildChartBlock("🕐 24 小时发言分布", hourDist, stats.peak_hour, (i) => String(i), "hour"),
        );
    }
    if (weekdayDist.length) {
        wrap.appendChild(
            buildChartBlock("📅 星期发言分布", weekdayDist, stats.peak_weekday, (i) => WEEKDAY_NAMES[i] || String(i), "weekday"),
        );
    }
    container.appendChild(wrap);
}

function buildChartBlock(title, dist, peak, labelOf, kind) {
    const block = el("div", "pc-block");
    const titleEl = el("div", "pc-title");
    titleEl.appendChild(el("span", null, title));
    const max = Math.max(0, ...dist);
    if (typeof peak === "number" && peak >= 0 && dist[peak] > 0 && max > 0) {
        titleEl.appendChild(el("span", "pc-peak", `峰值 ${labelOf(peak)} · ${dist[peak]} 条`));
    }
    block.appendChild(titleEl);

    const box = el("div", "pc-box");
    // 纯 CSS 柱图先行渲染（永远在 DOM，CDN 失败也能看）
    box.appendChild(buildCssBars(dist, peak, labelOf, kind, max));
    block.appendChild(box);

    // ECharts 惰性加载成功后原地升级为可交互图表；失败/已卸载则保持 CSS 形态
    ensureECharts().then((ec) => {
        if (!ec || !box.isConnected) return;
        try {
            box.innerHTML = "";
            const node = document.createElement("div");
            node.className = "pc-echart";
            box.appendChild(node);
            const chart = ec.init(node);
            node._ec = chart;
            chart.setOption(buildBarOption(dist, peak, labelOf, kind));
            bindChartResize();
        } catch {
            // ECharts 初始化异常 → 回退 CSS 柱图
            box.innerHTML = "";
            box.appendChild(buildCssBars(dist, peak, labelOf, kind, max));
        }
    });
    return block;
}

// 纯 CSS 柱状图：百分比高度 div，峰值高亮，hover 显示数值
function buildCssBars(dist, peak, labelOf, kind, max) {
    const safeMax = Math.max(1, max ?? 0);
    const chart = el("div", "pc-css" + (kind === "hour" ? " pc-css-hour" : ""));
    dist.forEach((v, i) => {
        const col = el("div", "pc-col");
        const bar = el("div", "pc-bar" + (i === peak && v > 0 ? " pc-bar-peak" : ""));
        bar.style.height = `${Math.max(v > 0 ? 3 : 1, Math.round((Number(v) / safeMax) * 100))}%`;
        bar.title = `${labelOf(i)}：${v} 条`;
        col.appendChild(bar);
        col.appendChild(el("div", "pc-lab", labelOf(i)));
        chart.appendChild(col);
    });
    return chart;
}

// ECharts 配置：主题色取自 dashboard 设计令牌（双主题自适应），峰值橙色高亮
function buildBarOption(dist, peak, labelOf, kind) {
    const css = getComputedStyle(document.documentElement);
    const primary = css.getPropertyValue("--primary").trim() || "#165dff";
    const text3 = css.getPropertyValue("--text-3").trim() || "#86909c";
    const border = css.getPropertyValue("--border").trim() || "#e5e6eb";
    return {
        animationDuration: 500,
        grid: { left: 38, right: 10, top: 16, bottom: 24 },
        tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
        xAxis: {
            type: "category",
            data: dist.map((_, i) => labelOf(i)),
            axisLabel: { color: text3, fontSize: 10, interval: kind === "hour" ? 2 : 0 },
            axisLine: { lineStyle: { color: border } },
            axisTick: { show: false },
        },
        yAxis: {
            type: "value",
            minInterval: 1,
            axisLabel: { color: text3, fontSize: 10 },
            splitLine: { lineStyle: { color: border } },
        },
        series: [
            {
                type: "bar",
                barMaxWidth: kind === "hour" ? 16 : 40,
                data: dist.map((v, i) => ({
                    value: v,
                    itemStyle: {
                        color: i === peak && v > 0 ? "#ff7d00" : primary,
                        borderRadius: [3, 3, 0, 0],
                    },
                })),
            },
        ],
    };
}

// ========== 互动对象排行（横向百分比条，确定性统计） ==========
function renderPartners(container, partners) {
    if (!partners.length) return;
    const block = el("div", "pr-block");
    block.appendChild(el("div", "pc-title", "🤝 互动排行"));
    const max = Math.max(1, ...partners.map((p) => Number(p?.[2]) || 0));
    partners.forEach((p, idx) => {
        const sid = p?.[0] ?? "";
        const name = p?.[1] ?? "";
        const count = Number(p?.[2]) || 0;
        const row = el("div", "pr-row");
        row.appendChild(el("span", "pr-rank" + (idx === 0 ? " pr-rank-top" : ""), String(idx + 1)));
        row.appendChild(el("span", "pr-name", String(name || sid || "未知用户")));
        if (name && sid) row.appendChild(el("span", "pr-id", String(sid)));
        const barWrap = el("div", "pr-bar-wrap");
        const bar = el("div", "pr-bar");
        bar.style.width = `${Math.max(count > 0 ? 2 : 0, Math.round((count / max) * 100))}%`;
        bar.title = `${name || sid}：${count} 次互动`;
        barWrap.appendChild(bar);
        row.appendChild(barWrap);
        row.appendChild(el("span", "pr-count", `${count} 次`));
        block.appendChild(row);
    });
    container.appendChild(block);
}

// ========== 板块 Markdown 渲染（marked + DOMPurify；缺库降级纯文本防 XSS） ==========
function appendMarkdownBlock(parent, title, markdown) {
    const block = el("div", "detail-section");
    block.appendChild(el("div", "detail-section-title", title));
    const content = el("div", "detail-section-content markdown-body");
    if (window.marked && window.DOMPurify) {
        // LLM 原文绝不直接拼 innerHTML：先 marked 再 DOMPurify 消毒
        const html = window.marked.parse(markdown, { breaks: true, gfm: true });
        content.innerHTML = window.DOMPurify.sanitize(html);
    } else {
        // CDN 缺库 → textContent 纯文本展示，杜绝注入
        content.textContent = markdown;
    }
    block.appendChild(content);
    parent.appendChild(block);
}

// ========== 事件绑定：发起分析 ==========
function bindProfileLaunchEvents() {
    document.getElementById("profileLaunchBtn").addEventListener("click", launchProfileAnalysis);
    document.getElementById("profileSenderId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") launchProfileAnalysis();
    });
}

export { loadProfileGroups, bindProfileLaunchEvents, renderProfileResult, disposeCharts };
