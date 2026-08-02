// 忽略管理分区：每群忽略发送者名单（快捷群芯片 + 名单表 + 增删）
import { bridge, showToast, el } from "./common.js";

let currentIgnoreGroup = null; // 当前正在查看的群号（字符串）

async function loadIgnoreGroups() {
    const container = document.getElementById("ignoreGroupChips");
    try {
        const data = await bridge.apiGet("summary/ignore/groups");
        const groups = data.groups || [];
        container.innerHTML = "";
        if (groups.length === 0) {
            container.appendChild(el("div", "chips-empty", "暂无配置忽略名单的群"));
            return;
        }
        for (const gid of groups) {
            const chip = el("button", "chip", String(gid));
            chip.type = "button";
            chip.dataset.gid = String(gid);
            chip.addEventListener("click", () => {
                document.getElementById("ignoreGroupId").value = String(gid);
                loadIgnoreList(String(gid));
            });
            container.appendChild(chip);
        }
        markActiveIgnoreChip();
    } catch (e) {
        container.innerHTML = "";
        container.appendChild(el("div", "chips-empty", "加载失败: " + (e.message || "未知错误")));
    }
}

function markActiveIgnoreChip() {
    document.querySelectorAll("#ignoreGroupChips .chip").forEach((c) => {
        c.classList.toggle("active", c.dataset.gid === currentIgnoreGroup);
    });
}

async function loadIgnoreList(groupId) {
    if (!/^\d+$/.test(groupId)) {
        showToast("请输入有效的群号（纯数字）", "error");
        return;
    }
    currentIgnoreGroup = groupId;
    markActiveIgnoreChip();
    document.getElementById("ignoreListCard").style.display = "block";
    document.getElementById("ignoreListTitle").textContent = `忽略名单 · 群 ${groupId}`;
    const table = document.getElementById("ignoreTable");
    const empty = document.getElementById("ignoreEmpty");
    const emptyText = empty.querySelector("p");
    table.style.display = "none";
    empty.style.display = "block";
    emptyText.textContent = "加载中...";
    try {
        const data = await bridge.apiGet("summary/ignore", { group_id: groupId });
        const senders = data.senders || [];
        const tbody = document.getElementById("ignoreTableBody");
        tbody.innerHTML = "";
        if (senders.length === 0) {
            emptyText.textContent = "该群暂无忽略的发送者";
            return;
        }
        empty.style.display = "none";
        table.style.display = "table";
        for (const s of senders) {
            const tr = document.createElement("tr");
            tr.appendChild(el("td", null, String(s.sender_id)));
            tr.appendChild(el("td", null, String(s.created_at || "-")));
            const tdAct = document.createElement("td");
            const btn = el("button", "btn btn-ghost btn-sm", "移除");
            btn.type = "button";
            btn.addEventListener("click", () => removeIgnoreSender(groupId, String(s.sender_id)));
            tdAct.appendChild(btn);
            tr.appendChild(tdAct);
            tbody.appendChild(tr);
        }
    } catch (e) {
        emptyText.textContent = "加载失败";
        showToast("加载忽略名单失败: " + (e.message || "未知错误"), "error");
    }
}

async function addIgnoreSender() {
    if (!currentIgnoreGroup) {
        showToast("请先输入群号并点击「加载」", "error");
        return;
    }
    const input = document.getElementById("ignoreSenderId");
    const senderId = input.value.trim();
    if (!/^\d+$/.test(senderId)) {
        showToast("请输入有效的 QQ 号（纯数字）", "error");
        return;
    }
    try {
        await bridge.apiPost("summary/ignore/add", { group_id: currentIgnoreGroup, sender_id: senderId });
        input.value = "";
        showToast(`已将 ${senderId} 加入忽略名单`, "success");
        // 刷新名单 + 群芯片列表（新群首次添加时出现在快捷区）
        await Promise.all([loadIgnoreList(currentIgnoreGroup), loadIgnoreGroups()]);
    } catch (e) {
        // 409 → 后端消息「该发送者已在忽略名单中」，直接展示
        showToast(e.message || "添加失败", "error");
    }
}

async function removeIgnoreSender(groupId, senderId) {
    try {
        await bridge.apiPost("summary/ignore/remove", { group_id: groupId, sender_id: senderId });
        showToast(`已将 ${senderId} 移出忽略名单`, "success");
        await Promise.all([loadIgnoreList(groupId), loadIgnoreGroups()]);
    } catch (e) {
        // 404 → 后端消息「该发送者不在忽略名单中」；刷新名单以同步真实状态
        showToast(e.message || "移除失败", "error");
        await loadIgnoreList(groupId);
    }
}

// ========== 事件绑定：忽略管理 ==========
function bindIgnoreEvents() {
    document.getElementById("ignoreLoadBtn").addEventListener("click", () => {
        loadIgnoreList(document.getElementById("ignoreGroupId").value.trim());
    });
    document.getElementById("ignoreGroupId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") loadIgnoreList(e.target.value.trim());
    });
    document.getElementById("ignoreAddBtn").addEventListener("click", addIgnoreSender);
    document.getElementById("ignoreSenderId").addEventListener("keydown", (e) => {
        if (e.key === "Enter") addIgnoreSender();
    });
}

export { loadIgnoreGroups, bindIgnoreEvents };
