// 终止结算明细：点行取 /api/settlements/<结算单号>，显示承诺、逐张冲抵、本期结算。
const settlementState = { cache: new Map(), loading: false };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function buildTable(headers, rows) {
  const table = el("table", "table");
  const thead = el("thead");
  const headRow = el("tr");
  headers.forEach(([label, numeric]) => headRow.appendChild(
    el("th", numeric ? "num" : "", label)));
  thead.appendChild(headRow);
  const tbody = el("tbody");
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach(([value, numeric]) => tr.appendChild(
      el("td", numeric ? "num" : "", value)));
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  const wrap = el("div", "table-wrap");
  wrap.appendChild(table);
  return wrap;
}

function renderSettlement(body, data) {
  body.replaceChildren();
  const head = el("div", "block-head");
  head.appendChild(el("h3", "", "结算说明"));
  head.appendChild(el("span", "sub",
    `停机 ${data.stopped_on} · 未履行 [${data.unserved_start}, ${data.unserved_end}) · `
    + `本期结算 ${data.payable} 元 · 最终应收 ${data.final_receivable} 元`));
  body.appendChild(head);
  body.appendChild(buildTable(
    [["合同承诺(元)", true], ["已出账单(元)", true], ["本期结算(元)", true],
     ["最终应收(元)", true], ["已服务天", true], ["未履行天", true]],
    [[[data.commitment, true], [data.issued, true], [data.payable, true],
      [data.final_receivable, true], [data.served_days, true],
      [data.unserved_days, true]]],
  ));
  body.appendChild(el("h3", "", "结算成分"));
  body.appendChild(buildTable(
    [["成分", false], ["来源", false], ["账期", false], ["天数", true], ["金额(元)", true]],
    data.lines.map((row) => [
      [row.kind === "commitment" ? "合同承诺" : "已出账单冲抵", false],
      [row.source, false],
      [`[${row.period_start}, ${row.period_end})`, false],
      [row.days, true],
      [row.amount, true],
    ]),
  ));
}

async function loadSettlement(settlementId) {
  if (settlementState.loading) return;
  const dialog = document.getElementById("settlement-dialog");
  const body = document.getElementById("settlement-body");
  const title = document.getElementById("settlement-title");
  if (!dialog || !body || !title) return;
  settlementState.loading = true;
  title.textContent = settlementId;
  body.replaceChildren(el("p", "empty", "读取中…"));
  try {
    let data = settlementState.cache.get(settlementId);
    if (!data) {
      const response = await fetch(
        `/api/settlements/${encodeURIComponent(settlementId)}`,
        { headers: { Accept: "application/json" } });
      data = await response.json();
      if (!response.ok) throw new Error(data.message || `请求失败 ${response.status}`);
      settlementState.cache.set(settlementId, data);
    }
    renderSettlement(body, data);
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
  } catch (error) {
    body.replaceChildren(el("p", "notice", error.message));
  } finally {
    settlementState.loading = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("tr[data-settlement]").forEach((row) => {
    const open = () => loadSettlement(row.dataset.settlement);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
});
