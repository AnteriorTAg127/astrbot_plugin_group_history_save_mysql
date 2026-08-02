// 人物分析 · 分析设置 tab（v0.4.0）：19 项 profile_* 配置分组表单
// 字段与 PRD §5 一一对应；备用模型复用 v0.3.2 拖拽排序弹窗组件（summary-settings.js 导出）；
// 「图片渲染」分组仅说明文字 —— 与消息总结共用 summary_t2i_* 配置，不重复表单
// 端点契约（PRD §3.8）：GET/POST profile/settings、POST profile/settings/reset、GET profile/providers
import { bridge, showToast, el } from "./common.js";
import {
    openProviderMultiModalEx,
    refreshProviderMultiTrigger,
    parseProviderMultiValue,
} from "./summary-settings.js";

// 19 项人物分析配置元数据：分组 / 展示名 / 说明 / 控件类型
// kind 与后端 PROFILE_TYPES（bool/int/str/list）对应，请求封装沿用 bridge.apiGet/apiPost
const PROFILE_FIELD_META = [
    // —— 基础 ——
    { key: "profile_enabled", group: "基础", label: "功能总开关", desc: "关闭后 /人物分析 指令与 Web 发起分析均不可用", kind: "bool" },
    { key: "profile_permission", group: "基础", label: "指令权限", desc: "人物画像涉及个体隐私，默认仅管理员可在群里触发", kind: "select", options: [["admin", "仅管理员（admin）"], ["all", "所有人（all）"]] },
    { key: "profile_output_mode", group: "基础", label: "输出模式", desc: "指令触发时的输出形态；Web 发起分析始终在页面内渲染结果", kind: "select", options: [["forward", "合并转发（forward）"], ["image", "文转图（image）"], ["text", "纯文本（text）"]] },
    // —— 模型 ——
    { key: "profile_provider", group: "模型", label: "主选 LLM 提供商", desc: "人物分析独立一套模型配置，与消息总结互不干扰；留空时回退当前会话 provider", kind: "provider" },
    { key: "profile_fallback_providers", group: "模型", label: "备用模型列表", desc: "主选模型失败后按勾选顺序逐个尝试，全部失败回退会话模型", kind: "provider_multi" },
    // —— 参数上限 ——
    { key: "profile_max_count", group: "参数上限", label: "最大分析条数", desc: "单次分析最多纳入的目标消息条数（分页拉取上限）", kind: "int", unit: "条", min: 1 },
    { key: "profile_max_prompt_chars", group: "参数上限", label: "素材长度预算", desc: "送入 LLM 的素材字符上限，超出保留最近消息截断（统计仍全量）", kind: "int", unit: "字符", min: 1000 },
    // —— 关系上下文 ——
    { key: "profile_relation_context", group: "关系上下文", label: "关系上下文开关", desc: "开启后双向识别 @/回复 互动对象并拉取双方消息，用于人物关系分析；任一环节失败自动降级，不阻断主分析", kind: "bool" },
    { key: "profile_relation_max_partners", group: "关系上下文", label: "最大互动对象数", desc: "互动对象排行取 Top N，其消息作为关系分析的双方对话上下文", kind: "int", unit: "人", min: 1 },
    // —— 分析维度（5 项多选） ——
    { key: "profile_dim_habits", group: "分析维度", label: "发言习惯", desc: "频率 / 平均长度 / 口头禅与高频表达 / 语气与表情使用 / 活跃时段解读", kind: "bool" },
    { key: "profile_dim_activity", group: "分析维度", label: "活动时间规律", desc: "按小时 + 按星期发言分布图表与简短解读", kind: "bool" },
    { key: "profile_dim_personality", group: "分析维度", label: "性格分析", desc: "基于发言内容的性格推断（外向/理性/幽默等，需给出依据）", kind: "bool" },
    { key: "profile_dim_hobbies", group: "分析维度", label: "兴趣爱好", desc: "从发言主题归纳兴趣领域", kind: "bool" },
    { key: "profile_dim_relations", group: "分析维度", label: "人物关系", desc: "与 Top 互动对象的关系刻画、互动模式推测", kind: "bool" },
    // —— 限流 ——
    { key: "profile_user_cooldown", group: "限流", label: "用户触发冷却", desc: "同一用户两次触发人物分析的最小间隔", kind: "int", unit: "秒", min: 0 },
    { key: "profile_group_cooldown", group: "限流", label: "群触发冷却", desc: "同一群两次触发人物分析的最小间隔", kind: "int", unit: "秒", min: 0 },
    // —— 触发反馈 ——
    { key: "profile_feedback_mode", group: "触发反馈", label: "触发反馈模式", desc: "指令触发后的即时反馈；reaction=贴表情回应（协议端不支持自动降级文字）", kind: "select", options: [["reaction", "贴表情回应（reaction）"], ["text", "文字提示（text）"], ["none", "关闭（none）"]] },
    { key: "profile_feedback_text", group: "触发反馈", label: "触发反馈文案", desc: "text 模式的提示文案；reaction 失败降级时同用；留空回退内置默认", kind: "text" },
    // —— 存储保留 ——
    { key: "profile_keep_days", group: "存储保留", label: "历史保留天数", desc: "历史分析 JSON 保留天数，每日定时清理过期文件", kind: "int", unit: "天", min: 1 },
];

let profileProviders = []; // LLM 提供商列表（可能为空数组 → 下拉仅含回退项）

async function loadProfileSettings() {
    const container = document.getElementById("profileSettingsForm");
    try {
        // 设置与 providers 并行拉取；providers 失败不阻塞表单渲染（降级为空列表）
        const [data, provData] = await Promise.all([
            bridge.apiGet("profile/settings"),
            bridge.apiGet("profile/providers").catch(() => ({ providers: [] })),
        ]);
        profileProviders = provData.providers || [];
        renderProfileForm(data.settings || {});
    } catch (e) {
        container.innerHTML = "";
        container.appendChild(el("div", "loading", "人物分析设置加载失败"));
        showToast("加载人物分析设置失败: " + (e.message || "未知错误"), "error");
    }
}

function renderProfileForm(settings) {
    const container = document.getElementById("profileSettingsForm");
    container.innerHTML = "";
    let currentGroup = null;
    let groupEl = null;
    for (const meta of PROFILE_FIELD_META) {
        if (meta.group !== currentGroup) {
            currentGroup = meta.group;
            groupEl = el("div", "summary-group");
            groupEl.appendChild(el("div", "summary-group-title", meta.group));
            container.appendChild(groupEl);
        }
        groupEl.appendChild(buildProfileItem(meta, settings[meta.key] ?? ""));
    }
    // 「图片渲染」分组：与消息总结共用 summary_t2i_* 配置，仅说明 + 跳转入口
    container.appendChild(buildT2iNoteGroup());
}

// 图片渲染说明分组（PRD 3.6：渲染基础设施配置共享，不新增 profile_t2i_ 冗余项）
function buildT2iNoteGroup() {
    const groupEl = el("div", "summary-group");
    groupEl.appendChild(el("div", "summary-group-title", "图片渲染"));
    const note = el("div", "profile-note");
    note.appendChild(
        el(
            "div",
            "profile-note-text",
            "人物报告的图片渲染（主题模式 / 深浅色时段 / 渲染超时 / CDN 节点顺序）与「消息总结」共用同一套配置（summary_t2i_*），不在此重复列出。调整请前往：消息总结 → 总结设置 → 图片渲染。",
        ),
    );
    const goBtn = el("button", "btn btn-ghost btn-sm", "前往「总结设置」调整 ›");
    goBtn.type = "button";
    goBtn.addEventListener("click", () => {
        // 分区切换后 switchScope 自动激活首个子 tab（总结设置）
        const scopeBtn = document.querySelector('.scope-btn[data-scope="summary"]');
        if (scopeBtn) scopeBtn.click();
    });
    note.appendChild(goBtn);
    groupEl.appendChild(note);
    return groupEl;
}

function buildProfileItem(meta, rawValue) {
    const stacked = meta.kind === "provider_multi";
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
            for (const prov of profileProviders) {
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
            // 复用 v0.3.2 拖拽排序弹窗组件：触发按钮 + 隐藏 input（JSON 字符串数组）
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
            refreshProviderMultiTrigger(trigger, selected, profileProviders);
            trigger.addEventListener("click", () =>
                openProviderMultiModalEx({
                    formSelector: "#profileSettingsForm",
                    key: meta.key,
                    trigger,
                    providers: profileProviders,
                    title: "🔀 备用人物分析模型 · 降级顺序",
                }),
            );
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
    }
    item.appendChild(control);
    return item;
}

// 用接口返回的全量 settings 回填表单（保存/重置成功后复用）
function fillProfileForm(settings) {
    for (const meta of PROFILE_FIELD_META) {
        if (!(meta.key in settings)) continue;
        const ctrl = document.querySelector(`#profileSettingsForm [data-key="${meta.key}"]`);
        if (!ctrl) continue;
        const raw = settings[meta.key];
        if (meta.kind === "bool") ctrl.checked = String(raw).toLowerCase() === "true";
        else if (meta.kind === "provider_multi") {
            // ctrl 为隐藏 input[data-key]，写入 JSON 字符串数组并刷新同列触发按钮摘要
            const ids = parseProviderMultiValue(raw);
            ctrl.value = JSON.stringify(ids);
            const trig = ctrl.parentElement?.querySelector(".provider-multi-trigger");
            if (trig) refreshProviderMultiTrigger(trig, ids, profileProviders);
        } else ctrl.value = String(raw ?? "");
    }
}

// 收集全部 19 项组装 save 载荷；前端基础校验失败返回 null（后端 400 为最终裁决）
function collectProfileSettings() {
    const settings = {};
    for (const meta of PROFILE_FIELD_META) {
        const ctrl = document.querySelector(`#profileSettingsForm [data-key="${meta.key}"]`);
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
            case "provider_multi":
                // 值存于隐藏 input（JSON 字符串数组，顺序即降级顺序，含 stale 失效项）
                settings[meta.key] = parseProviderMultiValue(ctrl.value);
                break;
            default:
                settings[meta.key] = ctrl.value;
        }
    }
    return settings;
}

async function saveProfileSettings() {
    const settings = collectProfileSettings();
    if (!settings) return;
    const btn = document.getElementById("saveProfileBtn");
    btn.disabled = true;
    try {
        // PRD §3.8：保存为 POST /profile/settings（与 summary 的 settings/save 不同，严格按端点契约）
        const data = await bridge.apiPost("profile/settings", { settings });
        fillProfileForm(data.settings || settings);
        showToast("人物分析设置已保存", "success");
    } catch (e) {
        showToast("保存失败: " + (e.message || "未知错误"), "error");
    } finally {
        btn.disabled = false;
    }
}

async function resetProfileAll() {
    if (!confirm(`确定将全部 ${PROFILE_FIELD_META.length} 项人物分析设置恢复为默认值吗？此操作不可撤销。`)) return;
    try {
        const data = await bridge.apiPost("profile/settings/reset", {});
        fillProfileForm(data.settings || {});
        showToast("已恢复全部默认设置", "success");
    } catch (e) {
        showToast("重置失败: " + (e.message || "未知错误"), "error");
    }
}

// ========== 事件绑定：人物分析设置 ==========
function bindProfileSettingsEvents() {
    document.getElementById("saveProfileBtn").addEventListener("click", saveProfileSettings);
    document.getElementById("resetProfileAllBtn").addEventListener("click", resetProfileAll);
    // 备用模型弹窗的 取消/完成/防误触 事件已由 bindSummarySettingsEvents 统一绑定（弹窗 DOM 全局唯一）
}

export { loadProfileSettings, bindProfileSettingsEvents };
