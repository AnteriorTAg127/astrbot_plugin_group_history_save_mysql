// 总结设置分区：24 项配置分组表单 / 备用模型多选弹窗 / 白名单与 CSV 互转
import { bridge, showToast, el, confirmDialog } from "./common.js";

// ========== v0.3 Tab 1：总结设置（24 项配置分组表单） ==========

// 24 项总结配置元数据：分组 / 展示名 / 说明 / 控件类型。
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
    { key: "summary_max_prompt_chars", group: "参数上限", label: "素材长度预算", desc: "送入 LLM 的完整提示词字符上限，超出从最旧消息开始截断（统计仍全量）", kind: "int", unit: "字符", min: 1000 },
    // —— 总结行为 ——
    { key: "summary_provider_id", group: "总结行为", label: "总结专用 LLM 提供商", desc: "留空时回退使用当前会话的 LLM 提供商", kind: "provider" },
    { key: "summary_fallback_providers", group: "总结行为", label: "备用总结模型列表", desc: "主选模型失败后按勾选顺序逐个尝试，全部失败回退会话模型", kind: "provider_multi" },
    { key: "summary_prompt", group: "总结行为", label: "提示词模板", desc: "占位符：{stats} {messages} {time_range} {group_id} {format_constraint}", kind: "prompt" },
    { key: "summary_output_mode", group: "总结行为", label: "输出形态", desc: "forward=合并转发（剥 Markdown）；image=文转图（保留 Markdown）", kind: "select", options: [["forward", "合并转发（forward）"], ["image", "文转图（image）"]] },
    { key: "summary_feedback_mode", group: "总结行为", label: "触发反馈模式", desc: "reaction=在触发消息上贴 👍（协议端不支持自动降级文字）；text=文字提示；none=关闭", kind: "select", options: [["reaction", "贴表情回应（reaction）"], ["text", "文字提示（text）"], ["none", "关闭（none）"]] },
    { key: "summary_feedback_text", group: "总结行为", label: "触发反馈文案", desc: "text 模式的提示文案；reaction 失败降级时同用；留空回退内置默认", kind: "text" },
    { key: "summary_rank_top_n", group: "总结行为", label: "活跃排行条数", desc: "活跃排行展示条数", kind: "int", unit: "条", min: 0 },
    // —— 存储 ——
    { key: "summary_retention_days", group: "存储", label: "总结保留天数", desc: "总结 JSON 保留天数，每日定时清理过期文件", kind: "int", unit: "天", min: 1 },
    // —— 图片渲染（v0.3.2）——
    { key: "summary_t2i_theme_mode", group: "图片渲染", label: "主题模式", desc: "自动：按服务器时间切换——浅色时段起点至深色时段起点为浅色，其余为深色", kind: "select", options: [["auto", "自动（auto）"], ["light", "浅色（light）"], ["dark", "深色（dark）"]] },
    { key: "summary_t2i_dark_start", group: "图片渲染", label: "深色时段起点", desc: "HH:MM 24 小时制，服务器本地时间", kind: "hhmm", placeholder: "22:00" },
    { key: "summary_t2i_light_start", group: "图片渲染", label: "浅色时段起点", desc: "HH:MM 24 小时制，服务器本地时间", kind: "hhmm", placeholder: "08:00" },
    { key: "summary_t2i_timeout", group: "图片渲染", label: "渲染超时", desc: "单轮截图超时；页面过大渲染不完时调大。失败后自动以双倍超时重试第二轮（JPEG 降质）", kind: "int", unit: "秒", min: 5 },
    { key: "summary_t2i_cdn_providers", group: "图片渲染", label: "CDN 节点顺序", desc: "加载 Markdown/图表脚本的 CDN 尝试顺序，逗号分隔；任一节点失败自动切换下一个。可选值：bootcdn / npmmirror / staticfile / jsdelivr / unpkg，留空=默认（国内镜像优先）", kind: "csv_list" },
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
    const stacked = meta.kind === "prompt" || meta.kind === "whitelist" || meta.kind === "provider_multi";
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
        case "provider_multi": {
            // 备用模型多选（v0.3.2 改版）：内联长列表 → 触发按钮 + 弹窗编辑。
            // 真实值存于隐藏 input[data-key]（JSON 字符串数组，collect/fill 据此读写），
            // 弹窗内「降级顺序」区可拖拽排序、「可选模型」区滚动勾选；点遮罩不关闭。
            control.className = "setting-control";
            const selected = parseProviderMultiValue(rawValue);
            const hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.dataset.key = meta.key;
            hidden.value = JSON.stringify(selected);
            control.appendChild(hidden);
            const trigger = el("button", "btn btn-ghost provider-multi-trigger");
            trigger.type = "button";
            control.appendChild(trigger);
            refreshProviderMultiTrigger(trigger, selected, summaryProviders);
            trigger.addEventListener("click", () => openProviderMultiModal(meta.key, trigger));
            break;
        }
        case "text": {
            const input = document.createElement("input");
            input.type = "text";
            input.className = "input input-text";
            input.dataset.key = meta.key;
            input.value = String(rawValue ?? "");
            control.appendChild(input);
            break;
        }
        case "hhmm": {
            // v0.3.2 图片渲染：HH:MM 时段起点文本框；前端不强校验，非法值由后端回退默认并记 warning
            const input = document.createElement("input");
            input.type = "text";
            input.className = "input input-sm";
            if (meta.placeholder) input.placeholder = meta.placeholder;
            input.dataset.key = meta.key;
            input.value = String(rawValue ?? "");
            control.appendChild(input);
            break;
        }
        case "csv_list": {
            // v0.3.2 图片渲染：有序字符串列表（CDN 节点顺序）以逗号分隔文本框编辑，
            // 后端以 JSON 字符串数组存储（与 provider_multi 同语义）
            const input = document.createElement("input");
            input.type = "text";
            input.className = "input input-text";
            input.dataset.key = meta.key;
            input.value = jsonArrayToCsv(rawValue);
            control.appendChild(input);
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

// provider_multi 值解析：后端 summary_settings 表以 JSON 字符串数组存储（如 '["id1","id2"]'），
// 解析失败 / 非数组一律视为 []；条目仅保留非空字符串（与后端非法条目静默过滤的语义一致）
function parseProviderMultiValue(rawValue) {
    let arr = rawValue;
    if (typeof rawValue === "string") {
        try {
            arr = JSON.parse(rawValue);
        } catch {
            return [];
        }
    }
    if (!Array.isArray(arr)) return [];
    return arr.filter((s) => typeof s === "string" && s !== "");
}

// v0.3.2 有序字符串列表（CDN 节点顺序）与逗号分隔文本的互转：
// 后端 list 值以 JSON 字符串数组存储，解析复用 parseProviderMultiValue（非数组/解析失败 → []）
function jsonArrayToCsv(rawValue) {
    return parseProviderMultiValue(rawValue).join(",");
}

// 逗号分隔文本 → 字符串数组：逐项 trim 并过滤空串（兼容全角逗号，与白名单解析同款）
function csvToArray(text) {
    return String(text)
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean);
}

// ========== v0.3.2 备用模型多选：触发按钮 + 弹窗（滚动勾选 + 拖拽排序）==========
// 设计：内联长 checkbox 列表改为点击触发按钮 → 弹窗编辑。弹窗分两区：
//   上「降级顺序」= 已选模型，可拖拽排序（顺序即后端降级调用顺序）；
//   下「可选模型」= 全量 provider 滚动勾选，勾选加入顺序区、取消则移除。
// 弹窗不可点遮罩关闭（仅 取消/完成 按钮），避免误触丢失排序。
// v0.4.0：组件泛化为总结/人物分析共用 —— 调用方经 openProviderMultiModalEx
// 传入各自上下文（表单选择器/providers/弹窗标题），弹窗状态全部挂 mask。

// 弹窗当前上下文的 providers（渲染弹窗内行时使用；未打开时回退总结 providers）
function activePmProviders() {
    const mask = document.getElementById("providerMultiModal");
    if (mask && Array.isArray(mask._pmProviders)) return mask._pmProviders;
    return summaryProviders || [];
}

// id → 展示名（providers 拉取后构建；失效 id 回退原值）
function providerNameOf(id) {
    const p = activePmProviders().find((x) => x.id === id);
    return p ? p.name || p.id : id;
}

// 刷新触发按钮摘要文案（已选数量 + 前两个名称，或「未配置」提示）
// providers 显式传入（fill/collect 场景弹窗未打开，不能依赖 mask 上下文）
function refreshProviderMultiTrigger(trigger, ids, providers) {
    if (!trigger) return;
    const provs = Array.isArray(providers) ? providers : summaryProviders || [];
    const nameOf = (id) => {
        const p = provs.find((x) => x.id === id);
        return p ? p.name || p.id : id;
    };
    const arr = Array.isArray(ids) ? ids : [];
    if (arr.length === 0) {
        trigger.innerHTML = '<span class="pm-trigger-empty">未配置（回退会话模型）</span><span class="pm-trigger-edit">编辑 ›</span>';
        return;
    }
    const head = arr.slice(0, 2).map(nameOf).join("、");
    const more = arr.length > 2 ? ` 等 ${arr.length} 个` : "";
    trigger.innerHTML =
        `<span class="pm-trigger-count">${arr.length}</span>` +
        `<span class="pm-trigger-names">${head}${more}</span>` +
        `<span class="pm-trigger-edit">编辑 ›</span>`;
}

// 泛化入口（v0.4.0）：ctx = { formSelector, key, trigger, providers, title }
// 表单选择器决定「完成」时写回哪个隐藏 input，providers 决定两区行内容
function openProviderMultiModalEx(ctx) {
    const mask = document.getElementById("providerMultiModal");
    if (!mask || !ctx) return;
    const formSelector = ctx.formSelector || "#summarySettingsForm";
    const hidden = document.querySelector(`${formSelector} [data-key="${ctx.key}"]`);
    const selected = hidden ? parseProviderMultiValue(hidden.value) : [];
    mask.dataset.key = ctx.key;
    mask._pmFormSelector = formSelector;
    mask._pmProviders = Array.isArray(ctx.providers) ? ctx.providers : [];
    mask._pmTrigger = ctx.trigger || null;
    mask._pmSelected = selected.slice();
    const title = document.getElementById("providerMultiModalTitle");
    if (title) title.textContent = ctx.title || "🔀 备用总结模型 · 降级顺序";
    renderProviderMultiModal();
    mask.style.display = "flex";
}

// 打开弹窗：以隐藏 input 当前值为工作副本渲染两区（总结分区专用入口，保持原签名）
function openProviderMultiModal(key, trigger) {
    openProviderMultiModalEx({
        formSelector: "#summarySettingsForm",
        key,
        trigger,
        providers: summaryProviders,
        title: "🔀 备用总结模型 · 降级顺序",
    });
}

function closeProviderMultiModal() {
    const mask = document.getElementById("providerMultiModal");
    if (mask) mask.style.display = "none";
}

// 完成：写回隐藏 input + 刷新触发摘要（写回目标表单由 mask._pmFormSelector 决定）
function confirmProviderMultiModal() {
    const mask = document.getElementById("providerMultiModal");
    if (!mask) return;
    const key = mask.dataset.key;
    const order = mask._pmSelected || [];
    const formSelector = mask._pmFormSelector || "#summarySettingsForm";
    const hidden = document.querySelector(`${formSelector} [data-key="${key}"]`);
    if (hidden) hidden.value = JSON.stringify(order);
    refreshProviderMultiTrigger(mask._pmTrigger, order, mask._pmProviders);
    closeProviderMultiModal();
}

// 渲染两区：顺序区（已选，可拖拽）+ 可选区（未选，滚动勾选）
function renderProviderMultiModal() {
    const mask = document.getElementById("providerMultiModal");
    const selectedZone = document.getElementById("pmSelectedZone");
    const availableZone = document.getElementById("pmAvailableList");
    const countEl = document.getElementById("pmSelectedCount");
    selectedZone.innerHTML = "";
    availableZone.innerHTML = "";
    const selected = mask._pmSelected || [];
    const selSet = new Set(selected);

    // 顺序区
    if (selected.length === 0) {
        selectedZone.appendChild(el("div", "pm-zone-empty", "尚未选择，请在下方勾选模型"));
    } else {
        selected.forEach((id, idx) => selectedZone.appendChild(buildPmRow(id, true, idx, selected.length)));
    }
    if (countEl) countEl.textContent = String(selected.length);

    // 可选区：未选 provider + 已失效项（在 selected 但不在 providers 中者已在顺序区显示，此处不重复）
    const providers = activePmProviders();
    const known = new Set(providers.map((p) => p.id));
    const avail = providers.filter((p) => !selSet.has(p.id));
    if (avail.length === 0) {
        availableZone.appendChild(el("div", "pm-zone-empty", selected.length ? "其余模型均已加入顺序" : "暂无可用提供商"));
    } else {
        avail.forEach((p) => availableZone.appendChild(buildPmRow(p.id, false, -1, 0)));
    }
    // 失效项若未被选中（理论上选中才进 selected，此处兜底不处理）—忽略
    void known;
}

// 构造一行：selected=true → 拖拽手柄 + 序号 + 名称 + 移除(×)；false → 复选框 + 名称
function buildPmRow(id, selected, idx, total) {
    const stale = !activePmProviders().some((p) => p.id === id);
    const row = el("div", "pm-row" + (selected ? " pm-row-selected" : "") + (stale ? " pm-row-stale" : ""));
    row.dataset.id = id;
    if (selected) {
        row.draggable = true;
        const handle = el("span", "pm-handle", "⠿");
        handle.title = "拖动以调整降级顺序";
        row.appendChild(handle);
        row.appendChild(el("span", "pm-index", String(idx + 1)));
        row.appendChild(el("span", "pm-name" + (stale ? " pm-name-stale" : ""), stale ? `${id}（已不可用）` : providerNameOf(id)));
        const rm = el("button", "pm-remove", "×");
        rm.type = "button";
        rm.title = "移除";
        rm.addEventListener("click", () => {
            const mask = document.getElementById("providerMultiModal");
            mask._pmSelected = (mask._pmSelected || []).filter((x) => x !== id);
            renderProviderMultiModal();
        });
        row.appendChild(rm);
        wirePmDrag(row);
    } else {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "pm-check";
        cb.addEventListener("change", () => {
            const mask = document.getElementById("providerMultiModal");
            if (cb.checked) mask._pmSelected = [...(mask._pmSelected || []), id];
            else mask._pmSelected = (mask._pmSelected || []).filter((x) => x !== id);
            renderProviderMultiModal();
        });
        row.appendChild(cb);
        row.appendChild(el("span", "pm-name" + (stale ? " pm-name-stale" : ""), stale ? `${id}（已不可用）` : providerNameOf(id)));
        row.addEventListener("click", (e) => {
            if (e.target !== cb) {
                cb.checked = !cb.checked;
                cb.dispatchEvent(new Event("change"));
            }
        });
    }
    return row;
}

// 顺序区拖拽排序（HTML5 DnD，仅在同区内重排；拖动时高亮插入位置）
function wirePmDrag(row) {
    row.addEventListener("dragstart", (e) => {
        row.classList.add("pm-dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", row.dataset.id);
    });
    row.addEventListener("dragend", () => {
        row.classList.remove("pm-dragging");
        const zone = document.getElementById("pmSelectedZone");
        if (zone) zone.querySelectorAll(".pm-row").forEach((r) => r.classList.remove("pm-drop-before"));
    });
    row.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        const zone = document.getElementById("pmSelectedZone");
        zone.querySelectorAll(".pm-row").forEach((r) => r.classList.remove("pm-drop-before"));
        const rect = row.getBoundingClientRect();
        if (e.clientY < rect.top + rect.height / 2) row.classList.add("pm-drop-before");
    });
    row.addEventListener("drop", (e) => {
        e.preventDefault();
        const mask = document.getElementById("providerMultiModal");
        const dragId = e.dataTransfer.getData("text/plain");
        const targetId = row.dataset.id;
        if (!dragId || dragId === targetId) return;
        const arr = (mask._pmSelected || []).filter((x) => x !== dragId);
        let ti = arr.indexOf(targetId);
        const rect = row.getBoundingClientRect();
        if (e.clientY >= rect.top + rect.height / 2) ti += 1;
        if (ti < 0) ti = 0;
        arr.splice(ti, 0, dragId);
        mask._pmSelected = arr;
        renderProviderMultiModal();
    });
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
        else if (meta.kind === "csv_list") ctrl.value = jsonArrayToCsv(raw);
        else if (meta.kind === "provider_multi") {
            // v0.3.2：ctrl 为隐藏 input[data-key]，写入 JSON 字符串数组并刷新同列触发按钮摘要
            const ids = parseProviderMultiValue(raw);
            ctrl.value = JSON.stringify(ids);
            const trig = ctrl.parentElement?.querySelector(".provider-multi-trigger");
            if (trig) refreshProviderMultiTrigger(trig, ids, summaryProviders);
        }
        else ctrl.value = String(raw ?? "");
    }
}

// 收集全部 24 项组装 save 载荷；前端基础校验失败返回 null（后端 400 为最终裁决）
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
            case "provider_multi":
                // v0.3.2：值存于隐藏 input（JSON 字符串数组，顺序即降级顺序，含 stale 失效项）
                settings[meta.key] = parseProviderMultiValue(ctrl.value);
                break;
            case "csv_list":
                // v0.3.2：CDN 节点顺序文本框 → split + trim + 过滤空串 → 数组提交（后端 list 类型 JSON 序列化）
                settings[meta.key] = csvToArray(ctrl.value);
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
    // iframe sandbox 无 allow-modals，原生 confirm() 恒 false，改用自定义确认弹窗（common.js）
    const ok = await confirmDialog("确定将全部 24 项总结设置恢复为默认值吗？此操作不可撤销。");
    if (!ok) return;
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

// ========== 事件绑定：总结设置 + 备用模型弹窗 ==========
function bindSummarySettingsEvents() {
    document.getElementById("saveSummaryBtn").addEventListener("click", saveSummarySettings);
    document.getElementById("resetSummaryAllBtn").addEventListener("click", resetSummaryAll);

    // 备用模型多选弹窗：仅按钮关闭，点遮罩/卡片内部均不关闭（防误触丢失排序）
    const pmMask = document.getElementById("providerMultiModal");
    if (pmMask) {
        document.getElementById("pmCancelBtn").addEventListener("click", closeProviderMultiModal);
        document.getElementById("pmDoneBtn").addEventListener("click", confirmProviderMultiModal);
        pmMask.addEventListener("click", (e) => {
            // 阻止冒泡到遮罩，确保点击卡片空白/滚动条不触发关闭
            if (e.target === pmMask) e.stopPropagation();
        });
        const pmCard = pmMask.querySelector(".modal-card");
        if (pmCard) pmCard.addEventListener("click", (e) => e.stopPropagation());
    }
}

export {
    loadSummarySettings,
    bindSummarySettingsEvents,
    // v0.4.0：备用模型弹窗组件供人物分析设置复用（总结/人物分析共用同一弹窗 DOM）
    openProviderMultiModalEx,
    refreshProviderMultiTrigger,
    parseProviderMultiValue,
};
