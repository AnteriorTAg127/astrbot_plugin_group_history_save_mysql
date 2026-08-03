// v0.4.1 前端导出图片：零依赖 SVG foreignObject 截图
// 插件页 iframe 的 sandbox 为 "allow-scripts allow-forms allow-downloads"（无 allow-same-origin，
// 即 opaque origin），因此：document.styleSheets 不可枚举（无法用 CSSRules 复制样式）、
// fetch 页面自身 CSS 会 CORS 失败、无 allow-modals（window.print 确认框不可用）。
// 故本模块对每个元素用 getComputedStyle 逐条内联计算样式（对 opaque origin 可用），
// CSS 变量一次性写入根节点 style 属性；ECharts canvas 通过 canvas.toDataURL 转 <img> 捕获。
// 支持裁剪：background-image 渐变（linear/radial/conic）、透明度、inset 阴影（box-shadow）。
// 导出 PNG 走 dataURL → <a download> 触发浏览器下载（sandbox 已含 allow-downloads）。
// SVG foreignObject 内的内联样式不受外部样式表影响，天然与页面脱钩，无需临时样式副本。

const SKIP_ELEMENTS = new Set([
    "SCRIPT", "STYLE", "LINK", "META", "TITLE", "NOSCRIPT",
    "BUTTON", "INPUT", "SELECT", "TEXTAREA", "OPTION", "FORM",
]);

// 需要隐藏的交互元素（导出图内无意义）
const HIDE_SELECTORS = [
    ".modal-actions",         // 弹窗底部按钮行（含导出/关闭按钮）
    ".detail-raw summary",    // 「查看 LLM 原始输出」折叠开关
    ".profile-saved-hint",    // 「已保存」提示（仅发起分析页有，防御性）
];

// 透明通道必须保持的元素（压平为 1 后叠加会透出底下内容）
const KEEP_TRANSPARENT = new Set(["IMG", "CANVAS", "SVG", "VIDEO", "IFRAME"]);

// 滚动容器：导出前展开完整内容并允许溢出可见
const SCROLL_EXPAND_SELECTOR = ".detail-body, .table-wrap";

// 需要转 <img> 的原生 canvas（ECharts 实例，DOM 查询用 .pc-echart 定位）
const CANVAS_BOX_SELECTOR = ".pc-echart canvas";

// 这些属性要么布局相关、要么 foreignObject 内不可用，一律不内联
const HARD_BLACKLIST = new Set([
    "display", "grid", "flex", "transition", "animation", "cursor",
    "visibility", "accent-color", "appearance", "caret-color", "user-select",
    "pointer-events", "touch-action", "will-change", "backdrop-filter",
    "text-overflow", "overflow", "overflow-x", "overflow-y", "gap",
]);

// 样式名 → 自身引用（em/rem/% 类相对单位在 foreignObject 的 0 尺寸 viewport 下会解析异常）
const SELF_REF = new Set([
    "height", "min-height", "max-height", "width", "min-width", "max-width",
    "font", "font-size", "line-height", "border", "border-width",
    "border-radius", "padding", "margin", "top", "left", "right", "bottom",
    "translate", "transform", "gap", "row-gap", "column-gap", "text-indent",
    "letter-spacing", "word-spacing", "background-size", "stroke-width",
    "flex-basis", "inset", "vertical-align",
]);

// 值里的属性名占位符（如 "var(--x)"、"attr(...)"、"env(...)"），替换为自身当前值
const PROP_RE = /(?:var|attr|env)\(\s*([a-z0-9-]+)/gi;

// 伪元素能识别的属性白名单（其余无法在 SVG 内重现）
const PSEUDO_STYLE_PROPS = new Set([
    "content", "background", "background-color", "background-image",
    "background-position", "background-size", "background-repeat",
    "color", "font-family", "font-size", "font-weight", "font-style",
    "border", "border-radius", "width", "height", "line-height",
    "text-align", "display", "box-sizing", "padding", "margin",
    "position", "top", "left", "right", "bottom", "transform", "z-index",
]);

// 主题变量集：导出图跟随页面当前主题（亮/暗）
function themeVars() {
    const root = getComputedStyle(document.documentElement);
    const names = [
        "bg", "surface", "surface-hover", "text", "text-2", "text-3",
        "border", "primary", "primary-light", "primary-hover",
        "success", "success-light", "danger", "danger-light", "danger-hover",
    ];
    const vars = {};
    for (const n of names) vars[`--${n}`] = root.getPropertyValue(`--${n}`).trim();
    return vars;
}

function isTransparent(style, prop) {
    return /^rgba?\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\)$/.test(style.getPropertyValue(prop));
}

function blurOf(v) {
    const m = /([\d.]+)px/.exec(v);
    return m ? parseFloat(m[1]) : 0;
}

// box-shadow 只有 inset（内阴影）能被保留；外阴影无法重现，仅统计最大 blur
function prepareBoxShadow(style) {
    let outerBlur = 0;
    const parts = [];
    for (const sh of style.boxShadow.split(",")) {
        const t = sh.trim();
        if (!t || t === "none") continue;
        if (t.startsWith("inset ")) parts.push(t);
        else outerBlur = Math.max(outerBlur, blurOf(t));
    }
    return { outerBlur, value: parts.length ? parts.join(", ") : "none" };
}

function resolveProp(value, selfVal) {
    if (!value || !selfVal || typeof value !== "string") return value;
    return value.replace(PROP_RE, (_m, p) => {
        const v = selfVal.getPropertyValue(p);
        return v || "initial";
    });
}

// 归一化背景：仅保留渐变层（url() 图片内联后会把 foreignObject 从 SVG 派系逐出）
function filterBackground(style, out, notes) {
    const img = style.backgroundImage;
    if (!img || img === "none") return;
    const kept = [];
    for (const layer of img.split(",")) {
        const t = layer.trim();
        if (!t) continue;
        if (/^(?:linear|radial|conic)-gradient\(/.test(t)) {
            if (isTransparent(style, "background-color")) {
                const color = style.backgroundColor;
                if (color && color !== "transparent") {
                    kept.push(color + " " + t);
                    continue;
                }
            }
            kept.push(t);
        } else if (/^url\(/.test(t)) {
            notes.push("背景图（url 图片）未导出");
        }
    }
    if (kept.length) out["background-image"] = kept.join(", ");
}

// 伪元素物化为真实子元素（内容/样式从 getComputedStyle(el, '::before'/'::after') 提取）。
// 克隆树不在渲染树中，无法用 offsetParent 判断占位，故一律按计算样式物化：
// 绝对定位的伪元素（如开关 thumb）靠复制的 position/top/left 呈现，流内伪元素自然参与布局。
function materializePseudo(el, cls, ctx) {
    const p = getComputedStyle(el, cls);
    const content = p.getPropertyValue("content");
    if (!content || content === "none" || content === "normal" || content === "") return null;
    const node = document.createElement("span");
    node.className = "cap-pseudo " + cls.slice(2);
    const inner = document.createElement("span");
    inner.className = "cap-pseudo-text";
    if (content.startsWith('"') && content.endsWith('"')) {
        inner.textContent = content.slice(1, -1);
    } else if (content === '""' || content === "''") {
        inner.textContent = "";
    } else {
        // counter(name) 序号：按父级 counter-reset + 逐元素递增近似还原
        const cm = /counter\(\s*([a-z0-9-]+)/i.exec(content);
        if (!cm) return null; // attr() 等其他内容物化不了
        ctx.counters[cm[1]] = (ctx.counters[cm[1]] || 0) + 1;
        inner.textContent = String(ctx.counters[cm[1]]);
    }
    node.appendChild(inner);
    for (const prop of PSEUDO_STYLE_PROPS) {
        const v = p.getPropertyValue(prop);
        if (v && v !== "initial" && v !== "none" && v !== "auto") {
            if (prop === "content") continue;
            node.style.setProperty(prop, v);
        }
    }
    return node;
}

function collectMeta(section, el, css, meta) {
    meta.count++;
    if (css.backgroundImage && css.backgroundImage !== "none") meta.bgImages++;
    if (css.backgroundClip === "text") meta.bgClipText++;
    if (css.mixBlendMode && css.mixBlendMode !== "normal") meta.mixBlend++;
    if (css.filter && css.filter !== "none") meta.filter++;
    if (css.clipPath && css.clipPath !== "none") meta.clipPath++;
    if (css.fontFamily && css.fontFamily !== "none") {
        meta.fonts.add(css.fontFamily.split(",")[0].trim().replace(/^["']|["']$/g, ""));
    }
    // 统计被裁剪的滚动内容，用于 toast 提示
    let node = el.parentElement;
    while (node && node !== section) {
        if (node.scrollWidth > node.clientWidth + 2) meta.scrollX = true;
        if (node.scrollHeight > node.clientHeight + 2) meta.scrollY = true;
        node = node.parentElement;
    }
}

// 深度优先转换节点：返回带内联样式的克隆子树；返回 null 表示整棵被跳过
function cloneNode(section, el, ctx, notes, meta) {
    if (el.nodeType === Node.TEXT_NODE) return el.cloneNode();
    if (el.nodeType !== Node.ELEMENT_NODE) return null;
    if (SKIP_ELEMENTS.has(el.tagName)) return null;

    if (el.tagName === "CANVAS") {
        const img = document.createElement("img");
        try {
            img.setAttribute("src", el.toDataURL("image/png"));
            img.setAttribute("width", String(el.width || 0));
            img.setAttribute("height", String(el.height || 0));
        } catch { /* 画布不可读（被污染）时放弃 */ }
        return img;
    }
    if (el.tagName === "IMG") return cloneImg(el);
    if (el.tagName === "SVG") return null; // 页面无独立 SVG 元素，防御性跳过

    const clone = el.cloneNode(false);
    clone.removeAttribute("id");
    // 克隆后元素脱离文档，getComputedStyle 失效，必须提前取
    const css = getComputedStyle(el);
    const isRoot = el === section;

    // 交互元素整棵隐藏
    if (!isRoot && HIDE_SELECTORS.some((s) => el.matches && el.matches(s))) return null;
    if (css.display === "none" || css.visibility === "hidden") return null;

    const boxShadow = prepareBoxShadow(css);
    const targetStyle = clone.style;
    for (let i = 0; i < css.length; i++) {
        const prop = css.item(i);
        if (HARD_BLACKLIST.has(prop)) continue;
        let value = css.getPropertyValue(prop);
        if (!value) continue;
        if (SELF_REF.has(prop)) value = resolveProp(value, css);
        if (prop === "box-shadow") value = boxShadow.value;
        if (prop === "background-image" || prop === "background") continue; // 统一走 filterBackground
        if (isTransparent(css, prop)) {
            // 透明背景不写死；只有必须保持透明的元素（图片/画布）才保留
            if (!KEEP_TRANSPARENT.has(el.tagName)) continue;
        }
        try {
            targetStyle.setProperty(prop, value);
        } catch { /* 非法的内联值忽略 */ }
    }
    filterBackground(css, targetStyle, notes);

    collectMeta(section, el, css, meta);

    for (const child of el.childNodes) {
        if (child.nodeType === Node.TEXT_NODE && !child.textContent.trim()) continue;
        const c = cloneNode(section, child, ctx, notes, meta);
        if (c) clone.appendChild(c);
    }

    // 伪元素物化（排行序号圆点、金冠高亮等）：::before 在内容前，::after 在内容后
    const before = materializePseudo(el, "::before", ctx);
    if (before) clone.insertBefore(before, clone.firstChild);
    const after = materializePseudo(el, "::after", ctx);
    if (after) clone.appendChild(after);
    return clone;
}

function cloneImg(el) {
    const clone = el.cloneNode(false);
    clone.removeAttribute("id");
    // 外链图导出后无法访问，去掉 src 避免破图；data: URL 原样保留
    if (el.currentSrc && el.currentSrc.startsWith("data:")) {
        clone.setAttribute("src", el.currentSrc);
    } else {
        clone.removeAttribute("src");
    }
    const [w, h] = nativeSize(el);
    if (w) clone.setAttribute("width", String(w));
    if (h) clone.setAttribute("height", String(h));
    return clone;
}

// IMG 的原生尺寸（避免 SVG 里按 CSS 尺寸放大的模糊）
function nativeSize(img) {
    if (img.naturalWidth && img.naturalHeight) {
        return [img.naturalWidth, img.naturalHeight];
    }
    const probe = new Image();
    probe.src = img.currentSrc || img.src;
    return [probe.naturalWidth || img.width, probe.naturalHeight || img.height];
}

// 导出前 DOM 预处理：滚动容器展开（高度撑满 + 溢出可见）+ 图表 canvas 全尺寸固定
// 返回还原函数（页面状态被临时改动，导出后必须还原）
function prepareDom(section) {
    const restores = [];
    section.querySelectorAll(SCROLL_EXPAND_SELECTOR).forEach((el) => {
        const prevH = el.style.height;
        const prevOverflow = el.style.overflow;
        const prevOverflowY = el.style.overflowY;
        if (el.scrollHeight > el.clientHeight + 2) {
            el.style.height = el.scrollHeight + "px";
        }
        el.style.overflow = "visible";
        restores.push(() => {
            el.style.height = prevH;
            el.style.overflow = prevOverflow;
            el.style.overflowY = prevOverflowY;
        });
    });
    section.querySelectorAll(CANVAS_BOX_SELECTOR).forEach((c) => {
        const prevW = c.getAttribute("width");
        const prevH = c.getAttribute("height");
        const rect = c.getBoundingClientRect();
        if (rect.width > 0 && Math.abs(rect.width - c.width) > 1) {
            c.setAttribute("width", String(Math.round(rect.width)));
        }
        if (rect.height > 0 && Math.abs(rect.height - c.height) > 1) {
            c.setAttribute("height", String(Math.round(rect.height)));
        }
        restores.push(() => {
            if (prevW) c.setAttribute("width", prevW);
            else c.removeAttribute("width");
            if (prevH) c.setAttribute("height", prevH);
            else c.removeAttribute("height");
        });
    });
    return restores;
}

// 主体流程：构建 SVG → Blob URL → 画到 canvas → PNG dataURL
async function captureSection(section, opts = {}) {
    const notes = [];
    const meta = {
        count: 0, bgImages: 0, bgClipText: 0, mixBlend: 0,
        filter: 0, clipPath: 0, fonts: new Set(), scrollX: false, scrollY: false,
    };
    const restores = prepareDom(section);
    try {
        const ctx = { counters: {} };
        const clone = cloneNode(section, section, ctx, notes, meta);
        if (!clone) return null;

        // 主题变量注入克隆根（导出图跟随当前主题）
        for (const [k, v] of Object.entries(themeVars())) {
            if (v) clone.style.setProperty(k, v);
        }

        const pad = 14;
        const width = Math.max(1, Math.ceil(section.scrollWidth) + pad * 2);
        const height = Math.max(1, Math.ceil(section.scrollHeight) + pad * 2);

        const xmlns = "http://www.w3.org/1999/xhtml";
        const xml = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
<foreignObject width="100%" height="100%" x="${pad}" y="${pad}">
<body xmlns="${xmlns}">${clone.outerHTML}</body>
</foreignObject></svg>`;
        const svgUrl = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);

        // 直接用 data: URL 作为图片源（不经 fetch→blob：blob 是 opaque origin，
        // drawImage 后会污染 canvas 导致 toDataURL 抛 SecurityError）
        const img = new Image();
        await new Promise((resolve, reject) => {
            img.onload = resolve;
            img.onerror = () => reject(new Error("SVG 渲染失败"));
            img.src = svgUrl;
        });

        // 超大图限制边长，防 canvas 溢出
        const scale = Math.min(2, 8000 / Math.max(width, height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(width * scale);
        canvas.height = Math.ceil(height * scale);
        const g = canvas.getContext("2d");
        g.fillStyle = "#ffffff";
        g.fillRect(0, 0, canvas.width, canvas.height); // 白底（导出图不透明）
        g.drawImage(img, 0, 0, canvas.width, canvas.height);

        const dataUrl = canvas.toDataURL("image/png");
        notes.push(`宽 ${width}px · 高 ${height}px · ${meta.count} 个元素`);
        if (meta.bgImages) notes.push(`${meta.bgImages} 处背景渐变`);
        if (meta.bgClipText) notes.push("文字渐变背景未导出（background-clip:text）");
        if (meta.mixBlend) notes.push(`${meta.mixBlend} 处混合模式未导出`);
        if (meta.filter) notes.push(`${meta.filter} 处滤镜未导出`);
        if (meta.clipPath) notes.push("裁剪路径未导出");
        if (meta.scrollX) notes.push("横向滚动内容已展开");
        if (meta.scrollY) notes.push("纵向滚动内容已展开");
        return { dataUrl, width: canvas.width, height: canvas.height, notes };
    } finally {
        restores.forEach((fn) => fn());
    }
}

// 导出入口：捕获弹窗内的详情正文并触发下载
export async function exportSectionAsPng(section, filename) {
    if (!section) return;
    const result = await captureSection(section, {});
    if (!result) {
        showExportToast("导出失败：内容为空或渲染异常", "error");
        return;
    }
    const a = document.createElement("a");
    a.href = result.dataUrl;
    a.download = filename + ".png";
    document.body.appendChild(a);
    a.click();
    a.remove();
    if (result.notes.length) showExportToast(result.notes.join(" · "));
}

function showExportToast(msg, type = "") {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.className = "toast show" + (type ? ` ${type}` : "");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.className = "toast"; }, 3200);
}
