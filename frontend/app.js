"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = v => (v === null || v === undefined || v === "") ? "–" : "₹" + Number(v).toLocaleString("en-IN", { minimumFractionDigits: Number.isInteger(+v) ? 0 : 2, maximumFractionDigits: 2 });
const state = { tab: "chat", sid: null, status: null, runId: null, batchId: null, filter: "all", draft: null };
const TABS = [["chat", "Request (chat)"], ["work", "Buyer workspace"], ["policy", "Policy"], ["suppliers", "Preferred suppliers"], ["contracts", "Contracts"], ["audit", "Audit"], ["setup", "Setup"]];

function user() { const [name, role] = $("#user").value.split("|"); return { name, role }; }
async function api(path, opts = {}) {
  const u = user(); const h = { "X-User": u.name, "X-Role": u.role, ...(opts.headers || {}) };
  let body = opts.body;
  if (body && !(body instanceof FormData) && typeof body === "object") { body = JSON.stringify(body); h["Content-Type"] = "application/json"; }
  const r = await fetch("/api" + path, { method: opts.method || (body ? "POST" : "GET"), headers: h, body });
  if (!r.ok) { let d = r.statusText; try { d = (await r.json()).detail || d; } catch { } throw new Error(typeof d === "string" ? d : JSON.stringify(d)); }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r.text();
}
function toast(msg, bad) { const t = document.createElement("div"); t.textContent = msg; if (bad) t.style.background = "var(--bad)"; $("#toast").append(t); setTimeout(() => t.remove(), bad ? 7000 : 3500); }
const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };
const routeChip = r => `<span class="chip ${/PO$/.test(r) ? "ok" : r === "NOT_PROCESSED" ? "bad" : "warn"}">${esc(r)}</span>`;
function download(name, text) { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" })); a.download = name; a.click(); }

function nav() {
  $("#nav").innerHTML = TABS.map(([k, l]) => `<a data-t="${k}" class="${state.tab === k ? "on" : ""}">${l}</a>`).join("");
  $("#nav").onclick = e => { const t = e.target.dataset.t; if (t) { state.tab = t; render(); } };
}
async function render() {
  nav();
  try { state.status = await api("/status"); } catch { }
  const s = state.status;
  if (s) {
    $("#chips").innerHTML = `<span class="chip ${s.llm ? "ok" : ""}">${s.llm ? "AI assist on" : "rules mode"}</span>`;
    $("#banner").innerHTML = s.master.items ? "" : `<div class="b">No master data loaded yet. Go to Setup and upload the item master, suppliers, PO history and contracts.</div>`;
  }
  const v = $("#view"); v.innerHTML = "";
  await ({ chat: viewChat, work: viewWork, policy: viewPolicy, suppliers: viewSuppliers, contracts: viewContracts, audit: viewAudit, setup: viewSetup }[state.tab])(v);
}
$("#user").onchange = () => { state.sid = null; render(); };

// ---------------------------------------------------------------- chat
async function viewChat(v) {
  v.innerHTML = `<div class="card chat"><div class="msgs" id="msgs"></div><div class="quick" id="quick"></div>
  <form class="composer" id="cf"><input type="text" id="ci" placeholder="e.g. need 200 nitrile gloves for Chakan by next Friday" autocomplete="off"><button>Send</button></form></div>`;
  const draw = (hist, quick) => {
    $("#msgs").innerHTML = (hist.length ? hist : [{ role: "assistant", content: "Hi! Tell me what you need, e.g. “need 200 nitrile gloves for Chakan by next Friday”. I'll match the item, ask for anything missing, confirm, and raise the PR." }])
      .map(m => `<div class="m ${m.role}">${esc(m.content)}</div>`).join("");
    $("#msgs").scrollTop = 1e9;
    $("#quick").innerHTML = (quick || []).map(q => `<button type="button">${esc(q)}</button>`).join("");
    $("#quick").onclick = e => { if (e.target.tagName === "BUTTON") send(e.target.textContent); };
  };
  const send = guard(async text => {
    if (!text.trim()) return; $("#ci").value = "";
    const r = await api("/chat", { body: { session_id: state.sid, message: text } });
    state.sid = r.session_id; draw(r.history, r.quick_replies);
  });
  $("#cf").onsubmit = e => { e.preventDefault(); send($("#ci").value); };
  if (state.sid) { const h = await api("/chat/" + state.sid); draw(h.history, []); } else draw([], []);
  $("#ci").focus();
}

// ---------------------------------------------------------------- buyer workspace
async function viewWork(v) {
  const batches = await api("/batches");
  if (!state.batchId && batches.length) state.batchId = (batches.find(b => b.kind === "live" && b.latest_run) || batches[0]).id;
  const b = batches.find(x => x.id == state.batchId);
  v.innerHTML = `<div class="card"><div class="row">
    <b>Batch</b><select id="bsel">${batches.map(x => `<option value="${x.id}" ${x.id == state.batchId ? "selected" : ""}>#${x.id} ${esc(x.name)} (${x.pr_lines} lines${x.pending ? ", " + x.pending + " pending" : ""})</option>`).join("")}</select>
    <button id="reproc" class="sec">Reprocess with current policy</button>
    <button id="exp" class="ghost">Download decisions CSV</button><span class="grow"></span>
    <label class="mute">Upload PR batch CSV</label><input type="file" id="up" accept=".csv"></div></div><div id="runbox"></div>`;
  $("#bsel").onchange = e => { state.batchId = e.target.value; state.runId = null; render(); };
  $("#up").onchange = guard(async e => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    const r = await api("/process-batch", { method: "POST", body: fd });
    state.batchId = r.batch_id; state.runId = r.run_id; toast(`Processed ${r.decisions.length} lines under policy v${r.policy_versions.join(", ")}`); render();
  });
  $("#reproc").onclick = guard(async () => { if (!state.batchId) return; const r = await api(`/batches/${state.batchId}/reprocess`, { method: "POST" }); state.runId = r.run_id; toast("Reprocessed under policy v" + r.policy_versions.join(", ")); render(); });
  const runId = state.runId && b && b.latest_run === state.runId ? state.runId : (b && b.latest_run);
  state.runId = runId;
  $("#exp").onclick = guard(async () => { if (runId) download(`decisions_run${runId}.csv`, await api(`/runs/${runId}/export.csv`)); });
  if (!runId) { $("#runbox").innerHTML = `<div class="card mute">Nothing here yet. Requests raised via chat appear here, or upload a PR batch CSV above.</div>`; return; }
  const data = await api("/runs/" + runId);
  drawRun(data);
}
function drawRun(d) {
  const reqs = d.requirements, f = state.filter;
  const routes = {}; d.lines.forEach(l => routes[l.route] = (routes[l.route] || 0) + 1);
  const shown = reqs.filter(r => f === "all" || r.route === f || (f === "pending" && r.status === "pending"));
  $("#runbox").innerHTML = `<div class="card"><div class="row"><b>Run #${d.run.id}</b><span class="mute">${esc(d.run.created_at)} · policy v${esc(d.run.policy_versions)}</span><span class="grow"></span>
    ${Object.entries(routes).map(([k, n]) => `<span class="chip ${/PO$/.test(k) ? "ok" : k === "NOT_PROCESSED" ? "bad" : "warn"}">${esc(k)} ${n}</span>`).join("")}</div>
    <div class="row" style="margin-top:10px"><label>Filter</label><select id="flt"><option value="all">All requirements</option><option value="pending" ${f === "pending" ? "selected" : ""}>Pending approval</option>
    ${[...new Set(reqs.map(r => r.route))].map(r => `<option ${f === r ? "selected" : ""}>${r}</option>`).join("")}</select><span class="mute">${shown.length} of ${reqs.length} requirements</span></div></div>
  <div class="card" style="padding:0;overflow:auto"><table><thead><tr><th>Req</th><th>Item</th><th>Plant</th><th class="num">Qty</th><th class="num">Value</th><th>Route</th><th>Supplier(s)</th><th>PO</th><th>Status</th></tr></thead><tbody>
  ${shown.map(r => `<tr class="click" data-g="${r.group_id}"><td>${r.group_id}<div class="mute">${r.line_keys.length} line${r.line_keys.length > 1 ? "s" : ""}</div></td>
    <td>${esc(r.item_code)}<div class="mute">${esc(r.item_description)}</div></td><td>${r.plant}</td><td class="num">${r.quantity}</td><td class="num">${inr(r.value)}</td>
    <td>${routeChip(r.route)}${r.is_emergency ? ' <span class="chip bad">Emergency</span>' : ""}</td>
    <td>${esc(r.supplier_id || r.suggested_supplier_ids.join(", "))}</td><td>${esc(r.po_id || "")}</td>
    <td><span class="chip ${r.status === "pending" ? "" : "ok"}">${r.status}</span>${r.doc_ref ? `<div class="mute">${r.doc_ref}</div>` : ""}</td></tr>`).join("")}</tbody></table>
  ${d.lines.filter(l => l.route === "NOT_PROCESSED").map(l => `<div style="padding:10px 14px" class="mute">⚠ ${l.pr_no}/${l.pr_line} not processed — ${esc(l.reason)}</div>`).join("")}</div>`;
  $("#flt").onchange = e => { state.filter = e.target.value; drawRun(d); };
  $("#runbox tbody").onclick = e => { const tr = e.target.closest("tr"); if (tr) drawer(d, reqs.find(r => r.group_id === tr.dataset.g)); };
}
function drawer(d, r) {
  document.querySelector(".drawer")?.remove();
  const el = document.createElement("div"); el.className = "drawer";
  const ca = r.contract_assessment || { applicable: [], notes: [] };
  el.innerHTML = `<div class="row"><h2 class="grow">${r.group_id} · ${esc(r.item_code)} ${routeChip(r.route)}</h2><button class="ghost" id="x">Close</button></div>
  <div class="why">${esc(r.reason)}</div>
  <h3>Decision</h3><div class="kv"><div>Decision</div><div>${r.decision} via ${r.route} (clause ${r.clause}, policy v${r.policy_version})</div>
  <div>Item</div><div>${esc(r.item_description)} · ${esc(r.category || "new item")}</div><div>Value</div><div>${inr(r.value)} (${esc(r.valuation_source.text)})</div>
  <div>Supplier</div><div>${esc(r.supplier_id || "—")} ${r.unit_price ? "@ " + inr(r.unit_price) : ""} ${r.contract_id ? "· contract " + r.contract_id : ""}</div>
  <div>Suggested</div><div>${esc(r.suggested_supplier_ids.join(", ") || "—")}</div><div>PR lines</div><div>${r.line_keys.map(k => k.join("/")).join(", ")}</div>
  <div>Requirement date</div><div>${r.requirement_date}, need by ${r.need_by}</div><div>PO</div><div>${esc(r.po_id || "—")}</div></div>
  ${ca.applicable.length || ca.notes.length ? `<h3>Rate contracts</h3>${ca.applicable.map(a => `<div>${a.contract_id} · ${a.supplier_id} · ${inr(a.rate)}/unit · remaining ${inr(a.remaining)} of ${inr(a.ceiling)} · needs ${inr(a.cost)} → <b>${a.has_room ? "usable" : "ceiling exhausted"}</b></div>`).join("")}${ca.notes.map(n => `<div class="mute">${esc(n)}</div>`).join("")}` : ""}
  ${r.preferred_top.length ? `<h3>Preferred suppliers (top ${r.preferred_top.length})</h3><table><tr><th>#</th><th>Supplier</th><th class="num">Score</th><th class="num">POs</th><th class="num">On-time</th><th class="num">Quality</th><th class="num">Ref price</th></tr>${r.preferred_top.map(p => `<tr><td>${p.rank}</td><td>${p.supplier_id}<div class="mute">${esc(p.supplier_name)}</div></td><td class="num">${p.score}</td><td class="num">${p.po_count}</td><td class="num">${p.on_time_pct}%</td><td class="num">${p.quality}</td><td class="num">${inr(p.reference_price)}</td></tr>`).join("")}</table>` : ""}
  <h3>Facts the policy saw</h3><pre>${esc(JSON.stringify(r.facts, null, 1))}</pre>
  ${r.overrides.length ? `<h3>Overrides</h3>${r.overrides.map(o => `<div class="rule">${esc(o.by)} · ${esc(o.at)}<br>${esc(o.comment)}<br><span class="mute">→ ${esc(JSON.stringify(o.new_decision))}</span></div>`).join("")}` : ""}
  <div id="act"></div>`;
  document.body.append(el); $("#x", el).onclick = () => el.remove();
  if (r.status === "pending") {
    $("#act", el).innerHTML = `<h3>Buyer action</h3><div class="row"><button id="ap">${r.decision === "PO" ? "Approve PO" + (r.po_id ? " " + r.po_id : "") : "Approve " + r.route}</button><button id="ov" class="ghost">Override…</button></div><div id="ovf"></div>`;
    $("#ap", el).onclick = guard(async () => { const o = await api(`/runs/${d.run.id}/requirements/${r.group_id}/approve`, { method: "POST" }); toast(`Created ${o.doc_no}`); el.remove(); render(); });
    $("#ov", el).onclick = () => {
      $("#ovf", el).innerHTML = `<div class="grid2" style="margin-top:8px"><label>Decision<select id="od"><option>PO</option><option>SOURCING_REQUEST</option></select></label><label>Route<input id="or" type="text" value="SPOT_PO"></label>
      <label>Supplier ID<input id="os" type="text" placeholder="S0xx"></label><label>Unit price<input id="op" type="number" step="0.01"></label></div>
      <label>Reason (required, kept with the original decision)<textarea id="oc" rows="3"></textarea></label><button id="osv" class="bad">Save override</button>`;
      $("#osv", el).onclick = guard(async () => {
        const o = await api(`/runs/${d.run.id}/requirements/${r.group_id}/override`, { body: { decision: $("#od").value, route: $("#or").value, supplier_id: $("#os").value.trim().toUpperCase() || null, unit_price: $("#op").value, comment: $("#oc").value } });
        toast("Override saved as " + o.doc_no); el.remove(); render();
      });
    };
  } else $("#act", el).innerHTML = `<h3>Status</h3><div>${r.status} by ${esc(r.decided_by)} at ${esc(r.decided_at)} → ${esc(r.doc_ref)}</div>`;
}

// ---------------------------------------------------------------- policy
async function viewPolicy(v) {
  const pol = await api("/policies");
  const cur = pol.find(p => p.in_force_today) || pol.find(p => p.status === "approved");
  const draft = pol.find(p => p.status === "draft");
  v.innerHTML = `<div class="grid2"><div>
  <div class="card"><h2>Policy in force: v${esc(cur?.version)} <span class="mute">(effective ${esc(cur?.effective_date)})</span></h2>
  ${(cur?.doc.rules || []).map((r, i) => `<div class="rule"><span class="mute">${i + 1}.</span> <b>[${esc(r.clause)}] ${esc(r.name)}</b><br>${esc(ruleText(r))}</div>`).join("")}
  <h3>Parameters</h3><div class="kv">${Object.entries(cur?.doc.parameters || {}).map(([k, x]) => `<div>${k}</div><div>${x}</div>`).join("")}</div></div>
  <div class="card"><h2>All versions</h2><table><tr><th>Version</th><th>Status</th><th>Effective</th><th>Source</th><th>By</th></tr>${pol.map(p => `<tr><td>v${esc(p.version)}</td><td><span class="chip ${p.status === "approved" ? "ok" : p.status === "draft" ? "warn" : "bad"}">${p.status}</span>${p.in_force_today ? '<span class="chip">in force</span>' : ""}</td><td>${p.effective_date}</td><td>${esc(p.source_kind)} ${esc(p.source_name || "")}</td><td>${esc(p.decided_by || p.created_by)}</td></tr>`).join("")}</table></div></div>
  <div><div class="card"><h2>Change the policy</h2><p class="mute">Upload an amendment (PDF, Word, text) or describe it in words. Nothing changes until Procurement approves the draft. No code is touched.</p>
  <div class="row"><input type="file" id="pf" accept=".pdf,.docx,.txt,.md,.json"><label>Effective <input type="date" id="pe"></label><button id="pu">Upload</button></div>
  <h3>…or describe it</h3><textarea id="pt" rows="4" placeholder="e.g. Raise the spot PO limit to ₹3,00,000 from 1 Dec 2026. At Chakan, Electrical Spares above ₹1 lakh must always go to RFQ."></textarea>
  <div class="row" style="margin-top:6px"><button id="pd" class="sec">Interpret</button></div></div>
  <div id="draft"></div></div></div>`;
  $("#pu").onclick = guard(async () => { const f = $("#pf").files[0]; if (!f) throw new Error("Choose a file first"); const fd = new FormData(); fd.append("file", f); if ($("#pe").value) fd.append("effective_date", $("#pe").value); const r = await api("/policies/upload", { method: "POST", body: fd }); toast(`Draft v${r.version} created (${r.method})`); render(); });
  $("#pd").onclick = guard(async () => { const r = await api("/policies/describe", { body: { text: $("#pt").value, effective_date: $("#pe").value || undefined } }); toast(`Draft v${r.version} updated (${r.method})`); render(); });
  if (draft) drawDraft(draft);
}
function ruleText(r) {
  const c = x => x.all ? x.all.map(c).join(" AND ") : x.any ? "(" + x.any.map(c).join(" OR ") + ")" : x.not ? "NOT " + c(x.not) : `${x.field} ${x.op} ${x.field === "value" ? inr(x.value) : x.value}`;
  const o = r.outcome, s = o.supplier || {};
  return `when ${r.when ? c(r.when) : "always"} → ${o.decision} / ${o.route}${s.mode === "suggest" ? ` (suggest ${s.count})` : ` (${s.mode})`}`;
}
function drawDraft(d) {
  $("#draft").innerHTML = `<div class="card"><h2>Draft v${esc(d.version)} <span class="chip warn">awaiting approval</span></h2>
  <div class="mute">Effective <input type="date" id="de" value="${d.effective_date}"> · from ${esc(d.source_kind)} ${esc(d.source_name || "")} ${d.notes ? "· " + esc(d.notes) : ""}</div>
  <h3>What changes (plain language)</h3>${d.changes.length ? `<ul class="diff">${d.changes.map(c => `<li class="${c.kind}">${esc(c.text)}</li>`).join("")}</ul>` : '<div class="mute">No differences from the policy in force.</div>'}
  <h3>Rules after this change</h3>${d.doc.rules.map((r, i) => `<div class="rule"><span class="mute">${i + 1}.</span> <b>[${esc(r.clause)}]</b> ${esc(ruleText(r))}</div>`).join("")}
  <details><summary>Edit as JSON</summary><textarea id="dj" rows="14" style="font:12px monospace">${esc(JSON.stringify(d.doc, null, 2))}</textarea><button id="ds" class="sec">Save edits</button></details>
  <div class="row" style="margin-top:10px"><button id="da">Approve &amp; activate</button><button id="dr" class="ghost">Reject</button></div></div>`;
  $("#de").onchange = guard(async () => { const doc = { ...d.doc, effective_date: $("#de").value }; await api("/policies/" + d.id, { method: "PUT", body: { doc } }); render(); });
  $("#ds").onclick = guard(async () => { await api("/policies/" + d.id, { method: "PUT", body: { doc: JSON.parse($("#dj").value) } }); toast("Saved"); render(); });
  $("#da").onclick = guard(async () => { await api(`/policies/${d.id}/approve`, { body: {} }); toast("Policy approved. Reprocess a batch to apply it."); render(); });
  $("#dr").onclick = guard(async () => { await api(`/policies/${d.id}/reject`, { body: {} }); render(); });
}

// ---------------------------------------------------------------- suppliers
async function viewSuppliers(v) {
  const items = await api("/items");
  const today = state.status?.today || new Date().toISOString().slice(0, 10);
  v.innerHTML = `<div class="card"><div class="row"><b>Item</b><select id="si">${items.map(i => `<option value="${i.item_code}">${i.item_code} · ${esc(i.description)}</option>`).join("")}</select>
  <b>As of</b><input type="date" id="sd" value="${today}"><span class="grow"></span><button id="se" class="ghost">Download preferred-supplier CSV (top 3, all items)</button></div></div><div id="sres"></div>`;
  const load = guard(async () => {
    const r = await api(`/preferred/${$("#si").value}?as_of=${$("#sd").value}`);
    $("#sres").innerHTML = `<div class="card"><h2>${esc(r.item.description)} <span class="mute">· policy v${r.policy_version} · ${r.suppliers.length} preferred</span></h2>
    <table><tr><th>#</th><th>Supplier</th><th class="num">Score</th><th class="num">POs (548d)</th><th>Last PO</th><th>Contract</th><th class="num">On-time</th><th class="num">Quality</th><th class="num">Reference price</th><th>Score breakdown</th></tr>
    ${r.suppliers.map(s => `<tr><td>${s.rank}</td><td>${s.supplier_id}<div class="mute">${esc(s.supplier_name)}</div></td><td class="num">${s.score}</td><td class="num">${s.po_count}</td><td>${s.last_po_date || "–"}</td><td>${s.had_contract ? esc(s.contract_ids.join(", ")) : "–"}</td><td class="num">${s.on_time_pct}%</td><td class="num">${s.quality}</td><td class="num">${inr(s.reference_price)}<div class="mute">${esc(s.reference_source.type)} ${esc(s.reference_source.po_number || s.reference_source.contract_id)}</div></td>
    <td class="mute">${Object.entries(s.breakdown).map(([k, b]) => `${k} ${b.points.toFixed(3)}`).join(" · ")}</td></tr>`).join("") || '<tr><td colspan="10" class="mute">No eligible supplier has a PO or contract for this item in the window.</td></tr>'}</table>
    ${r.excluded.map(x => `<div class="mute">Excluded: ${x.supplier_id} ${esc(x.name)} — ${esc(x.reason)}</div>`).join("")}</div>`;
  });
  $("#si").onchange = load; $("#sd").onchange = load; load();
  $("#se").onclick = guard(async () => download(`preferred_${$("#sd").value}.csv`, await api(`/preferred-export.csv?as_of=${$("#sd").value}`)));
}

// ---------------------------------------------------------------- contracts
async function viewContracts(v) {
  const cs = await api("/contracts");
  v.innerHTML = `<div class="card"><h2>Rate contracts</h2><table><tr><th>Contract</th><th>Supplier</th><th>Validity</th><th>Ceiling used</th><th>Source</th><th>Items</th></tr>
  ${cs.map(c => { const p = Math.min(100, c.consumed_value / c.value_cap * 100); return `<tr><td>${c.contract_id}</td><td>${c.supplier_id} <span class="chip ${c.supplier_status === "Active" ? "ok" : "bad"}">${c.supplier_status}</span><div class="mute">${esc(c.supplier_name)}</div></td><td>${c.start_date} → ${c.end_date}</td>
  <td><div class="bar ${p > 90 ? "hot" : ""}"><i style="width:${p}%"></i></div>${inr(c.consumed_value)} / ${inr(c.value_cap)}</td><td>${c.source}</td><td class="mute">${c.lines.map(l => l.item_code + " @ " + inr(l.rate)).join("<br>")}</td></tr>`; }).join("")}</table></div>
  <div class="card"><h2>Add a contract from PDF</h2><p class="mute">A signed contract PDF that is not in the register. I read it, you check it, then save.</p><input type="file" id="cp" accept=".pdf"><div id="cf2"></div></div>`;
  $("#cp").onchange = guard(async e => {
    const fd = new FormData(); fd.append("file", e.target.files[0]); const p = await api("/contracts/parse-pdf", { method: "POST", body: fd });
    $("#cf2").innerHTML = `${p.warnings.length ? `<div class="chip warn">${esc(p.warnings.join("; "))}</div>` : ""}<div class="grid2" style="margin-top:8px">
    <label>Contract no<input type="text" id="k1" value="${esc(p.contract_id || "")}"></label><label>Supplier ID<input type="text" id="k2" value="${esc(p.supplier_id || "")}"></label>
    <label>Start<input type="date" id="k3" value="${p.start_date || ""}"></label><label>End<input type="date" id="k4" value="${p.end_date || ""}"></label><label>Value ceiling (₹)<input type="number" id="k5" value="${p.value_cap || ""}"></label></div>
    <label>Lines (item_code,rate per row)<textarea id="k6" rows="4">${(p.lines || []).map(l => l.item_code + "," + l.rate).join("\n")}</textarea></label><button id="ks">Save contract</button>`;
    $("#ks").onclick = guard(async () => {
      const lines = $("#k6").value.split("\n").filter(x => x.trim()).map(x => { const [i, r] = x.split(","); return { item_code: i.trim(), rate: parseFloat(r) }; });
      await api("/contracts", { body: { contract_id: $("#k1").value, supplier_id: $("#k2").value, supplier_name: p.supplier_name, start_date: $("#k3").value, end_date: $("#k4").value, value_cap: $("#k5").value, lines, pdf_name: p.pdf_name } });
      toast("Contract saved"); render();
    });
  });
}

// ---------------------------------------------------------------- audit & setup
async function viewAudit(v) {
  const rows = await api("/audit");
  v.innerHTML = `<div class="card"><h2>Audit trail</h2><table><tr><th>When</th><th>Who</th><th>Action</th><th>Entity</th><th>Detail</th></tr>${rows.map(r => `<tr><td>${esc(r.ts)}</td><td>${esc(r.user)}<div class="mute">${esc(r.role)}</div></td><td>${esc(r.action)}</td><td>${esc(r.entity)} ${esc(r.entity_id)}</td><td class="mute">${esc(JSON.stringify(r.detail)).slice(0, 220)}</td></tr>`).join("")}</table></div>`;
}
async function viewSetup(v) {
  const s = state.status?.master || {};
  v.innerHTML = `<div class="card"><h2>Master data</h2><div class="row">${Object.entries(s).map(([k, n]) => `<span class="chip ${n ? "ok" : "bad"}">${k}: ${n}</span>`).join("")}</div>
  <p class="mute">Upload the item master, supplier master, plants, PO history, contract register, contract lines (CSV) and contract PDFs, or a single .zip containing them. Files are recognised by their columns, not their names. Contracts that exist only as PDFs are read automatically. Requires the Procurement role.</p>
  <input type="file" id="mf" multiple accept=".csv,.pdf,.zip"><button id="mu">Load</button><div id="mr" class="mute"></div></div>`;
  $("#mu").onclick = guard(async () => {
    const fd = new FormData(); [...$("#mf").files].forEach(f => fd.append("files", f));
    const r = await api("/admin/load-master-data", { method: "POST", body: fd }); $("#mr").textContent = JSON.stringify(r.loaded); toast("Master data loaded"); render();
  });
}
render();
