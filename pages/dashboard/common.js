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

export { bridge, showToast, el };
