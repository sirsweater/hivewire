"use strict";
// Hive Admin front end. No framework and no network dependencies: the page must
// work at a site with no internet, served by the Pi alone.

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let KINDS = {};
let STATE = null;
let timer = null;
let FLASH_ONLY = false;     // PC mode: the Flash page is the whole app

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(path, body, opts = {}) {
  const init = body === undefined ? {} : {
    method: "POST",
    headers: { "X-Hive": "1", "Content-Type": opts.raw ? "application/octet-stream" : "application/json" },
    body: opts.raw ? body : JSON.stringify(body),
  };
  const r = await fetch(path, init);
  if (r.status === 401) { showLogin(); throw new Error("login required"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function toast(msg, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------
function slotSpec(kind, sid) {
  const k = KINDS[kind] || {};
  if (String(sid).startsWith("d:")) return (k.derived || {})[sid.slice(2)] || { label: sid };
  return (k.slots || {})[String(sid)] || { label: "Slot " + sid };
}
function fmtVal(spec, v) {
  if (v === null || v === undefined) return "—";
  // Some zeroes mean "not measured" rather than a reading of zero — a board
  // with no battery divider reports 0, and "0.00 V" would read as flat.
  if (v === 0 && spec.zero_means) return spec.zero_means;
  if (spec.hex) return (v >>> 0).toString(16).padStart(8, "0");
  if (spec.error) { const c = v & 0xFFFF, sub = (v >>> 16) & 0xFF; return c ? "E" + c + (sub ? "/" + sub : "") : "none"; }
  if (spec.bool) return v ? "yes" : "no";
  if (spec.bits) {
    const bad = Object.entries(spec.bits).filter(([b]) => !(v & +b)).map(([, l]) => l);
    return bad.length ? "missing: " + bad.join(", ") : "all ok";
  }
  const x = v * (spec.scale || 1);
  const d = spec.decimals ?? (spec.scale && spec.scale < 1 ? 2 : 0);
  return x.toFixed(d) + (spec.unit ? " " + spec.unit : "");
}
function fmtAge(s) {
  if (s == null) return "never";
  if (s < 90) return s + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 129600) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function fmtTime(t) { return new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
function nodeTitle(n) { return n.name || (n.kind ? n.kind + " " + n.id : "Node " + n.id); }
function valueOf(n, sid) {
  if (String(sid).startsWith("d:")) return n.derived[sid.slice(2)];
  return n.slots[sid];
}
function chartable(kind, slots) {
  const k = KINDS[kind] || {};
  const out = [];
  for (const [key, d] of Object.entries(k.derived || {})) if (d.chart) out.push("d:" + key);
  for (const [sid, s] of Object.entries(k.slots || {})) if (s.chart && (!slots || sid in slots)) out.push(sid);
  if (!out.length && slots) return Object.keys(slots);
  return out;
}

// ---------------------------------------------------------------------------
// Chart: step line, because a stored value holds until the next one arrives.
// ---------------------------------------------------------------------------
function drawChart(host, points, spec, t0, t1, notes = []) {
  const pts = points.filter((p) => p[1] !== null && p[1] !== undefined);
  if (!pts.length) { host.innerHTML = '<div class="empty">No readings in this period yet.</div>'; return; }
  const W = 800, H = 240, L = 52, R = 12, T = 12, B = 26;
  const scale = spec.scale || 1;
  const ys = pts.map((p) => p[1] * scale);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const now = Math.floor(Date.now() / 1000);
  const tEnd = Math.min(t1, now);
  const X = (t) => L + ((t - t0) / Math.max(1, tEnd - t0)) * (W - L - R);
  const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

  let d = "";
  pts.forEach((p, i) => {
    const x = X(Math.max(p[0], t0)), y = Y(p[1] * scale);
    d += i === 0 ? `M${x},${y}` : `H${x}V${y}`;
  });
  const last = pts[pts.length - 1];
  d += `H${X(tEnd)}`;
  const area = d + `V${H - B}H${X(Math.max(pts[0][0], t0))}Z`;

  // Enough decimals that neighbouring gridlines never print the same number.
  let dec = spec.decimals ?? (scale < 1 ? 1 : 0);
  while (dec < 4 && (hi - lo) / 4 < Math.pow(10, -dec) * 2) dec++;
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4, y = Y(v);
    grid += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>` +
            `<text x="${L - 8}" y="${y + 4}" text-anchor="end">${v.toFixed(dec)}</text>`;
  }
  const span = tEnd - t0;
  for (let i = 0; i <= 4; i++) {
    const t = t0 + (span * i) / 4;
    const lab = span > 2 * 86400
      ? new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" })
      : new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    grid += `<text x="${X(t)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${lab}</text>`;
  }
  // Markers: what a person did, drawn where they did it. A dip in a reading
  // and "moved it to the hallway" are only related if you can see them together.
  const marks = notes.filter((m) => m.ts >= t0 && m.ts <= tEnd).map((m) =>
    `<line class="mark" x1="${X(m.ts)}" x2="${X(m.ts)}" y1="${T}" y2="${H - B}"/>` +
    `<circle class="mark-dot" cx="${X(m.ts)}" cy="${T}" r="3"/>`).join("");
  host.innerHTML =
    `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(spec.label)} over time">` +
    grid + `<path class="area" d="${area}"/>` + marks + `<path class="line" d="${d}" vector-effect="non-scaling-stroke"/>` +
    `<circle class="dot" cx="${X(Math.max(last[0], t0))}" cy="${Y(last[1] * scale)}" r="3.5"/>` +
    `<line class="cursor" y1="${T}" y2="${H - B}" x1="-10" x2="-10"/></svg>` +
    `<div class="chart-tip">Latest: ${esc(fmtVal(spec, last[1]))} · ${fmtTime(last[0])}</div>`;
  const svg = $("svg", host), cur = $(".cursor", host), tip = $(".chart-tip", host);
  svg.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * W;
    const t = t0 + ((x - L) / (W - L - R)) * (tEnd - t0);
    let p = pts[0];
    for (const q of pts) { if (q[0] <= t) p = q; else break; }
    cur.setAttribute("x1", x); cur.setAttribute("x2", x);
    const near = notes.filter((m) => Math.abs(X(m.ts) - x) < 6);
    tip.textContent = `${fmtVal(spec, p[1])} · ${fmtTime(Math.max(t0, Math.round(t)))}`
      + (near.length ? `  —  ${near.map((m) => m.text).join(" | ")}` : "");
  });
  svg.addEventListener("mouseleave", () => {
    cur.setAttribute("x1", -10); cur.setAttribute("x2", -10);
    tip.textContent = `Latest: ${fmtVal(spec, last[1])} · ${fmtTime(last[0])}`;
  });
}

const RANGES = [["24h", 86400], ["7 days", 7 * 86400], ["30 days", 30 * 86400], ["1 year", 365 * 86400]];

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------
const views = {};

views.dashboard = async (el) => {
  const s = STATE;
  const h = s.health || {};
  const nodes = s.nodes.filter((n) => !n.hidden);
  const warnCount = nodes.reduce((a, n) => a + (n.warnings.length ? 1 : 0), 0);
  el.innerHTML = `
    <div class="head spread"><div><h1>Dashboard</h1>
      <div class="muted small">${s.snapshot_time ? "Updated " + fmtAge(s.time - s.snapshot_time) : "Waiting for the gateway…"}${s.fake ? " · simulated swarm" : ""}</div></div>
      <button id="refresh">Refresh now</button></div>
    <div class="grid cols-4">
      <div class="card stat"><div class="v">${nodes.length}</div><div class="k">nodes known</div></div>
      <div class="card stat"><div class="v">${h.ok ?? "—"}<span class="muted" style="font-size:16px"> / ${h.up ?? "—"}</span></div><div class="k">in step with the hive</div></div>
      <div class="card stat"><div class="v">${h.m ?? "—"}</div><div class="k">swarm mode · epoch ${h.ep ?? "—"}</div></div>
      <div class="card stat"><div class="v" style="color:${warnCount ? "var(--warn)" : "var(--good)"}">${warnCount}</div><div class="k">nodes needing attention</div></div>
    </div>
    <h2 style="margin-top:28px">Nodes</h2>
    <div class="grid cols-4" id="cards"></div>`;
  $("#refresh").onclick = async () => { await api("/api/poll", {}); toast("Asked the gateway for fresh values"); setTimeout(refresh, 1500); };
  const cards = $("#cards");
  if (!nodes.length) cards.innerHTML = '<div class="card empty">No nodes heard yet.</div>';
  for (const n of nodes) {
    const k = KINDS[n.kind] || {};
    const head = (k.headline || Object.keys(n.slots).slice(0, 4));
    const rows = head.filter((sid) => valueOf(n, sid) !== undefined).map((sid) => {
      const sp = slotSpec(n.kind, sid);
      return `<div class="k">${esc(sp.label)}</div><div class="v">${esc(fmtVal(sp, valueOf(n, sid)))}</div>`;
    }).join("");
    const cls = n.warnings.length ? "warn" : "good";
    cards.insertAdjacentHTML("beforeend", `
      <a class="card node-card" href="#/node/${n.id}">
        <div class="spread"><div class="title">${esc(nodeTitle(n))}</div><span class="pill ${cls}">${fmtAge(n.age)}</span></div>
        <div class="sub">#${n.id} · ${esc(n.kind || "unknown type")}${n.location ? " · " + esc(n.location) : ""}${n.hops ? " · via a relay, " + n.hops + " hop" + (n.hops > 1 ? "s" : "") : ""}</div>
        <div class="kv">${rows}</div>
        ${n.warnings.length ? `<div class="warnings">${n.warnings.map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("")}</div>` : ""}
      </a>`);
  }
};

views.node = async (el, id) => {
  const nid = +id;
  const n = STATE.nodes.find((x) => x.id === nid);
  if (!n) { el.innerHTML = `<div class="card empty">Node ${nid} has not been heard since the admin started.</div>`; return; }
  const k = KINDS[n.kind] || {};
  const metrics = chartable(n.kind, n.slots);
  let metric = views.node.metric?.[nid] || metrics[0];
  let range = views.node.range || 86400;
  el.innerHTML = `
    <div class="head spread"><div>
      <h1>${esc(nodeTitle(n))}</h1>
      <div class="muted small">#${n.id} · ${esc(n.kind || "unknown type")}${n.location ? " · " + esc(n.location) : ""} · heard ${fmtAge(n.age)}</div>
    </div><div class="row"><button id="addmark">Add marker</button><a class="btn" href="#/settings">Edit name &amp; calibration</a></div></div>
    ${n.warnings.length ? `<div class="card" style="border-color:var(--warn);margin-bottom:16px">${n.warnings.map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("")}</div>` : ""}
    <div class="card stack">
      <div class="spread"><div class="tabs" id="metric-tabs"></div><div class="tabs" id="range-tabs"></div></div>
      <div id="chart"></div>
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card"><h2>Current values</h2><div class="tablewrap"><table id="slots"></table></div></div>
      <div class="stack">
        <div class="card"><h2>Actions</h2><div class="row" id="actions"></div>
          <form id="setform" class="row" style="margin-top:14px">
            <select id="set-slot"></select><input id="set-val" type="number" style="width:110px" placeholder="value" required>
            <button>Write</button></form>
          <p class="muted small">Writes go out over the swarm and the gateway echoes them over LoRa too, so each one costs a little airtime.</p>
          <div id="set-reply" class="mono muted"></div></div>
        <div class="card"><h2>Path to the hive</h2>
          <div id="linkpath" class="muted">checking…</div>
          <p class="muted small">The hive records the hop count of the first copy of each report it receives. Direct means the node was heard without help; via a relay means that report only got through because another node carried it.</p></div>
        <div class="card"><div class="spread"><h2>Node's own log</h2><button id="getlog">Fetch</button></div>
          <p class="muted small">Asks the node over the air for its recent history. Takes ~10 seconds, and the reply is relayed over LoRa.</p>
          <div id="nodelog" class="log mono" hidden></div></div>
      </div>
    </div>`;
  $("#addmark").onclick = async () => {
    const text = prompt("What happened? (shows as a line on every chart)", "");
    if (!text) return;
    try { await api("/api/note", { text }); toast("Marker added"); load(); } catch (e) { toast(e.message); }
  };
  const mt = $("#metric-tabs"), rt = $("#range-tabs");
  const drawTabs = () => {
    mt.innerHTML = metrics.map((m) => `<button data-m="${m}" class="${m === metric ? "on" : ""}">${esc(slotSpec(n.kind, m).label)}</button>`).join("");
    rt.innerHTML = RANGES.map(([l, s]) => `<button data-r="${s}" class="${s === range ? "on" : ""}">${l}</button>`).join("");
  };
  const load = async () => {
    drawTabs();
    const t1 = Math.floor(Date.now() / 1000), t0 = t1 - range;
    const [r, notes] = await Promise.all([
      api(`/api/series?node=${nid}&slot=${encodeURIComponent(metric)}&from=${t0}&to=${t1}`),
      api(`/api/notes?from=${t0}&to=${t1}`),
    ]);
    drawChart($("#chart"), r.points, slotSpec(n.kind, metric), t0, t1, notes);
  };
  mt.onclick = (e) => { if (e.target.dataset.m) { metric = e.target.dataset.m; (views.node.metric ||= {})[nid] = metric; load(); } };
  rt.onclick = (e) => { if (e.target.dataset.r) { range = +e.target.dataset.r; views.node.range = range; load(); } };
  if (metric) load(); else $("#chart").innerHTML = '<div class="empty">Nothing to chart.</div>';

  const rows = [];
  for (const [key, v] of Object.entries(n.derived)) {
    const sp = slotSpec(n.kind, "d:" + key);
    rows.push(`<tr><td>${esc(sp.label)} <span class="muted small">(calibrated)</span></td><td class="num">${esc(fmtVal(sp, v))}</td></tr>`);
  }
  for (const [sid, v] of Object.entries(n.slots)) {
    const sp = slotSpec(n.kind, sid);
    rows.push(`<tr><td>${esc(sp.label)}${sp.note ? `<div class="muted small">${esc(sp.note)}</div>` : ""}</td><td class="num">${esc(fmtVal(sp, v))}</td><td class="muted small num">${sid}</td></tr>`);
  }
  $("#slots").innerHTML = `<tr><th>Value</th><th class="num"></th><th class="num">slot</th></tr>` + rows.join("");

  const acts = $("#actions");
  for (const a of k.actions || []) {
    const b = document.createElement("button");
    b.textContent = a.label;
    b.onclick = async () => {
      if (a.confirm && !confirm(a.confirm)) return;
      b.disabled = true;
      try { const r = await api("/api/set", { target: String(nid), slot: a.slot, value: a.value }); toast(r.reply || "No reply from the gateway"); }
      catch (e) { toast(e.message); } finally { b.disabled = false; }
    };
    acts.appendChild(b);
  }
  if (!acts.children.length) acts.innerHTML = '<span class="muted small">No shortcuts for this node type.</span>';
  const writable = Object.entries(k.slots || {}).filter(([, s]) => s.writable);
  $("#set-slot").innerHTML = writable.map(([sid, s]) => `<option value="${sid}">${esc(s.label)} (${s.min}–${s.max})</option>`).join("") || '<option value="">no writable slots</option>';
  $("#setform").onsubmit = async (e) => {
    e.preventDefault();
    const slot = +$("#set-slot").value, value = +$("#set-val").value;
    const s = (k.slots || {})[slot];
    if (s && (value < s.min || value > s.max)) { toast(`Must be ${s.min}–${s.max}`); return; }
    if (!confirm(`Write ${value} to "${s ? s.label : slot}" on ${nodeTitle(n)}?`)) return;
    try { const r = await api("/api/set", { target: String(nid), slot, value }); $("#set-reply").textContent = r.reply || "no reply from the gateway"; }
    catch (err) { $("#set-reply").textContent = err.message; }
  };
  (async () => {
    const box = $("#linkpath");
    try {
      const r = await api(`/api/linkpath?node=${nid}&from=${Math.floor(Date.now() / 1000) - 86400}`);
      if (r.relay_fraction === null) { box.textContent = "No reports stored yet."; return; }
      const pct = Math.round(r.relay_fraction * 100);
      const now = n.hops ? `now: via a relay (${n.hops} hop${n.hops > 1 ? "s" : ""})` : "now: heard directly";
      box.innerHTML = `<div class="row"><span class="pill ${n.hops ? "warn" : "good"}">${now}</span></div>
        <div class="kv" style="max-width:420px">
          <div class="k">Last 24 hours</div><div class="v">${100 - pct}% direct · ${pct}% via a relay</div>
        </div>`;
    } catch (e) { box.textContent = e.message; }
  })();

  $("#getlog").onclick = async (e) => {
    const b = e.target; b.disabled = true; b.textContent = "Asking…";
    const box = $("#nodelog"); box.hidden = false; box.textContent = "Waiting for the node…";
    try { const r = await api("/api/nodelog", { id: nid }); box.textContent = r.lines.length ? r.lines.join("\n") : "No reply. The node may be out of range."; }
    catch (err) { box.textContent = err.message; } finally { b.disabled = false; b.textContent = "Fetch"; }
  };
};

views.reports = async (el) => {
  const nodes = STATE.nodes;
  const saved = views.reports.sel || {};
  el.innerHTML = `
    <div class="head"><h1>Reports</h1><div class="muted small">History of any reading, daily summaries, and CSV export.</div></div>
    <div class="card row" style="align-items:flex-end">
      <label class="f">Node<select id="r-node">${nodes.map((n) => `<option value="${n.id}">${esc(nodeTitle(n))}</option>`).join("")}</select></label>
      <label class="f">Reading<select id="r-metric"></select></label>
      <label class="f">From<input type="date" id="r-from"></label>
      <label class="f">To<input type="date" id="r-to"></label>
      <div class="tabs" id="r-quick">${RANGES.map(([l, s]) => `<button data-r="${s}">${l}</button>`).join("")}</div>
    </div>
    <div class="card stack" style="margin-top:16px"><div class="spread"><h2 id="r-title">—</h2>
      <a class="btn" id="r-csv" href="#">Download CSV</a></div><div id="r-chart"></div>
      <div class="grid cols-4" id="r-summary"></div></div>
    <div class="card" style="margin-top:16px"><h2>By day</h2><div class="tablewrap"><table id="r-days"></table></div></div>`;
  const nodeSel = $("#r-node"), metSel = $("#r-metric"), from = $("#r-from"), to = $("#r-to");
  const iso = (t) => new Date(t * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const setRange = (secs) => { const t1 = Date.now() / 1000; from.value = iso(t1 - secs); to.value = iso(t1); };
  if (saved.node) nodeSel.value = saved.node;
  const fillMetrics = () => {
    const n = nodes.find((x) => x.id === +nodeSel.value);
    const ms = n ? [...new Set([...chartable(n.kind, n.slots), ...Object.keys(n.slots)])] : [];
    metSel.innerHTML = ms.map((m) => `<option value="${m}">${esc(slotSpec(n.kind, m).label)}</option>`).join("");
    if (saved.metric && ms.includes(saved.metric)) metSel.value = saved.metric;
  };
  fillMetrics();
  if (saved.from) { from.value = saved.from; to.value = saved.to; } else setRange(7 * 86400);
  const run = async () => {
    const n = nodes.find((x) => x.id === +nodeSel.value);
    if (!n || !metSel.value) return;
    views.reports.sel = { node: nodeSel.value, metric: metSel.value, from: from.value, to: to.value };
    const t0 = Math.floor(new Date(from.value + "T00:00").getTime() / 1000);
    const t1 = Math.floor(new Date(to.value + "T23:59:59").getTime() / 1000);
    const spec = slotSpec(n.kind, metSel.value);
    $("#r-title").textContent = `${spec.label} · ${nodeTitle(n)}`;
    $("#r-csv").href = `/api/export.csv?node=${n.id}&slot=${encodeURIComponent(metSel.value)}&from=${t0}&to=${t1}`;
    const [r, notes] = await Promise.all([
      api(`/api/report?node=${n.id}&slot=${encodeURIComponent(metSel.value)}&from=${t0}&to=${t1}`),
      api(`/api/notes?from=${t0}&to=${t1}`),
    ]);
    drawChart($("#r-chart"), r.points, spec, t0, t1, notes);
    const S = r.summary;
    $("#r-summary").innerHTML = S ? [["Lowest", S.min], ["Average", S.avg], ["Highest", S.max]].map(([l, v]) =>
      `<div class="stat"><div class="v">${esc(fmtVal(spec, v))}</div><div class="k">${l}</div></div>`).join("") +
      `<div class="stat"><div class="v">${S.n}</div><div class="k">readings stored</div></div>` : "";
    $("#r-days").innerHTML = `<tr><th>Day</th><th class="num">Low</th><th class="num">Average</th><th class="num">High</th><th class="num">Readings</th></tr>` +
      (r.days.slice().reverse().map((d) => `<tr><td>${d.day}</td><td class="num">${esc(fmtVal(spec, d.min))}</td><td class="num">${esc(fmtVal(spec, d.avg))}</td><td class="num">${esc(fmtVal(spec, d.max))}</td><td class="num">${d.n}</td></tr>`).join("") ||
      '<tr><td colspan="5" class="muted">No readings in this period.</td></tr>');
  };
  nodeSel.onchange = () => { fillMetrics(); run(); };
  metSel.onchange = from.onchange = to.onchange = run;
  $("#r-quick").onclick = (e) => { if (e.target.dataset.r) { setRange(+e.target.dataset.r); run(); } };
  run();
};

views.swarm = async (el) => {
  const h = STATE.health || {};
  el.innerHTML = `
    <div class="head"><h1>Swarm</h1><div class="muted small">State every node adopts, and direct commands.</div></div>
    <div class="grid cols-2">
      <div class="card stack"><h2>Swarm mode</h2>
        <div class="row"><span class="pill plain">mode ${h.m ?? "—"}</span><span class="pill plain">epoch ${h.ep ?? "—"}</span></div>
        <p class="muted small">Sets the state the hive advertises. Every node adopts it and passes it on, including nodes that were asleep or out of range, when they next hear the swarm. A TTL makes it expire back to safe on its own.</p>
        <form id="modeform" class="row" style="align-items:flex-end">
          <label class="f">Mode<input id="m-mode" type="number" min="0" max="255" value="${h.m ?? 0}" style="width:90px" required></label>
          <label class="f">Param<input id="m-param" type="number" min="0" max="255" value="0" style="width:90px"></label>
          <label class="f">TTL (s, 0 = none)<input id="m-ttl" type="number" min="0" max="65535" value="0" style="width:130px"></label>
          <button class="primary">Set mode</button></form>
        <div id="mode-reply" class="mono muted"></div></div>
      <div class="card stack"><h2>Write a slot</h2>
        <p class="muted small">Target a node id, <span class="mono">all</span>, or a role such as <span class="mono">r2</span> (sensors). Nodes refuse anything outside a slot's declared range and say so in their log.</p>
        <form id="rawset" class="row" style="align-items:flex-end">
          <label class="f">Target<input id="s-target" value="all" style="width:90px" required></label>
          <label class="f">Slot<input id="s-slot" type="number" min="0" max="255" style="width:80px" required></label>
          <label class="f">Value<input id="s-val" type="number" style="width:110px" required></label>
          <button>Write</button></form>
        <div id="set-reply2" class="mono muted"></div></div>
    </div>
    <div class="card" style="margin-top:16px"><div class="spread"><h2>Gateway log</h2><button id="gwlog">Fetch</button></div>
      <p class="muted small">The gateway's own last events. Its reply also goes out over LoRa.</p>
      <div id="gwlog-box" class="log mono" hidden></div></div>`;
  $("#modeform").onsubmit = async (e) => {
    e.preventDefault();
    const b = { mode: +$("#m-mode").value, param: +$("#m-param").value, ttl: +$("#m-ttl").value };
    if (!confirm(`Set swarm mode ${b.mode} (param ${b.param}${b.ttl ? ", expires in " + b.ttl + "s" : ""}) on every node?`)) return;
    try { const r = await api("/api/mode", b); $("#mode-reply").textContent = r.reply || "no reply"; refresh(); }
    catch (err) { $("#mode-reply").textContent = err.message; }
  };
  $("#rawset").onsubmit = async (e) => {
    e.preventDefault();
    const b = { target: $("#s-target").value.trim(), slot: +$("#s-slot").value, value: +$("#s-val").value };
    if (!confirm(`Write ${b.value} to slot ${b.slot} on ${b.target}?`)) return;
    try { const r = await api("/api/set", b); $("#set-reply2").textContent = r.reply || "no reply"; }
    catch (err) { $("#set-reply2").textContent = err.message; }
  };
  $("#gwlog").onclick = async () => {
    const box = $("#gwlog-box"); box.hidden = false; box.textContent = "…";
    try { const r = await api("/api/gatewaylog", {}); box.textContent = r.lines.join("\n") || "(empty)"; }
    catch (err) { box.textContent = err.message; }
  };
};

views.firmware = async (el) => {
  el.innerHTML = `
    <div class="head"><h1>Firmware</h1><div class="muted small">Upload an app image and send it to the swarm over ESP-NOW.</div></div>
    <div class="note" style="margin-bottom:16px">A push goes to <b>every</b> node at once. Each node installs it only if it is the same firmware family it already runs, and not the image it already has; everything else says so in its log and keeps running. A new image must rejoin the hive within three minutes or the node reverts to its previous firmware on its own.</div>
    <div class="grid cols-2">
      <div class="card stack"><h2>Upload</h2>
        <label class="drop" id="drop">Drop a <span class="mono">.ino.bin</span> app image here, or click to choose<input type="file" id="file" accept=".bin" hidden></label>
        <p class="muted small">Use the plain app image (<span class="mono">Sketch.ino.bin</span>), not <span class="mono">merged.bin</span> or the bootloader.</p></div>
      <div class="card stack" id="jobcard"><h2>Current push</h2><div id="job" class="muted">No push running.</div></div>
    </div>
    <div class="card" style="margin-top:16px"><h2>Images on the Pi</h2><div class="tablewrap"><table id="images"></table></div></div>
    <div class="card" style="margin-top:16px"><h2>Push history</h2><div class="tablewrap"><table id="history"></table></div></div>`;
  const inp = $("#file"), drop = $("#drop");
  const upload = async (f) => {
    if (!f) return;
    toast("Uploading " + f.name + "…");
    try { const r = await api("/api/firmware/upload?name=" + encodeURIComponent(f.name), await f.arrayBuffer(), { raw: true });
      toast(`Uploaded: ${r.size} bytes, family ${r.families.join(", ") || "none"}`); load(); }
    catch (e) { toast(e.message, 6000); }
  };
  inp.onchange = () => upload(inp.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files[0]); };

  const whoTakes = (fams) => {
    const nodes = STATE.nodes;
    if (!fams.length) return "Nodes that declare a family will refuse it (no family marker).";
    const yes = nodes.filter((n) => fams.includes(n.kind)).map(nodeTitle);
    const no = nodes.filter((n) => !fams.includes(n.kind)).map(nodeTitle);
    return `Would install on: ${yes.join(", ") || "none"}` + (no.length ? `. Refused by: ${no.join(", ")}.` : ".");
  };
  const load = async () => {
    const r = await api("/api/firmware");
    const running = r.job && r.job.state === "running";
    $("#images").innerHTML = `<tr><th>File</th><th>Family</th><th class="num">Size</th><th>CRC</th><th>Uploaded</th><th></th></tr>` +
      (r.images.map((i) => `<tr><td class="mono">${esc(i.name)}</td><td>${esc(i.families.join(", ") || "—")}</td><td class="num">${(i.size / 1024).toFixed(0)} KB</td><td class="mono">${i.crc}</td><td class="small muted">${fmtTime(i.mtime)}</td>
        <td class="row" style="justify-content:flex-end"><button class="primary" data-push="${esc(i.name)}" data-fams="${esc(i.families.join(","))}" ${running ? "disabled" : ""}>Push</button><button class="danger" data-del="${esc(i.name)}">Delete</button></td></tr>`).join("") ||
       '<tr><td colspan="6" class="muted">No images uploaded yet.</td></tr>');
    $("#history").innerHTML = `<tr><th>When</th><th>File</th><th>Family</th><th>CRC</th><th>Result</th></tr>` +
      (r.history.map((h) => `<tr><td class="small">${fmtTime(h.ts)}</td><td class="mono">${esc(h.file)}</td><td>${esc(h.family || "—")}</td><td class="mono">${esc(h.crc)}</td><td>${h.result === "complete" ? '<span class="pill good">complete</span>' : '<span class="pill bad">' + esc(h.result) + "</span>"}</td></tr>`).join("") ||
       '<tr><td colspan="5" class="muted">No pushes yet.</td></tr>');
    const j = r.job;
    if (j) {
      const pct = j.info.size ? Math.min(100, (100 * j.fed) / j.info.size) : 0;
      const badge = j.state === "running" ? '<span class="pill warn">sending</span>' : j.state === "done" ? '<span class="pill good">complete</span>' : '<span class="pill bad">failed</span>';
      $("#job").innerHTML = `<div class="spread"><span class="mono">${esc(j.file)}</span>${badge}</div>
        <div class="bar" style="margin:10px 0"><div style="width:${pct.toFixed(1)}%"></div></div>
        <div class="small muted">${(j.fed / 1024).toFixed(0)} / ${(j.info.size / 1024).toFixed(0)} KB fed to the gateway · started ${fmtTime(j.started)}</div>
        <p class="small muted">Nodes install it after the transfer ends: watch their firmware CRC on the dashboard change, and boots go up by one.</p>
        <div class="log mono" style="max-height:200px">${esc(j.lines.slice(-30).join("\n"))}</div>`;
      if (running) setTimeout(() => { if (location.hash === "#/firmware") load(); }, 2000);
    }
    el.querySelectorAll("[data-push]").forEach((b) => (b.onclick = async () => {
      const fams = b.dataset.fams ? b.dataset.fams.split(",") : [];
      if (!confirm(`Push ${b.dataset.push} to the swarm?\n\n${whoTakes(fams)}\n\nIt takes about four minutes, and the dashboard pauses while it runs.`)) return;
      try { await api("/api/firmware/push", { name: b.dataset.push }); toast("Push started"); load(); } catch (e) { toast(e.message); }
    }));
    el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
      if (!confirm("Delete " + b.dataset.del + " from the Pi?")) return;
      try { await api("/api/firmware/delete", { name: b.dataset.del }); load(); } catch (e) { toast(e.message); }
    }));
  };
  load();
};

views.flash = async (el) => {
  el.innerHTML = `
    <div class="head"><h1>Flash a board</h1><div class="muted small">Plug an ESP32-C6 into ${FLASH_ONLY ? "this computer" : "the Pi"} by USB, pick what it should be, and flash it.</div></div>
    <div class="grid cols-2">
      <div class="card stack">
        <div class="spread"><h2>Boards on this USB</h2><button id="fl-refresh">Refresh</button></div>
        <div id="fl-ports" class="muted">Looking…</div>
        <label class="stack small">Firmware<select id="fl-image"></select></label>
        <label class="stack small">Node number<input id="fl-id" type="number" min="1" max="254" placeholder="leave blank to keep its number"></label>
        <div id="fl-idnote" class="small muted"></div>
        <label class="row small" style="gap:8px;align-items:center"><input type="checkbox" id="fl-erase" checked> Wipe the board first <span class="muted">(recommended: clears its old node number and saved radio data; a board with stale saved data was found hearing no radio at all until it was wiped)</span></label>
        <button class="primary" id="fl-go" disabled>Flash</button>
        <div id="fl-env" class="small"></div>
      </div>
      <div class="card stack"><h2>Progress</h2><div id="fl-job" class="muted">Nothing flashed yet.</div></div>
    </div>
    <div class="note" style="margin-top:16px">The board is checked by its MAC before anything is written, and the hive gateway is never offered. After writing, the board reports its radio: a C6 that hears no Wi-Fi networks at all has a faulty radio, and it is better to find that now than after it is wired into a pot.</div>`;
  let data = null;
  const pick = () => { const r = el.querySelector('input[name="fl-port"]:checked'); return r ? r.value : null; };
  const syncButton = () => {
    const running = data && data.job && data.job.state === "running";
    $("#fl-go").disabled = !pick() || !$("#fl-image").value || running || !(data && data.esptool);
  };
  const renderJob = () => {
    const j = data && data.job;
    if (!j) return;
    const badge = j.state === "running" ? '<span class="pill warn">' + esc(j.step) + "</span>"
      : j.state === "done" ? '<span class="pill good">done</span>' : '<span class="pill bad">failed</span>';
    const r = j.result || {};
    const radio = r.radio_networks === undefined ? "" : r.radio_networks === 0
      ? '<span class="pill bad">radio heard nothing: faulty board</span>'
      : `<span class="pill good">radio ok: ${r.radio_networks} networks, best ${r.radio_best} dBm</span>`;
    $("#fl-job").innerHTML = `
      <div class="spread"><span class="mono small">${esc(j.image)} → ${esc(j.mac)}</span>${badge}</div>
      ${j.state === "running" && j.progress !== undefined ? `<div class="bar" style="margin:10px 0"><div style="width:${Number(j.progress).toFixed(0)}%"></div></div>` : ""}
      ${r.family ? `<p class="small">This board is <b>${esc(r.family)}</b>, node <b>${esc(r.id)}</b>. ${radio}</p>` : ""}
      ${r.sensors ? `<p class="small mono">${esc(r.sensors)}</p>` : ""}
      ${j.error ? `<p class="err small">${esc(j.error)}</p>` : ""}
      ${j.warning ? `<p class="err small">${esc(j.warning)}</p>` : ""}
      <div class="log mono small" style="max-height:260px">${esc(j.lines.slice(-60).join("\n"))}</div>`;
    if (j.state === "running") setTimeout(async () => {
      if (route().name !== "flash") return;
      data = await api("/api/flash"); renderJob(); syncButton();
      if (data.job && data.job.state !== "running") loadBoards();
    }, 1500);
  };
  const loadBoards = async () => {
    data = await api("/api/flash");
    const was = pick();
    const ports = data.ports.filter((p) => !p.protected);
    const gw = data.ports.filter((p) => p.gateway);
    const kept = data.ports.filter((p) => p.protected && !p.gateway);
    $("#fl-ports").innerHTML = (ports.length ? ports.map((p, i) => `
      <label class="row small" style="gap:8px;align-items:center">
        <input type="radio" name="fl-port" value="${esc(p.device)}" ${(was ? was === p.device : i === 0) ? "checked" : ""}>
        <span class="mono">${esc(p.mac)}</span><span class="muted">${esc(p.device)}</span>${p.busy ? '<span class="pill warn">flashing</span>' : ""}
      </label>`).join("") : '<span class="muted">No ESP32 board found. Plug one in and press Refresh.</span>') +
      (gw.length ? `<div class="small muted" style="margin-top:6px">Hive gateway ${esc(gw[0].mac)} is plugged in too; it is left alone.</div>` : "") +
      (kept.length ? `<div class="small muted">Also left alone, because it was plugged in when the admin started (hive equipment, like the LoRa radio): ${kept.map((p) => esc(p.mac)).join(", ")}.</div>` : "");
    const cur = $("#fl-image").value;
    $("#fl-image").innerHTML = data.images.map((i) => `<option value="${esc(i.name)}">${esc(i.family)} · ${esc(i.name)}${i.built ? " · built " + esc(i.built) : ""}</option>`).join("") ||
      '<option value="">No images yet: run tools/build_images.py</option>';
    const soil = data.images.find((i) => i.family === "SoilNode");
    if (cur) $("#fl-image").value = cur; else if (soil) $("#fl-image").value = soil.name;
    if (!$("#fl-id").value && data.next_id) $("#fl-id").value = data.next_id;
    $("#fl-idnote").textContent = data.known_ids.length
      ? "Already in the swarm: " + data.known_ids.join(", ") + ". Reusing a number makes two boards fight over it."
      : FLASH_ONLY ? "Each board needs its own number, 1 to 254. This computer can't see the swarm: check the Pi's admin page for numbers already in use."
      : "Each board in the swarm needs its own number, 1 to 254.";
    $("#fl-env").innerHTML = data.esptool ? "" : '<span class="pill bad">esptool not installed</span> <span class="muted">Run <span class="mono">pip install esptool</span> on this machine.</span>';
    el.querySelectorAll('input[name="fl-port"]').forEach((r) => (r.onchange = syncButton));
    renderJob();
    syncButton();
  };
  $("#fl-refresh").onclick = loadBoards;
  $("#fl-image").onchange = syncButton;
  $("#fl-go").onclick = async () => {
    const dev = pick(), img = $("#fl-image").value, id = $("#fl-id").value.trim();
    const port = data.ports.find((p) => p.device === dev);
    if (id && data.known_ids.includes(Number(id)) && !confirm(`Node ${id} already exists in the swarm. Flash this board as node ${id} anyway?`)) return;
    if (!confirm(`Flash ${img} onto ${port ? port.mac : dev}${id ? " as node " + id : ""}?\n\nEverything on that board is replaced.`)) return;
    try {
      await api("/api/flash/start", { device: dev, image: img, node_id: id || null, erase: $("#fl-erase").checked });
      data = await api("/api/flash"); renderJob(); syncButton();
    } catch (e) { toast(e.message, 6000); }
  };
  loadBoards();
};

views.g4rden = async (el) => {
  const r = await api("/api/g4rden");
  const g = r.config, nodes = STATE.nodes;
  const soilNodes = nodes.filter((n) => n.kind === "SoilNode" || g.devices[n.id]);
  const lastLine = (x) => x ? `${fmtTime(x.ts)} — ${x.ok ? `sent ${x.sent} reading(s) across ${x.nodes} node(s)` : `failed: ${esc(x.error || "HTTP " + x.status)}`}` : "never";
  el.innerHTML = `
    <div class="head spread"><div><h1>g4rden</h1>
      <div class="muted small">Upload plant readings to the g4rden site. Nothing leaves the Pi until you switch it on.</div></div>
      <span class="pill ${g.enabled ? "good" : ""}">${g.enabled ? "uploading automatically" : "uploads off"}</span></div>

    <div class="grid cols-2">
      <div class="card stack"><h2>1 · Pair with your g4rden account</h2>
        ${g.paired
          ? `<div class="row"><span class="pill good">paired</span><span class="mono small">${esc(g.gateway_id || "")}</span></div>
             <p class="muted small">The Pi holds a write token. It can post readings and nothing else — it cannot read your history or touch the account.</p>
             <div><button class="danger" id="unpair">Forget this pairing</button></div>`
          : `<p class="muted small">In g4rden, add a plant monitor. You get a claim code like <span class="mono">K7M2P-QR9WX</span> that lasts 15 minutes and works once.</p>
             <form id="claimform" class="row" style="align-items:flex-end">
               <label class="f">Claim code<input id="claim-code" placeholder="K7M2P-QR9WX" style="width:170px" required></label>
               <button class="primary">Pair</button></form>`}
        <details><summary class="muted small">Advanced: server address</summary>
          <form id="urlform" class="row" style="align-items:flex-end;margin-top:10px">
            <label class="f">Base URL<input id="base-url" value="${esc(g.base_url)}" style="width:230px"></label>
            <button>Save</button></form>
          <p class="muted small">Changing this clears the pairing: a different destination has not been tested.</p></details>
        <div id="claim-msg" class="mono small"></div></div>

      <div class="card stack"><h2>2 · Which node is which plant</h2>
        <p class="muted small">Give each sensor the device id it should appear under on the site. A node with no id is never uploaded.</p>
        <div class="tablewrap"><table id="map-table"></table></div>
        <div class="row"><button class="primary" id="save-map">Save mapping</button>
          <span class="muted small">${r.queued} reading(s) waiting to be sent</span></div></div>
    </div>

    <div class="card stack" style="margin-top:16px"><h2>3 · Test before switching it on</h2>
      <div class="row"><button id="preview">Preview what would be sent</button>
        <button class="primary" id="sendnow" ${g.paired ? "" : "disabled"}>Send one now</button></div>
      <p class="muted small">Preview builds the exact request and touches no network. "Send one now" really uploads, and shows the site's reply.</p>
      <div id="g-out" class="log mono" hidden></div>
      <div class="kv" style="max-width:520px">
        <div class="k">Last attempt</div><div class="v small">${lastLine(r.last)}</div>
        <div class="k">Last success</div><div class="v small">${lastLine(r.last_success)}</div>
      </div></div>

    <div class="card stack" style="margin-top:16px"><h2>4 · Automatic uploads</h2>
      <div class="row">
        <button class="${g.enabled ? "danger" : "primary"}" id="toggle">${g.enabled ? "Turn uploads off" : "Turn uploads on"}</button>
        <span class="muted small">${r.last_success ? "" : "A send has to succeed first."}</span></div>
      <form id="cadence" class="row" style="align-items:flex-end">
        <label class="f">Upload every (s)<input id="g-interval" type="number" min="60" max="86400" value="${g.interval_seconds}" style="width:120px"></label>
        <label class="f">Minimum gap between samples (s)<input id="g-gap" type="number" min="60" max="86400" value="${g.min_gap_seconds}" style="width:150px"></label>
        <label class="f">Max samples per node<input id="g-max" type="number" min="1" max="500" value="${g.max_per_device}" style="width:120px"></label>
        <button>Save</button></form>
      <p class="muted small">Readings are sent from where the last upload finished, so an outage catches up instead of leaving a gap. Sample age is sent rather than a clock reading — the site stamps the time.</p></div>`;

  const out = (obj, label) => {
    const box = $("#g-out"); box.hidden = false;
    box.textContent = (label ? label + "\n\n" : "") + (typeof obj === "string" ? obj : JSON.stringify(obj, null, 1));
  };
  const tbl = $("#map-table");
  tbl.innerHTML = `<tr><th>Node</th><th>g4rden device id</th><th>Uploaded up to</th></tr>` +
    (soilNodes.map((n) => `<tr data-id="${n.id}"><td>${esc(nodeTitle(n))} <span class="muted small">#${n.id}</span></td>
      <td><input data-dev value="${esc(g.devices[n.id] || "")}" placeholder="hive-${n.id}" style="width:190px"></td>
      <td class="small muted">${r.marks[n.id] ? fmtTime(r.marks[n.id]) : "nothing sent yet"}</td></tr>`).join("") ||
      '<tr><td colspan="3" class="muted">No plant sensors seen yet.</td></tr>');

  if ($("#claimform")) $("#claimform").onsubmit = async (e) => {
    e.preventDefault();
    $("#claim-msg").textContent = "pairing…";
    try {
      const res = await api("/api/g4rden/claim", { code: $("#claim-code").value });
      if (res.ok) { toast("Paired with g4rden"); render(); }
      else out(res, "Pairing failed — codes are single-use and expire after 15 minutes.");
    } catch (err) { $("#claim-msg").textContent = err.message; }
  };
  if ($("#unpair")) $("#unpair").onclick = async () => {
    if (!confirm("Forget the g4rden pairing? Uploads stop until you pair again.")) return;
    await api("/api/g4rden/unpair", {}); toast("Pairing forgotten"); render();
  };
  $("#urlform").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/g4rden/config", { base_url: $("#base-url").value }); toast("Saved — pair again for this address"); render(); }
    catch (err) { toast(err.message); }
  };
  $("#save-map").onclick = async () => {
    const devices = {};
    tbl.querySelectorAll("tr[data-id]").forEach((tr) => { devices[tr.dataset.id] = $("[data-dev]", tr).value; });
    try { await api("/api/g4rden/config", { devices }); toast("Mapping saved"); render(); }
    catch (err) { toast(err.message); }
  };
  $("#preview").onclick = async () => {
    const p = await api("/api/g4rden/preview", {});
    const n = (p.body.nodes || []).reduce((a, x) => a + x.readings.length, 0);
    out(p, `POST ${p.url}\n${p.paired ? "Authorization: Bearer <token held on the Pi>" : "NOT PAIRED — this would be refused"}\n${n} reading(s):`);
  };
  $("#sendnow").onclick = async (e) => {
    e.target.disabled = true; out("sending…");
    try {
      const res = await api("/api/g4rden/send", {});
      out(res, res.ok ? "Upload accepted by g4rden." : "Upload failed.");
      if (res.ok) toast(`Sent ${res.sent} reading(s)`);
    } catch (err) { out(err.message, "Upload failed."); }
    finally { e.target.disabled = false; }
  };
  $("#toggle").onclick = async () => {
    try { await api("/api/g4rden/enable", { enabled: !g.enabled }); render(); }
    catch (err) { toast(err.message, 6000); }
  };
  $("#cadence").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/g4rden/config", { interval_seconds: +$("#g-interval").value,
        min_gap_seconds: +$("#g-gap").value, max_per_device: +$("#g-max").value });
      toast("Saved");
    } catch (err) { toast(err.message); }
  };
};

views.events = async (el) => {
  const ev = await api("/api/events?limit=300");
  el.innerHTML = `<div class="head"><h1>Activity</h1><div class="muted small">Commands, configuration changes, firmware and system events.</div></div>
    <div class="card"><div class="tablewrap"><table>
    <tr><th>When</th><th>Kind</th><th>What</th></tr>
    ${ev.map((e) => `<tr><td class="small" style="white-space:nowrap">${fmtTime(e.ts)}</td><td><span class="pill plain">${esc(e.kind)}</span></td><td>${esc(e.text)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">Nothing yet.</td></tr>'}
    </table></div></div>`;
};

// Problems: every Hivewire error code in plain words, and a way to report one.
// The report is built on the host with names, locations, addresses and tokens
// removed; it is shown here in full, editable, before anything leaves.
const MAX_ISSUE_URL = 7500;   // GitHub refuses much longer prefilled URLs
function issueUrl(repo, title, body) {
  const base = `https://github.com/${repo}/issues/new?title=${encodeURIComponent(title)}&body=`;
  let b = body, url = base + encodeURIComponent(b);
  if (url.length > MAX_ISSUE_URL) {
    const note = "\n\n*(Report cut short to fit a link. The full report was downloaded; attach it here.)*";
    while (b.length && (base + encodeURIComponent(b + note)).length > MAX_ISSUE_URL) b = b.slice(0, Math.floor(b.length * 0.9));
    return { url: base + encodeURIComponent(b + note), cut: true };
  }
  return { url, cut: false };
}
function saveText(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

views.problems = async (el) => {
  const p = await api("/api/problems");
  const nodeErrs = (STATE.nodes || []).filter((n) => n.error);
  const sev = (s) => (s === "error" ? "bad" : s === "info" ? "plain" : "warn");
  const codeRow = (e, extra) => `<div class="warn-line"><span class="pill ${sev(e.severity)}">${esc(e.label || "E" + e.code)}</span>
      <b>${esc(e.title)}</b>${extra ? ` <span class="muted small">${esc(extra)}</span>` : ""}
      ${e.cause ? `<div class="small muted">${esc(e.cause)}</div>` : ""}
      ${e.fix ? `<div class="small"><b>What to do:</b> ${esc(e.fix)}</div>` : ""}</div>`;
  el.innerHTML = `<div class="head"><h1>Problems</h1><div class="muted small">Hivewire error codes raised by the host, the gateway and the nodes. Every code is explained in ERRORS.md.</div></div>
    <div class="grid cols-2">
      <div class="card stack"><h2>This host</h2>
        ${p.active.map((a) => codeRow(a, a.detail + " — since " + fmtTime(a.since))).join("") || '<p class="muted">Nothing wrong right now.</p>'}</div>
      <div class="card stack"><h2>Nodes</h2>
        ${nodeErrs.map((n) => codeRow(n.error, nodeTitle(n) + (n.error.count ? " — " + n.error.count + " since boot" : ""))).join("") || '<p class="muted">No node has reported an error since it booted.</p>'}</div>
    </div>
    <div class="card stack" style="margin-top:16px"><h2>Report a problem</h2>
      <p class="muted small">Builds a report of this swarm's state for the Hivewire developers. Names, locations, network addresses and tokens are removed first. Read it, add what happened at the top, then open it as a GitHub issue (you need a GitHub account), or copy or download it to send another way.</p>
      <div class="row"><button id="rep-make" class="primary">Prepare report</button></div>
      <div id="rep" hidden class="stack">
        <input id="rep-title" style="width:100%">
        <textarea id="rep-body" rows="18" class="mono small" style="width:100%"></textarea>
        <div class="row"><button id="rep-open" class="primary">Open GitHub issue</button><button id="rep-copy">Copy</button><button id="rep-save">Download</button></div>
      </div></div>
    <div class="card" style="margin-top:16px"><h2>Recent error events</h2><div class="tablewrap"><table>
      <tr><th>When</th><th>What</th></tr>
      ${p.events.map((e) => `<tr><td class="small" style="white-space:nowrap">${fmtTime(e.ts)}</td><td>${esc(e.text)}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">None.</td></tr>'}
    </table></div></div>`;
  let repo = "";
  $("#rep-make").onclick = async () => {
    const r = await api("/api/problems/report");
    repo = r.repo;
    $("#rep-title").value = r.title; $("#rep-body").value = r.body; $("#rep").hidden = false;
    $("#rep-body").focus(); $("#rep-body").setSelectionRange(r.body.indexOf("\n") + 2, r.body.indexOf("\n") + 2);
  };
  $("#rep-open").onclick = () => {
    const { url, cut } = issueUrl(repo, $("#rep-title").value, $("#rep-body").value);
    if (cut) saveText("hivewire-report.md", $("#rep-body").value);
    window.open(url, "_blank", "noopener");
  };
  $("#rep-copy").onclick = async () => {
    try { await navigator.clipboard.writeText($("#rep-title").value + "\n\n" + $("#rep-body").value); toast("Copied"); }
    catch (_) { $("#rep-body").select(); toast("Select-all done: copy with Ctrl+C"); }
  };
  $("#rep-save").onclick = () => saveText("hivewire-report.md", "# " + $("#rep-title").value + "\n\n" + $("#rep-body").value);
};

views.settings = async (el) => {
  const cfg = await api("/api/config");
  const nodes = STATE.nodes;
  const kinds = Object.keys(KINDS);
  el.innerHTML = `
    <div class="head"><h1>Settings</h1></div>
    <div class="card"><h2>Nodes</h2>
      <p class="muted small">Soil calibration: put the probe in dry air and press <b>Use as dry</b>, then in water and press <b>Use as wet</b>. Moisture % everywhere, including past readings, is recalculated from the raw value; nothing is reflashed.</p>
      <p class="muted small">Battery correction: the divider resistors and the chip's converter are each a few percent out, so a full cell can read 4.13 V. Charge it fully and press <b>It is fully charged</b>, or measure the cell and enter the figure. Applies to stored history too.</p>
      <div class="tablewrap"><table id="nodes-table"></table></div></div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card stack"><h2>Polling</h2>
        <form id="pollform" class="row" style="align-items:flex-end">
          <label class="f">Poll every (s)<input id="p-poll" type="number" min="10" value="${cfg.poll_seconds}" style="width:110px"></label>
          <label class="f">Store unchanged values every (s)<input id="p-full" type="number" min="60" value="${cfg.full_every_seconds}" style="width:150px"></label>
          <label class="f">Warn when silent for (s)<input id="p-stale" type="number" min="30" value="${cfg.stale_seconds}" style="width:130px"></label>
          <button>Save</button></form>
        <p class="muted small">Polling uses the gateway's USB-only <span class="mono">dump</span> and costs no LoRa airtime.</p></div>
      <div class="card stack"><h2>Password</h2>
        <form id="pwform" class="row" style="align-items:flex-end">
          <label class="f">Current<input id="pw-old" type="password" autocomplete="current-password" required></label>
          <label class="f">New<input id="pw-new" type="password" autocomplete="new-password" minlength="8" required></label>
          <button>Change</button></form>
        <div class="row"><button id="logout">Sign out</button>
          <label class="f" style="flex-direction:row;align-items:center;gap:8px">Theme
            <select id="theme"><option value="">System</option><option value="light">Light</option><option value="dark">Dark</option></select></label></div></div>
    </div>`;
  const tbl = $("#nodes-table");
  tbl.innerHTML = `<tr><th>#</th><th>Name</th><th>Location</th><th>Type</th><th>Soil dry / wet (raw)</th><th>Battery correction</th><th>Hide</th><th></th></tr>` +
    nodes.map((n) => {
      const c = cfg.nodes[n.id] || {};
      const soil = n.kind === "SoilNode";
      return `<tr data-id="${n.id}">
        <td>${n.id}</td>
        <td><input data-k="name" value="${esc(c.name || "")}" placeholder="${esc(nodeTitle(n))}" style="width:150px"></td>
        <td><input data-k="location" value="${esc(c.location || "")}" style="width:140px"></td>
        <td><select data-k="kind"><option value="">auto (${esc(n.kind || "unknown")})</option>${kinds.map((k) => `<option ${c.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></td>
        <td>${soil ? `<div class="row"><input data-k="soil_dry" type="number" value="${c.soil_dry ?? ""}" placeholder="2800" style="width:80px">
          <input data-k="soil_wet" type="number" value="${c.soil_wet ?? ""}" placeholder="1200" style="width:80px"></div>
          <div class="row small" style="margin-top:6px"><span class="muted">now ${n.slots[3] ?? "—"}</span>
          <button type="button" data-cal="soil_dry">Use as dry</button><button type="button" data-cal="soil_wet">Use as wet</button></div>` : '<span class="muted">—</span>'}</td>
        <td>${n.slots[5] ? `<div class="row"><input data-k="batt_scale" type="number" step="0.001" min="0.8" max="1.25" value="${c.batt_scale ?? ""}" placeholder="1.000" style="width:90px"></div>
          <div class="row small" style="margin-top:6px"><span class="muted">reads ${(n.slots[5] / 1000).toFixed(2)} V</span>
          <button type="button" data-battfull>It is fully charged</button>
          <button type="button" data-battmeas>Enter measured V</button></div>` : '<span class="muted">—</span>'}</td>
        <td><input data-k="hidden" type="checkbox" ${c.hidden ? "checked" : ""}></td>
        <td><button class="primary" data-save>Save</button></td></tr>`;
    }).join("");
  tbl.onclick = async (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const nid = +tr.dataset.id;
    const battSet = (v) => {
      const raw = (nodes.find((n) => n.id === nid) || {}).slots?.[5];
      if (!raw) return toast("No battery reading from this node");
      $('[data-k="batt_scale"]', tr).value = (v * 1000 / raw).toFixed(4);
      toast(`Correction ${(v * 1000 / raw).toFixed(3)} — press Save to keep it`);
    };
    // A LiPo that has finished charging sits at 4.20 V, which makes a decent
    // reference without a meter. A measured value is better if you have one.
    if (e.target.dataset.battfull !== undefined) battSet(4.20);
    if (e.target.dataset.battmeas !== undefined) {
      const v = parseFloat(prompt("Battery voltage measured at the cell, in volts:", "4.20"));
      if (v >= 2.5 && v <= 4.5) battSet(v);
      else if (!isNaN(v)) toast("That is not a LiPo voltage (2.5-4.5 V)");
    }
    if (e.target.dataset.cal) {
      const raw = (nodes.find((n) => n.id === nid) || {}).slots?.[3];
      if (raw === undefined) return toast("No raw soil reading yet");
      $(`[data-k="${e.target.dataset.cal}"]`, tr).value = raw;
      toast("Set — press Save to keep it");
    }
    if (e.target.dataset.save !== undefined) {
      const body = { id: nid };
      tr.querySelectorAll("[data-k]").forEach((i) => (body[i.dataset.k] = i.type === "checkbox" ? i.checked || null : i.value.trim()));
      try { await api("/api/node", body); toast("Saved"); await refresh(true); } catch (err) { toast(err.message); }
    }
  };
  $("#pollform").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/settings", { poll_seconds: +$("#p-poll").value, full_every_seconds: +$("#p-full").value, stale_seconds: +$("#p-stale").value }); toast("Saved"); }
    catch (err) { toast(err.message); }
  };
  $("#pwform").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/password", { old: $("#pw-old").value, new: $("#pw-new").value }); toast("Password changed"); e.target.reset(); }
    catch (err) { toast(err.message); }
  };
  $("#logout").onclick = async () => { await api("/api/logout", {}); showLogin(); };
  const th = $("#theme");
  try { th.value = localStorage.getItem("hive-theme") || ""; } catch (_) {}
  th.onchange = () => { applyTheme(th.value); try { localStorage.setItem("hive-theme", th.value); } catch (_) {} };
};

// ---------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------
function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
try { applyTheme(localStorage.getItem("hive-theme") || ""); } catch (_) {}

async function refresh(rerender = false) {
  try {
    STATE = await api("/api/state");
    const c = $("#conn");
    if (STATE.pushing) { c.className = "pill warn"; c.textContent = "firmware push running"; }
    else if (STATE.error && !STATE.snapshot_time) { c.className = "pill bad"; c.textContent = "gateway: " + STATE.error; }
    else if (STATE.snapshot_time && STATE.time - STATE.snapshot_time > 300) { c.className = "pill warn"; c.textContent = "gateway quiet " + fmtAge(STATE.time - STATE.snapshot_time); }
    else if (STATE.snapshot_time) { c.className = "pill good"; c.textContent = STATE.fake ? "simulated" : "gateway ok"; }
    else { c.className = "pill"; c.textContent = "connecting…"; }
    const pr = $("#probs"), np = (STATE.problems || []).length;
    pr.hidden = !np;
    pr.textContent = np === 1 ? "E" + STATE.problems[0].code + " " + STATE.problems[0].title : np + " problems";
    const r = route();
    if (rerender || r.name === "dashboard") render();
  } catch (e) { /* login redirect handled in api() */ }
}

function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, arg] = h.split("/");
  if (FLASH_ONLY) return { name: "flash", arg };
  return { name: name || "dashboard", arg };
}

async function render() {
  const r = route();
  document.querySelectorAll("#nav a").forEach((a) => {
    const n = a.getAttribute("href").replace(/^#\/?/, "") || "dashboard";
    a.classList.toggle("on", n === r.name || (r.name === "node" && n === "dashboard"));
  });
  const v = views[r.name] || views.dashboard;
  const el = $("#view");
  try { await v(el, r.arg); } catch (e) { el.innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}

function showLogin(needsSetup) {
  clearInterval(timer);
  $("#shell").hidden = true; $("#login").hidden = false;
  $("#login-pw2").hidden = !needsSetup;
  $("#login-code").hidden = !needsSetup;
  $("#login-code").required = !!needsSetup;
  $("#login-pw2").required = !!needsSetup;
  $("#login-btn").textContent = needsSetup ? "Set password" : "Sign in";
  $("#login-msg").textContent = needsSetup ? "First run: enter the setup code from hive_data/setup_code.txt on the Pi, then choose a password (8+ characters)." : "Sign in to manage the swarm.";
  $("#login-form").onsubmit = async (e) => {
    e.preventDefault();
    const pw = $("#login-pw").value;
    if (needsSetup && pw !== $("#login-pw2").value) { $("#login-err").textContent = "Passwords do not match"; return; }
    const body = needsSetup ? { password: pw, code: $("#login-code").value } : { password: pw };
    try { await api(needsSetup ? "/api/setup" : "/api/login", body); $("#login-err").textContent = ""; $("#login-pw").value = ""; start(); }
    catch (err) { $("#login-err").textContent = err.message; }
  };
  $("#login-pw").focus();
}

async function start() {
  const s = await (await fetch("/api/session")).json();
  if (!s.authed) return showLogin(s.needs_setup);
  $("#login").hidden = true; $("#shell").hidden = false;
  if (s.flash_only) {
    // No swarm on this machine: just the Flash page, no polling of /api/state.
    FLASH_ONLY = true;
    document.querySelectorAll("#nav a").forEach((a) => { a.hidden = a.getAttribute("href") !== "#/flash"; });
    $("#conn").className = "pill"; $("#conn").textContent = "flash only";
    return render();
  }
  KINDS = await api("/api/kinds");
  await refresh();
  render();
  clearInterval(timer);
  timer = setInterval(refresh, 15000);
}

window.addEventListener("hashchange", render);
start();
