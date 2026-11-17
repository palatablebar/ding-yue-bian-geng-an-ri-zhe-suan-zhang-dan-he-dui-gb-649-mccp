// 采信触发与天数核算：只做请求与状态展示，计算都在服务端。
const state = { busy: false };

function toast(text, tone) {
  const node = document.getElementById("import-toast");
  if (!node) return;
  node.textContent = text;
  node.className = tone === "error" ? "notice" : "toast";
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || `请求失败 ${response.status}`);
  }
  return data;
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || `请求失败 ${response.status}`);
  }
  return data;
}

async function runImport() {
  if (state.busy) return;
  state.busy = true;
  toast("采信中…", "muted");
  const subscriptions = document.getElementById("sub-file")?.value.trim();
  const changes = document.getElementById("chg-file")?.value.trim();
  try {
    const data = await postJSON("/api/subscriptions/import", { subscriptions, changes });
    const totals = data.totals || {};
    const label = data.reused ? "复用" : "完成";
    toast(`${label} ${data.run_id} · 账单 ${totals.invoices ?? 0} 张 · ${totals.invoice_cents ?? 0} 分`,
      "muted");
    setTimeout(() => window.location.assign(`/?run_id=${encodeURIComponent(data.run_id)}`), 600);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.busy = false;
  }
}

async function checkDays() {
  const start = document.getElementById("day-start")?.value.trim();
  const end = document.getElementById("day-end")?.value.trim();
  const node = document.getElementById("day-result");
  if (!node) return;
  try {
    const data = await getJSON(`/api/calendar/days?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
    node.className = "toast";
    node.textContent = `${data.days} 天 · 闰日 ${data.leap ? "是" : "否"}`;
  } catch (error) {
    node.className = "notice";
    node.textContent = error.message;
  }
}

async function runSettlement() {
  if (state.busy) return;
  state.busy = true;
  toast2("结算中…", "muted");
  const contracts = document.getElementById("contract-file")?.value.trim();
  const issued = document.getElementById("issued-file")?.value.trim();
  try {
    const data = await postJSON("/api/settlements/import", { contracts, issued });
    const totals = data.totals || {};
    const label = data.reused ? "复用" : "完成";
    toast2(`${label} ${data.run_id} · 结算单 ${totals.settlements ?? 0} 张 · 最终应收 ${totals.final_receivable_cents ?? 0} 分`,
      "muted");
    setTimeout(() => window.location.assign("/settlements"), 600);
  } catch (error) {
    toast2(error.message, "error");
  } finally {
    state.busy = false;
  }
}

function toast2(text, tone) {
  const node = document.getElementById("settlement-toast");
  if (!node) return;
  node.textContent = text;
  node.className = tone === "error" ? "notice" : "toast";
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("run-import")?.addEventListener("click", runImport);
  document.getElementById("run-settlement")?.addEventListener("click", runSettlement);
  document.getElementById("day-check")?.addEventListener("click", checkDays);
  document.querySelectorAll(".field input").forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        if (document.getElementById("run-import")) runImport();
        if (document.getElementById("run-settlement")) runSettlement();
        if (document.getElementById("day-check")) checkDays();
      }
    });
  });
});
