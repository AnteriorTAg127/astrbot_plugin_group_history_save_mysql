// 跨模块共享：bridge 引用与通用 DOM 工具
const bridge = window.AstrBotPluginPage;

// 轻提示（全局唯一 #toast 元素）
function showToast(msg, type = "") {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = "toast show" + (type ? ` ${type}` : "");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.className = "toast"; }, 2600);
}

// 通用节点构建器：所有动态文本一律走 textContent，杜绝 HTML 注入（XSS 防护）
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

// 二次确认弹窗（Promise<boolean>）。
// 插件页 iframe 的 sandbox 未含 allow-modals，原生 confirm() 会被静默禁用并恒返回 false，
// 故破坏性操作一律改用本模态框确认（复用 #confirmModal 结构，文本走 textContent 防 XSS）。
function confirmDialog(message, { title = "⚠️ 确认操作", okText = "确认", danger = true } = {}) {
    return new Promise((resolve) => {
        const mask = document.getElementById("confirmModal");
        if (!mask) {
            resolve(window.confirm(message));
            return;
        }
        const titleEl = document.getElementById("confirmModalTitle");
        const msgEl = document.getElementById("confirmModalMessage");
        const okBtn = document.getElementById("confirmOkBtn");
        const cancelBtn = document.getElementById("confirmCancelBtn");
        titleEl.textContent = title;
        msgEl.textContent = message;
        okBtn.textContent = okText;
        okBtn.className = danger ? "btn btn-danger" : "btn btn-primary";
        mask.style.display = "flex";

        const done = (result) => {
            mask.style.display = "none";
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            mask.removeEventListener("click", onMask);
            resolve(result);
        };
        const onOk = () => done(true);
        const onCancel = () => done(false);
        const onMask = (e) => {
            if (e.target === mask) done(false);
        };
        okBtn.addEventListener("click", onOk);
        cancelBtn.addEventListener("click", onCancel);
        mask.addEventListener("click", onMask);
    });
}

export { bridge, showToast, el, confirmDialog };
