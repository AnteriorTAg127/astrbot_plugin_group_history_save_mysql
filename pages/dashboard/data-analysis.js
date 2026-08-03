// 数据分析 tab（v0.5.0，存储库分区第 4 个子 tab）
// 区块结构：
//   ① 常量与状态          —— CDN 锁版本 / 「全部」契约起点 / 表单元数据 / 页面状态
//   ② 日期工具            —— UTC 安全的日期加减与预设/自定义区间解析（跨度>366 前端拦截）
//   ③ ECharts 惰性加载    —— 锁 5.5.1（与 T2I 模板同版本），多 CDN 顺次尝试，失败降级
//   ④ 图表工具            —— 实例挂载/resize/主题取色/峰值计算/空态节点
//   ⑤ 数据加载            —— loadDataAnalysis（惰性入口）+ refreshStats（核心查询与渲染）
//   ⑥ 渲染：统计主体      —— 元信息/卡片/个人条/个人卡/每日趋势/24h与星期分布/发言人排行/群排行
//   ⑦ 渲染：推送设置区    —— 群级开关列表 + 全局 8 项表单（收集/校验/保存/重置）
//   ⑧ 事件绑定与导出
// 端点契约（分工.md 模块 H）：
//   GET  stats/data?group_id=&sender_id=&start=&end= → {stats: StatsData}（顶层键 stats）
//        start/end 均 YYYY-MM-DD 含当日（后端内部转左闭右开）；跨度含首尾 ≤366 天，否则 400
//        后端口径：sender_ranking 仅选定群维度非空；group_ranking 仅 group/member 均空时非空
//   GET  stats/settings → {settings: 8 项 typed, push_groups: [{group_id, enabled}]}
//   POST stats/settings/save  body 为扁平 {key: value}（逐键校验，任一非法整体 400）
//   POST stats/settings/reset / POST stats/push/toggle {group_id, enabled}
// 安全基线：动态文本一律 textContent / createElement（common.js el 助手），
//           严禁以 innerHTML 拼接用户数据；静态空字符串清空（innerHTML=""）除外。
import { bridge, showToast, el, confirmDialog } from "./common.js";

/* =====================================================================
 * ① 常量与状态
 * ===================================================================== */

// ECharts 锁版本 5.5.1（与 stats T2I 模板 t2i_render.py 同版本），顺次尝试，全挂则降级
const ECHARTS_CDN = [
    "https://registry.npmmirror.com/echarts/5.5.1/files/dist/echarts.min.js",
    "https://cdn.bootcdn.net/ajax/libs/echarts/5.5.1/echarts.min.js",
];
// 「全部」预设置点：与 stats/parser.py _ALL_TIME_START 契约固定值一致
const ALL_TIME_START = "2000-01-01";
// 自定义区间最长跨度（含首尾，与 PRD §6 一致），超限前端拦截 toast
const MAX_CUSTOM_DAYS = 366;
// weekday_dist 索引约定：周一=0..周日=6（模块 C 模型锚定）
const WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
// HH:MM 校验（24 小时制）
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

// 全局设置表单元数据：与 PRD §3 stats_settings 8 项一一对应（id 为 index.html 控件 id）
const SETTINGS_FIELDS = [
    { key: "stats_top_n", label: "排行条数", kind: "int", min: 1, max: 50, id: "daTopN" },
    { key: "stats_cooldown", label: "指令冷却", kind: "int", min: 0, max: 600, id: "daCooldown" },
    { key: "stats_image_top_k", label: "快照 Top K", kind: "int", min: 1, max: 100, id: "daImageTopK" },
    { key: "push_daily_enabled", label: "日报开关", kind: "bool", id: "daDailyEnabled" },
    { key: "push_daily_time", label: "日报推送时间", kind: "time", id: "daDailyTime" },
    { key: "push_weekly_enabled", label: "周报开关", kind: "bool", id: "daWeeklyEnabled" },
    { key: "push_weekly_weekday", label: "周报推送星期", kind: "weekday", id: "daWeeklyWeekday" },
    { key: "push_weekly_time", label: "周报推送时间", kind: "time", id: "daWeeklyTime" },
];

// 页面状态（模块级单例）
const state = {
    groupId: "", // "" = 全部群汇总
    preset: "7d", // today / yesterday / 7d / 30d / all
    senderId: "", // 个人视图目标 QQ；"" = 群维度
    loading: false, // stats/data 查询进行中
    loaded: false, // tab 是否已完成首次加载（惰性加载标记）
};
// 渲染序号：异步图表回调落地前比对，防止慢响应把旧数据渲染到新查询之后
let renderSeq = 0;

/* =====================================================================
 * ② 日期工具（UTC 运算避免夏令时漂移；API 口径为服务器本地日期）
 * ===================================================================== */

function fmtDateUTC(ms) {
    const d = new Date(ms);
    const m = String(d.getUTCMonth() + 1).padStart(2, "0");
    const day = String(d.getUTCDate()).padStart(2, "0");
    return `${d.getUTCFullYear()}-${m}-${day}`;
}

// "YYYY-MM-DD" → UTC 毫秒；非法返 NaN
function parseDateUTC(s) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s || "")) return NaN;
    const [y, m, d] = s.split("-").map(Number);
    return Date.UTC(y, m - 1, d);
}

function addDaysStr(s, n) {
    return fmtDateUTC(parseDateUTC(s) + n * 86400000);
}

// 本地「今天」（YYYY-MM-DD）
function todayStr() {
    const d = new Date();
    return fmtDateUTC(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
}

// 预设 → [start, end]（start/end 均含当日；后端 web_api 收到后自行转为
// 左闭右开 [start 00:00, end 次日 00:00)），均为 YYYY-MM-DD
function presetRange(preset) {
    const today = todayStr();
    switch (preset) {
        case "today":
            return { start: today, end: today };
        case "yesterday": {
            const y = addDaysStr(today, -1);
            return { start: y, end: y };
        }
        case "7d":
            return { start: addDaysStr(today, -6), end: today };
        case "30d":
            return { start: addDaysStr(today, -29), end: today };
        default: // all：契约固定起点 2000-01-01（与 stats/parser.py _ALL_TIME_START 对齐）
            return { start: ALL_TIME_START, end: today };
    }
}

// 解析当前过滤栏 → API start/end；校验失败返回 null（已 toast 提示）
// 优先级：自定义起止日期（两端都填）> 当前预设；只填一端视为未完成输入
function resolveRange() {
    const s = document.getElementById("daCustomStart").value;
    const e = document.getElementById("daCustomEnd").value;
    if (s && e) {
        const sMs = parseDateUTC(s);
        const eMs = parseDateUTC(e);
        if (isNaN(sMs) || isNaN(eMs)) {
            showToast("日期格式无效", "error");
            return null;
        }
        if (eMs < sMs) {
            showToast("结束日期不能早于开始日期", "error");
            return null;
        }
        const days = Math.round((eMs - sMs) / 86400000) + 1; // 含首尾
        if (days > MAX_CUSTOM_DAYS) {
            showToast(`自定义区间最长 ${MAX_CUSTOM_DAYS} 天（当前 ${days} 天）`, "error");
            return null;
        }
        return { start: s, end: e }; // end 含当日（后端口径：end 同样含当日）
    }
    if (s || e) {
        showToast("请填写完整的自定义起止日期", "error");
        return null;
    }
    return presetRange(state.preset);
}

/* =====================================================================
 * ③ ECharts 惰性加载（CDN 锁 5.5.1；失败 resolve(null)，由调用方降级）
 * ===================================================================== */

let echartsPromise = null;

function ensureECharts() {
    if (window.echarts) return Promise.resolve(window.echarts); // 页面内已有（如人物分析加载过）直接复用
    if (!echartsPromise) echartsPromise = loadCdnFrom(0);
    return echartsPromise;
}

// 自第 i 个 CDN 起顺次尝试；全部失败 resolve(null)（绝不 reject）
function loadCdnFrom(i) {
    return new Promise((resolve) => {
        if (i >= ECHARTS_CDN.length) {
            resolve(null);
            return;
        }
        const s = document.createElement("script");
        s.src = ECHARTS_CDN[i];
        s.async = true;
        // onload 但 window.echarts 缺失（资源损坏等）同样顺次下一家
        s.onload = () => resolve(window.echarts || loadCdnFrom(i + 1));
        s.onerror = () => resolve(loadCdnFrom(i + 1));
        document.head.appendChild(s);
    });
}

/* =====================================================================
 * ④ 图表工具
 * ===================================================================== */

// 实例挂载到节点（_ec 引用供 resize），旧实例先销毁防泄漏
function mountChart(node, ec, option) {
    if (node._ec) {
        try { node._ec.dispose(); } catch { /* 忽略销毁异常 */ }
        node._ec = null;
    }
    const chart = ec.init(node);
    node._ec = chart;
    chart.setOption(option);
}

// 统一 resize 本 tab 内可见的图表实例（窗口缩放 / 重新进入 tab 时调用）
function resizeDaCharts() {
    document.querySelectorAll("#page-data-analysis .da-echart").forEach((n) => {
        if (n._ec && n.offsetWidth > 0) {
            try { n._ec.resize(); } catch { /* 忽略 */ }
        }
    });
}

let resizeBound = false;
function bindChartResize() {
    if (resizeBound) return;
    resizeBound = true;
    window.addEventListener("resize", resizeDaCharts);
}

// 主题取色：沿用 dashboard 设计令牌，双主题自适应
function themeColors() {
    const css = getComputedStyle(document.documentElement);
    return {
        primary: css.getPropertyValue("--primary").trim() || "#165dff",
        text3: css.getPropertyValue("--text-3").trim() || "#86909c",
        border: css.getPropertyValue("--border").trim() || "#e5e6eb",
    };
}

// 分布峰值索引；全 0 返 null
function computePeak(dist) {
    let peak = null;
    let max = 0;
    dist.forEach((v, i) => {
        if (v > max) {
            max = v;
            peak = i;
        }
    });
    return max > 0 ? peak : null;
}

// 归一化为定长数字数组（缺位补 0、超长截断）
function toNumArray(arr, size) {
    if (!Array.isArray(arr)) return [];
    return Array.from({ length: size }, (_, i) => Number(arr[i]) || 0);
}

// 空态节点（图标 + 文本）
function buildEmpty(text, icon = "📭") {
    const box = el("div", "empty-state");
    box.appendChild(el("span", "empty-icon", icon));
    box.appendChild(el("p", null, text));
    return box;
}

/* =====================================================================
 * ⑤ 数据加载（惰性加载：app.js TAB_LAZY_LOAD 首次进入时调用）
 * ===================================================================== */

async function loadDataAnalysis() {
    if (state.loaded) return;
    state.loaded = true;
    loadGroupOptions(); // 过滤栏群下拉（失败不阻断，保留「全部群」）
    loadPushSettings(); // 推送设置区（群开关 + 全局 8 项）
    await refreshStats(); // 统计主体
}

// 群下拉：复用既有 groups 端点（与群管理同源），首项「全部群」value=""
async function loadGroupOptions() {
    const select = document.getElementById("daGroupSelect");
    try {
        const data = await bridge.apiGet("groups", { _t: Date.now() });
        const groups = Array.isArray(data.groups) ? data.groups : [];
        select.innerHTML = "";
        const all = el("option", null, "全部群");
        all.value = "";
        select.appendChild(all);
        for (const g of groups) {
            const gid = String(g.group_id ?? "");
            if (!gid) continue;
            const opt = el("option", null, `群 ${gid}${g.enabled ? "" : "（未启用记录）"}`);
            opt.value = gid;
            select.appendChild(opt);
        }
        select.value = state.groupId; // 恢复当前选中（重绘后不丢状态）
    } catch (e) {
        showToast("加载群列表失败: " + (e.message || "未知错误"), "error");
    }
}

// 核心查询：按当前过滤条件拉 /stats/data 并渲染全部区块
async function refreshStats() {
    if (state.loading) return;
    const range = resolveRange();
    if (!range) return;
    state.loading = true;
    renderSeq += 1;
    const seq = renderSeq;
    setFilterDisabled(true);
    showBodyLoading();
    try {
        const params = { start: range.start, end: range.end, _t: Date.now() };
        if (state.groupId) params.group_id = state.groupId; // 空 = 全部群汇总
        if (state.senderId) params.sender_id = state.senderId; // 个人视图
        const resp = await bridge.apiGet("stats/data", params);
        // 契约：顶层键 stats（避免桥接 {status,data} 解包撞名）
        const stats = resp && resp.stats;
        if (!stats) throw new Error("服务端返回了空数据");
        if (seq !== renderSeq) return; // 已有更新的查询，丢弃过期结果
        renderAll(stats);
    } catch (e) {
        if (seq !== renderSeq) return;
        showToast("统计查询失败: " + (e.message || "未知错误"), "error");
        renderQueryFailed(e.message || "未知错误");
    } finally {
        if (seq === renderSeq) {
            state.loading = false;
            setFilterDisabled(false);
        }
    }
}

// 过滤控件加载态禁用（含预设按钮组）
function setFilterDisabled(on) {
    for (const id of ["daRefreshBtn", "daGroupSelect", "daCustomStart", "daCustomEnd", "daBackToGroupBtn"]) {
        const n = document.getElementById(id);
        if (n) n.disabled = on;
    }
    document.querySelectorAll("#daPresets .da-preset").forEach((b) => (b.disabled = on));
}

// 加载占位：卡片置「…」、图表容器显示加载中、元信息提示查询中
function showBodyLoading() {
    document.getElementById("daMeta").textContent = "查询中…";
    for (const id of ["daTrendBox", "daHourBox", "daWeekdayBox"]) {
        const box = document.getElementById(id);
        box.innerHTML = "";
        box.appendChild(el("div", "loading", "加载中..."));
    }
    for (const id of ["daTotalMessages", "daTotalImages", "daActiveSenders", "daPeakHour"]) {
        document.getElementById(id).textContent = "…";
    }
}

// 查询失败：趋势区显示错误空态，排行表收起避免陈旧数据误导
function renderQueryFailed(msg) {
    const trendBox = document.getElementById("daTrendBox");
    trendBox.innerHTML = "";
    trendBox.appendChild(buildEmpty("查询失败：" + msg, "⚠️"));
    for (const id of ["daHourBox", "daWeekdayBox"]) {
        const box = document.getElementById(id);
        box.innerHTML = "";
    }
    toggleTable("daSenderTable", "daSenderEmpty", false);
    toggleTable("daGroupTable", "daGroupEmpty", false);
    document.getElementById("daMeta").textContent = "查询失败";
}

// 表格/空态显隐小工具
function toggleTable(tableId, emptyId, show) {
    document.getElementById(tableId).style.display = show ? "table" : "none";
    document.getElementById(emptyId).style.display = show ? "none" : "block";
}

/* =====================================================================
 * ⑥ 渲染：统计主体
 * ===================================================================== */

function renderAll(stats) {
    renderMemberBar(stats);
    renderMeta(stats);
    renderCards(stats);
    renderMemberCard(stats);
    renderTrend(stats);
    renderDistributions(stats);
    renderSenderRanking(stats);
    renderGroupRanking(stats);
}

// 元信息行：范围口径 + 时间标签 + 生成时刻
function renderMeta(stats) {
    const parts = [state.groupId ? `群 ${state.groupId}` : "全部群"];
    if (state.senderId) parts.push(`个人视图 QQ ${state.senderId}`);
    const label = stats?.query?.time_range?.label;
    if (label) parts.push(label);
    const gen = String(stats.generated_at || "").slice(0, 19);
    if (gen) parts.push(`生成于 ${gen}`);
    document.getElementById("daMeta").textContent = parts.join(" ｜ ");
}

// 数字千分位（非法值归 0）
function fmtInt(n) {
    const v = Number(n);
    if (!isFinite(v)) return "0";
    return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// 占比 0.0–1.0 → 百分比文本（1 位小数）
function pctText(ratio) {
    const v = Number(ratio);
    if (!isFinite(v)) return "—";
    return `${(v * 100).toFixed(1)}%`;
}

// 统计卡片（peak_hour 为 null 显「—」）
function renderCards(stats) {
    document.getElementById("daTotalMessages").textContent = fmtInt(stats.total_messages);
    document.getElementById("daTotalImages").textContent = fmtInt(stats.total_images);
    document.getElementById("daActiveSenders").textContent = fmtInt(stats.active_senders);
    const peak = stats.peak_hour;
    document.getElementById("daPeakHour").textContent = peak == null ? "—" : `${peak} 时`;
}

// 个人视图提示条（含「返回群视图」入口）
function renderMemberBar(stats) {
    const bar = document.getElementById("daMemberBar");
    if (!state.senderId) {
        bar.style.display = "none";
        return;
    }
    const m = stats.member || {};
    const name = m.sender_name || state.senderId;
    document.getElementById("daMemberBarText").textContent =
        `当前为个人视图：${name}（QQ ${state.senderId}），口径为所选群与时间范围`;
    bar.style.display = "flex";
}

// 个人交叉视图卡片：member 消息数/占比/名次/活跃天数/日均/图片数
function renderMemberCard(stats) {
    const card = document.getElementById("daMemberCard");
    const info = document.getElementById("daMemberInfo");
    const m = stats.member;
    if (!state.senderId || !m) {
        card.style.display = "none";
        return;
    }
    info.innerHTML = "";
    const grid = el("div", "detail-stats");
    grid.appendChild(statTile(fmtInt(m.count), "消息数"));
    grid.appendChild(statTile(pctText(m.ratio), "占群比例"));
    grid.appendChild(statTile(m.rank == null ? "—" : `第 ${m.rank} 名`, "群内名次"));
    grid.appendChild(statTile(fmtInt(m.active_days), "活跃天数"));
    grid.appendChild(statTile(Number(m.avg_per_day || 0).toFixed(1), "日均消息"));
    grid.appendChild(statTile(fmtInt(m.image_count), "图片数"));
    info.appendChild(grid);
    info.appendChild(
        el(
            "div",
            "da-footnote",
            "「占群比例」为所选群口径（全部群视图下为占全部消息比例）；图片数为快照 Top K 口径。",
        ),
    );
    card.style.display = "block";
}

function statTile(value, label) {
    const tile = el("div", "detail-stat-tile");
    tile.appendChild(el("div", "detail-stat-value", String(value)));
    tile.appendChild(el("div", "detail-stat-label", label));
    return tile;
}

// --- 每日趋势：ECharts 柱状图；CDN 加载失败降级为不渲染图表、仅表格 ---
function renderTrend(stats) {
    const box = document.getElementById("daTrendBox");
    box.innerHTML = "";
    const trend = Array.isArray(stats.daily_trend) ? stats.daily_trend : [];
    if (trend.length === 0) {
        box.appendChild(buildEmpty("该时间范围内暂无消息数据"));
        return;
    }
    const seq = renderSeq;
    box.appendChild(el("div", "loading", "图表加载中..."));
    ensureECharts().then((ec) => {
        if (seq !== renderSeq || !box.isConnected) return; // 过期渲染丢弃
        box.innerHTML = "";
        if (!ec) {
            box.appendChild(buildTrendTable(trend)); // 降级：仅表格
            return;
        }
        try {
            const node = el("div", "da-echart da-echart-trend");
            box.appendChild(node);
            mountChart(node, ec, buildTrendOption(trend));
            bindChartResize();
        } catch {
            box.innerHTML = "";
            box.appendChild(buildTrendTable(trend)); // 初始化异常同样降级表格
        }
    });
}

// 趋势 ECharts 配置：x 轴日期过多时旋转标签 + 自动间隔
function buildTrendOption(trend) {
    const { primary, text3, border } = themeColors();
    const dates = trend.map((t) => String(t.date || ""));
    const counts = trend.map((t) => Number(t.count) || 0);
    const n = dates.length;
    return {
        animationDuration: 400,
        grid: { left: 48, right: 14, top: 18, bottom: n > 14 ? 46 : 26 },
        tooltip: {
            trigger: "axis",
            axisPointer: { type: "shadow" },
            formatter: (ps) => {
                const p = Array.isArray(ps) ? ps[0] : ps;
                const i = p && typeof p.dataIndex === "number" ? p.dataIndex : 0;
                return `${dates[i] || ""}<br/>消息数：${fmtInt(p ? p.value : 0)}`;
            },
        },
        xAxis: {
            type: "category",
            data: dates.map((d) => d.slice(5)), // MM-DD 简写，完整日期见 tooltip
            axisLabel: {
                color: text3,
                fontSize: 10,
                rotate: n > 60 ? 60 : n > 14 ? 45 : 0, // 日期多时旋转标签
                interval: n > 31 ? "auto" : 0,
            },
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
                name: "消息数",
                type: "bar",
                barMaxWidth: 26,
                itemStyle: { color: primary, borderRadius: [3, 3, 0, 0] },
                data: counts,
            },
        ],
    };
}

// 趋势降级表格（ECharts 不可用时展示日期/消息数）
function buildTrendTable(trend) {
    const wrap = el("div", "da-trend-fallback");
    wrap.appendChild(el("div", "da-note", "图表组件加载失败，已降级为表格展示。"));
    const table = el("table", "table");
    const thead = el("thead");
    const trh = document.createElement("tr");
    trh.appendChild(el("th", null, "日期"));
    trh.appendChild(el("th", null, "消息数"));
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const t of trend) {
        const tr = document.createElement("tr");
        tr.appendChild(el("td", "td-mono", String(t.date || "-")));
        tr.appendChild(el("td", "td-mono", fmtInt(t.count)));
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
}

// --- 活跃规律分布：24 小时 + 星期；个人视图下取 member 分布（契约模块 C） ---
function renderDistributions(stats) {
    const m = state.senderId ? stats.member : null;
    const hourDist = toNumArray(m ? m.hourly_dist : stats.hourly_dist, 24);
    const weekdayDist = toNumArray(m ? m.weekday_dist : stats.weekday_dist, 7);
    document.getElementById("daDistTitle").textContent = m
        ? `🕐 活跃规律分布（${m.sender_name || state.senderId} 个人）`
        : "🕐 活跃规律分布";
    renderDistBox("daHourBox", hourDist, (i) => `${i}时`, "hour");
    renderDistBox("daWeekdayBox", weekdayDist, (i) => WEEKDAY_NAMES[i] || String(i), "weekday");
}

// 单个分布图容器：纯 CSS 柱图先行（复用 pc-* 既有组件），ECharts 成功后原地升级；失败保持 CSS 形态
function renderDistBox(boxId, dist, labelOf, kind) {
    const box = document.getElementById(boxId);
    box.innerHTML = "";
    if (dist.length === 0 || dist.every((v) => !v)) {
        box.appendChild(buildEmpty("暂无分布数据"));
        return;
    }
    const peak = computePeak(dist);
    const seq = renderSeq;
    box.appendChild(buildCssBars(dist, peak, labelOf));
    ensureECharts().then((ec) => {
        if (!ec || seq !== renderSeq || !box.isConnected) return;
        try {
            box.innerHTML = "";
            const node = el("div", "da-echart da-echart-dist");
            box.appendChild(node);
            mountChart(node, ec, buildDistOption(dist, peak, labelOf, kind));
            bindChartResize();
        } catch {
            // ECharts 初始化异常 → 回退 CSS 柱图
            box.innerHTML = "";
            box.appendChild(buildCssBars(dist, peak, labelOf));
        }
    });
}

// 纯 CSS 柱状图（pc-* 既有样式）：百分比高度 + 峰值橙色 + hover 数值
function buildCssBars(dist, peak, labelOf) {
    const max = Math.max(1, ...dist);
    const chart = el("div", "pc-css");
    dist.forEach((v, i) => {
        const col = el("div", "pc-col");
        const bar = el("div", "pc-bar" + (i === peak && v > 0 ? " pc-bar-peak" : ""));
        bar.style.height = `${Math.max(v > 0 ? 3 : 1, Math.round((v / max) * 100))}%`;
        bar.title = `${labelOf(i)}：${fmtInt(v)} 条`;
        col.appendChild(bar);
        col.appendChild(el("div", "pc-lab", labelOf(i)));
        chart.appendChild(col);
    });
    return chart;
}

// 分布 ECharts 配置（峰值橙色高亮，样式令牌与人物分析图表一致）
function buildDistOption(dist, peak, labelOf, kind) {
    const { primary, text3, border } = themeColors();
    return {
        animationDuration: 400,
        grid: { left: 40, right: 10, top: 16, bottom: 24 },
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

// --- 发言人排行：名次/昵称/消息数/图片数；点击行 → 追加 sender_id 重查（保留群与时间范围） ---
function renderSenderRanking(stats) {
    const tbody = document.getElementById("daSenderTableBody");
    const ranking = Array.isArray(stats.sender_ranking) ? stats.sender_ranking : [];
    tbody.innerHTML = "";
    if (ranking.length === 0) {
        // 空态文案按视图区分：后端口径发言人排行仅选定群维度非空，
        // 全部群汇总视图固定返回空数组（此时应看群排行）
        const emptyP = document.querySelector("#daSenderEmpty p");
        if (emptyP) {
            emptyP.textContent = state.groupId
                ? "该时间范围内暂无发言数据"
                : "发言人排行仅在选定群视图下提供，全部群汇总视图请查看群排行";
        }
        toggleTable("daSenderTable", "daSenderEmpty", false);
        return;
    }
    toggleTable("daSenderTable", "daSenderEmpty", true);
    ranking.forEach((item, idx) => {
        const sid = String(item.sender_id ?? "");
        const tr = document.createElement("tr");
        tr.className = "da-clickable-row" + (sid && sid === state.senderId ? " da-row-selected" : "");
        tr.title = "点击查看该成员的个人交叉数据";
        const tdRank = el("td");
        tdRank.appendChild(el("span", "da-rank-badge" + (idx === 0 ? " da-rank-top" : ""), String(idx + 1)));
        tr.appendChild(tdRank);
        // 昵称纯 textContent 插入防 XSS
        tr.appendChild(el("td", null, String(item.sender_name || sid || "未知用户")));
        tr.appendChild(el("td", "td-mono", fmtInt(item.count)));
        tr.appendChild(el("td", "td-mono", fmtInt(item.image_count)));
        tr.addEventListener("click", () => {
            if (state.loading || !sid) return;
            state.senderId = sid; // 保留当前群与时间范围，仅追加 sender_id 重查
            refreshStats();
        });
        tbody.appendChild(tr);
    });
}

// --- 群排行：仅「全部群」汇总视图展示（选定群/个人视图隐藏；
//     后端口径：group_ranking 仅在 group_id 与 member_id 均为空时非空） ---
function renderGroupRanking(stats) {
    const card = document.getElementById("daGroupRankCard");
    if (state.groupId || state.senderId) {
        card.style.display = "none";
        return;
    }
    card.style.display = "block";
    const tbody = document.getElementById("daGroupTableBody");
    const ranking = Array.isArray(stats.group_ranking) ? stats.group_ranking : [];
    tbody.innerHTML = "";
    if (ranking.length === 0) {
        toggleTable("daGroupTable", "daGroupEmpty", false);
        return;
    }
    toggleTable("daGroupTable", "daGroupEmpty", true);
    ranking.forEach((item, idx) => {
        const tr = document.createElement("tr");
        const tdRank = el("td");
        tdRank.appendChild(el("span", "da-rank-badge" + (idx === 0 ? " da-rank-top" : ""), String(idx + 1)));
        tr.appendChild(tdRank);
        tr.appendChild(el("td", "td-mono", String(item.group_id ?? "-")));
        tr.appendChild(el("td", "td-mono", fmtInt(item.count)));
        tr.appendChild(el("td", "td-mono", fmtInt(item.active_senders)));
        tr.appendChild(el("td", "td-mono", fmtInt(item.image_count)));
        tbody.appendChild(tr);
    });
}

/* =====================================================================
 * ⑦ 渲染：推送设置区（群级开关列表 + 全局 8 项表单）
 * ===================================================================== */

// GET stats/settings → {settings, push_groups}；失败仅提示不阻断统计主体
async function loadPushSettings() {
    const list = document.getElementById("daPushGroupList");
    try {
        const data = await bridge.apiGet("stats/settings", { _t: Date.now() });
        fillSettingsForm(data.settings || {});
        renderPushGroups(Array.isArray(data.push_groups) ? data.push_groups : []);
    } catch (e) {
        list.innerHTML = "";
        list.appendChild(el("div", "loading", "推送设置加载失败"));
        showToast("加载推送设置失败: " + (e.message || "未知错误"), "error");
    }
}

// 群级开关列表：每群 toggle → POST stats/push/toggle
function renderPushGroups(groups) {
    const list = document.getElementById("daPushGroupList");
    list.innerHTML = "";
    if (groups.length === 0) {
        list.appendChild(el("div", "loading", "暂无白名单群，请先在「群管理」添加"));
        return;
    }
    for (const g of groups) {
        const gid = String(g.group_id ?? "");
        if (!gid) continue;
        const row = el("div", "s-item");
        const info = el("div", "setting-info");
        info.appendChild(el("div", "setting-name", `群 ${gid}`));
        info.appendChild(el("div", "setting-desc", "开启后接收该群的日报/周报推送（需全局开关同时开启）"));
        row.appendChild(info);
        const control = el("div", "setting-control");
        const label = el("label", "all-mode-switch");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !!g.enabled;
        input.addEventListener("change", () => togglePushGroup(gid, input));
        const track = el("span", "switch-track");
        track.appendChild(el("span", "switch-thumb"));
        label.appendChild(input);
        label.appendChild(track);
        control.appendChild(label);
        row.appendChild(control);
        list.appendChild(row);
    }
}

async function togglePushGroup(groupId, input) {
    input.disabled = true;
    try {
        await bridge.apiPost("stats/push/toggle", { group_id: groupId, enabled: input.checked });
        showToast(input.checked ? `已开启群 ${groupId} 推送` : `已关闭群 ${groupId} 推送`, "success");
    } catch (e) {
        input.checked = !input.checked; // 失败回滚开关
        showToast("切换失败: " + (e.message || "未知错误"), "error");
    } finally {
        input.disabled = false;
    }
}

// 用接口返回的全量 settings 回填表单（首次加载/保存成功/重置成功复用）
function fillSettingsForm(settings) {
    for (const meta of SETTINGS_FIELDS) {
        const ctrl = document.getElementById(meta.id);
        if (!ctrl || !(meta.key in settings)) continue;
        const raw = settings[meta.key];
        if (meta.kind === "bool") ctrl.checked = String(raw).toLowerCase() === "true";
        else ctrl.value = String(raw ?? "");
    }
}

// 收集 8 项并做前端基础校验（后端 400 为最终裁决）；非法返 null（已 toast）
function collectSettings() {
    const settings = {};
    for (const meta of SETTINGS_FIELDS) {
        const ctrl = document.getElementById(meta.id);
        if (!ctrl) continue;
        if (meta.kind === "bool") {
            settings[meta.key] = ctrl.checked;
        } else if (meta.kind === "int") {
            const raw = (ctrl.value || "").trim();
            if (!/^\d+$/.test(raw)) {
                showToast(`「${meta.label}」需为非负整数`, "error");
                return null;
            }
            const v = parseInt(raw, 10);
            if (v < meta.min || v > meta.max) {
                showToast(`「${meta.label}」需在 ${meta.min}–${meta.max} 之间`, "error");
                return null;
            }
            settings[meta.key] = v;
        } else if (meta.kind === "time") {
            const v = ctrl.value || "";
            if (!TIME_RE.test(v)) {
                showToast(`「${meta.label}」格式需为 HH:MM（24 小时制）`, "error");
                return null;
            }
            settings[meta.key] = v;
        } else if (meta.kind === "weekday") {
            const v = parseInt(ctrl.value, 10);
            if (!(v >= 1 && v <= 7)) {
                showToast(`「${meta.label}」需在 1–7 之间`, "error");
                return null;
            }
            settings[meta.key] = v;
        }
    }
    return settings;
}

async function saveSettings() {
    const settings = collectSettings();
    if (!settings) return;
    const btns = [document.getElementById("daSaveSettingsBtn"), document.getElementById("daResetSettingsBtn")];
    btns.forEach((b) => (b.disabled = true));
    try {
        // 契约：body 为扁平 {key: value} 键值对象（后端逐键校验，未知键 400）
        const data = await bridge.apiPost("stats/settings/save", settings);
        if (data && data.settings) fillSettingsForm(data.settings); // 后端回写归一化值
        showToast("数据分析设置已保存", "success");
    } catch (e) {
        // 后端校验失败（400）的错误信息直接展示
        showToast("保存失败: " + (e.message || "未知错误"), "error");
    } finally {
        btns.forEach((b) => (b.disabled = false));
    }
}

async function resetSettings() {
    // iframe sandbox 无 allow-modals，原生 confirm 恒 false，统一用 confirmDialog
    const ok = await confirmDialog(
        `确定将全部 ${SETTINGS_FIELDS.length} 项数据分析/推送设置恢复为默认值吗？此操作不可撤销。`,
        { title: "⚠️ 恢复默认设置" },
    );
    if (!ok) return;
    const btns = [document.getElementById("daSaveSettingsBtn"), document.getElementById("daResetSettingsBtn")];
    btns.forEach((b) => (b.disabled = true));
    try {
        const data = await bridge.apiPost("stats/settings/reset", {});
        fillSettingsForm((data && data.settings) || {});
        showToast("已恢复全部默认设置", "success");
    } catch (e) {
        showToast("重置失败: " + (e.message || "未知错误"), "error");
    } finally {
        btns.forEach((b) => (b.disabled = false));
    }
}

/* =====================================================================
 * ⑧ 事件绑定与导出
 * ===================================================================== */

// 预设按钮高亮同步
function updatePresetActive() {
    document.querySelectorAll("#daPresets .da-preset").forEach((b) =>
        b.classList.toggle("active", b.dataset.range === state.preset),
    );
}

function bindDataAnalysisEvents() {
    // 刷新按钮
    document.getElementById("daRefreshBtn").addEventListener("click", () => refreshStats());

    // 群下拉切换 → 立即重查（个人视图保留 senderId，即查该成员在新群的数据）
    document.getElementById("daGroupSelect").addEventListener("change", (e) => {
        state.groupId = e.target.value;
        refreshStats();
    });

    // 时间预设按钮组：点击切换并立即查询；自定义日期交由「刷新」触发（两端都填时优先生效）
    document.querySelectorAll("#daPresets .da-preset").forEach((btn) => {
        btn.addEventListener("click", () => {
            state.preset = btn.dataset.range;
            document.getElementById("daCustomStart").value = "";
            document.getElementById("daCustomEnd").value = "";
            updatePresetActive();
            refreshStats();
        });
    });

    // 返回群视图：清除 sender_id 重查
    document.getElementById("daBackToGroupBtn").addEventListener("click", () => {
        state.senderId = "";
        refreshStats();
    });

    // 推送设置：保存 / 恢复默认
    document.getElementById("daSaveSettingsBtn").addEventListener("click", saveSettings);
    document.getElementById("daResetSettingsBtn").addEventListener("click", resetSettings);
}

// 每次进入 tab 时由 app.js TAB_ENTER_HOOKS 调用：
// 图表实例切 tab 不销毁只隐藏，重新可见后 resize 对齐容器（窗口在隐藏期间缩放过时尤为重要）
function enterDataAnalysis() {
    resizeDaCharts();
}

export { loadDataAnalysis, bindDataAnalysisEvents, enterDataAnalysis };
