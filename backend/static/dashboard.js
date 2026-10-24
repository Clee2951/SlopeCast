// Central dashboard: projected landslide paths, who is inside them, how long they have.
// Displays stored results from /api/dashboard; it never recalculates risk or deadlines.
"use strict";
(() => {
  const LEVEL_TEXT = { monitoring: "Monitoring", elevated: "Elevated concern", urgent_review: "Urgent review" };
  const LK_COLOR = { high: "#d9483b", medium: "#f3b74a", low: "#cfcab8" };
  const ST_COLOR = { ok: "#2f7d4f", degraded: "#b7791f", unavailable: "#9a9891", "no-data": "#9a9891" };
  const app = document.getElementById("app");
  let state = null, map = null, layer = null, sortKey = "leave_by_utc", fitted = false;

  app.innerHTML = `
    <section id="banner" class="banner" style="background:#666"><div class="level">...</div></section>
    <div class="grid">
      <div class="card"><h2>Projected landslide paths and homes</h2><div id="map"></div>
        <div class="legend">
          <span><i style="background:${LK_COLOR.high}"></i>path likely hit (high)</span>
          <span><i style="background:${LK_COLOR.medium}"></i>possible (medium)</span>
          <span><i style="background:${LK_COLOR.low}"></i>not indicated</span>
          <span><i style="background:#d9483b;border-radius:50%"></i>exposed home</span>
          <span><i style="background:#7b3fb3;border-radius:50%"></i>needs help</span>
          <span><i style="background:#6f8f73;border-radius:50%"></i>not exposed</span>
          <span><i style="background:#2a6fb0"></i>shelter</span>
          <span>&#9650; sensor stake</span>
        </div>
      </div>
      <div class="stack">
        <div class="card"><h2>Sensor stakes</h2><div id="stakes" class="stakes"></div></div>
        <div class="card"><h2>Rainfall (Apify weather)</h2><div id="weather"></div></div>
        <div class="card"><h2>Hazard engine</h2><div id="assess"></div></div>
      </div>
    </div>
    <div id="planbar" class="planbar"></div>
    <div class="tablewrap"><table id="rows"></table></div>
    <div class="grid">
      <div class="card"><h2>Event log</h2><div id="events" class="log"></div></div>
      <div class="card"><h2>Resident message outbox (simulated)</h2><div id="outbox" class="log"></div></div>
    </div>`;

  // ---------------------------------------------------------------- map
  function setupMap(d) {
    if (window.L) {
      map = L.map("map", { zoomControl: true }).setView(d.site.center, d.site.zoom || 16);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap" }).addTo(map);
      layer = L.layerGroup().addTo(map);
    }
  }
  function drawLeaflet(d) {
    layer.clearLayers();
    const bounds = [];
    d.zones.forEach((z) => {
      const lk = z.likelihood || "low";
      const latlngs = z.ring.map(([lon, lat]) => [lat, lon]);
      latlngs.forEach((p) => bounds.push(p));
      L.polygon(latlngs, { color: LK_COLOR[lk], weight: lk === "low" ? 1 : 2, fillColor: LK_COLOR[lk], fillOpacity: lk === "high" ? 0.45 : lk === "medium" ? 0.35 : 0.12 })
        .bindTooltip(`<b>${SC.esc(z.zone_id)} ${SC.esc(z.name)}</b><br>${SC.esc(lk)} likelihood<br>${SC.esc((z.reasons || []).join("; "))}<br><i>${SC.esc(z.severity_if_hit)}</i><br><small>${SC.esc(z.origin)}</small>`, { sticky: true })
        .addTo(layer);
      if (lk !== "low") L.tooltip({ permanent: true, direction: "center", className: "zone-label" }).setLatLng(latlngs[0]).setContent(z.zone_id).addTo(layer);
    });
    d.buildings.forEach((b) => {
      const color = !b.exposed ? "#6f8f73" : b.needs_help ? "#7b3fb3" : "#d9483b";
      L.circleMarker([b.lat, b.lon], { radius: b.exposed ? 7 : 5, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: 1 })
        .bindTooltip(`${SC.esc(b.households.join(", "))}<br>${b.exposed ? "exposed (" + SC.esc(b.likelihood) + ")" : "not exposed"}${b.needs_help ? "<br>needs help" : ""}${b.status ? "<br>" + SC.esc(b.status) : ""}`)
        .addTo(layer);
    });
    d.shelters.forEach((s) => {
      L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "", html: `<div style="background:#2a6fb0;color:#fff;font-weight:700;padding:1px 4px;border-radius:3px;font-size:11px">${SC.esc(s.shelter_id)}</div>` }) })
        .bindTooltip(`${SC.esc(s.name)}<br>${s.wheelchair_accessible ? "wheelchair accessible" : "not accessible"}; route ${SC.esc(s.route_status)}`).addTo(layer);
      bounds.push([s.lat, s.lon]);
    });
    d.nodes.forEach((n) => {
      const c = ST_COLOR[n.status] || "#9a9891";
      L.marker([n.lat, n.lon], { icon: L.divIcon({ className: "", html: `<div style="color:${c};font-size:22px;line-height:22px;text-shadow:0 0 3px #fff">&#9650;</div><div style="font-weight:700;font-size:11px;text-shadow:0 0 3px #fff">${SC.esc(n.node_id)}${n.simulated ? "*" : ""}</div>` }) })
        .bindTooltip(`<b>${SC.esc(n.node_id)}</b> ${SC.esc(n.sector)} (${SC.esc(n.status)})<br>tilt ${SC.num(n.tilt_deg, 2)} deg, trend ${SC.num(n.tilt_trend_deg_per_h)} deg/h<br>wetness ${SC.num(n.wetness_index, 2)}<br>${SC.esc(n.source || "no source")}`).addTo(layer);
      bounds.push([n.lat, n.lon]);
    });
    if (!fitted && bounds.length) { map.fitBounds(bounds, { padding: [20, 20] }); fitted = true; }
  }
  function drawSvg(d) {           // offline fallback: same layers without a basemap
    const el = document.getElementById("map");
    const pts = [];
    d.zones.forEach((z) => z.ring.forEach(([lon, lat]) => pts.push([lat, lon])));
    d.nodes.forEach((n) => pts.push([n.lat, n.lon])); d.shelters.forEach((s) => pts.push([s.lat, s.lon]));
    const la = pts.map((p) => p[0]), lo = pts.map((p) => p[1]);
    const W = el.clientWidth || 600, H = el.clientHeight || 520, pad = 24;
    const minLa = Math.min(...la), maxLa = Math.max(...la), minLo = Math.min(...lo), maxLo = Math.max(...lo);
    const k = Math.min((W - 2 * pad) / ((maxLo - minLo) * 0.92 || 1), (H - 2 * pad) / ((maxLa - minLa) || 1));
    const X = (lon) => pad + (lon - minLo) * 0.92 * k, Y = (lat) => H - pad - (lat - minLa) * k;
    let s = `<svg class="svgmap" width="${W}" height="${H}">`;
    d.zones.forEach((z) => {
      const lk = z.likelihood || "low";
      s += `<polygon points="${z.ring.map(([lon, lat]) => X(lon) + "," + Y(lat)).join(" ")}" fill="${LK_COLOR[lk]}" fill-opacity="${lk === "low" ? 0.2 : 0.5}" stroke="${LK_COLOR[lk]}"><title>${SC.esc(z.zone_id + " " + z.name + ": " + lk)}</title></polygon>`;
      s += `<text x="${X(z.ring[0][0]) + 4}" y="${Y(z.ring[0][1]) + 12}">${SC.esc(z.zone_id)}</text>`;
    });
    d.buildings.forEach((b) => { s += `<circle cx="${X(b.lon)}" cy="${Y(b.lat)}" r="${b.exposed ? 6 : 4}" fill="${!b.exposed ? "#6f8f73" : b.needs_help ? "#7b3fb3" : "#d9483b"}" stroke="#fff"><title>${SC.esc(b.households.join(", "))}</title></circle>`; });
    d.shelters.forEach((h) => { s += `<rect x="${X(h.lon) - 9}" y="${Y(h.lat) - 7}" width="18" height="14" fill="#2a6fb0"/><text x="${X(h.lon) + 12}" y="${Y(h.lat) + 4}">${SC.esc(h.shelter_id)}</text>`; });
    d.nodes.forEach((n) => { s += `<text x="${X(n.lon) - 6}" y="${Y(n.lat) + 6}" style="font-size:18px;fill:${ST_COLOR[n.status]}">&#9650;</text><text x="${X(n.lon) + 10}" y="${Y(n.lat)}">${SC.esc(n.node_id)}</text>`; });
    el.innerHTML = s + `</svg><div class="muted small" style="margin-top:-20px;padding-left:8px">offline map (no basemap)</div>`;
  }

  // ---------------------------------------------------------------- panels
  function banner(d) {
    const a = d.assessment, el = document.getElementById("banner");
    if (!a) { el.style.background = "#666"; el.innerHTML = `<div class="level">Waiting for data</div><div class="summary">No assessment yet. Plug in the stake or start a replay.</div>`; return; }
    el.style.background = getComputedStyle(document.documentElement).getPropertyValue("--" + a.risk_level) || "#666";
    const cd = d.countdown;
    el.innerHTML = `<div><div class="level">${SC.esc(LEVEL_TEXT[a.risk_level] || a.risk_level)}</div>
        <small>${a.engine.startsWith("claude") ? "assessed by Claude (" + SC.esc(a.engine.slice(7)) + "), checked by rules" : a.engine === "rules-fallback" ? "Claude unavailable: rule-based fallback" : "rule-based engine"} · ${SC.local(a.created_at_utc)}</small></div>
      <div class="summary">${SC.esc(a.summary)}</div>
      <div class="countdown">${cd ? `<small>${SC.esc(cd.label)}</small><div class="big mono" id="cd" data-t="${cd.time_utc}">${SC.untilText(cd.time_utc)}</div><small>by ${SC.local(cd.time_utc)} site time</small>` : `<small>No leave-by deadline set</small>`}</div>`;
  }
  function stakes(d) {
    document.getElementById("stakes").innerHTML = d.nodes.map((n) => `
      <div class="stake"><div class="id"><span class="dot st-${SC.esc(n.status)}"></span>${SC.esc(n.node_id)} <span class="muted small">${SC.esc(n.sector)}</span></div>
        <div class="kv small"><span>tilt</span><span class="mono">${SC.num(n.tilt_deg, 2)}&deg;</span>
        <span>trend</span><span class="mono">${SC.num(n.tilt_trend_deg_per_h)} &deg;/h</span>
        <span>wetness</span><span class="mono">${SC.num(n.wetness_index, 2)} <span class="muted">(raw ${SC.num(n.moisture_raw, 0)})</span></span>
        <span>source</span><span>${SC.esc(n.simulated ? "simulated" : n.source || "none")}</span></div>
        ${n.reasons.filter((r) => !r.startsWith("info:window")).length ? `<div class="muted small" title="${SC.esc(n.reasons.join("\n"))}">${SC.esc(n.reasons.filter((r) => !r.startsWith("info:window")).slice(0, 2).map((r) => r.replace(/^info:/, "").replace(/_/g, " ")).join(", "))}</div>` : ""}
      </div>`).join("");
  }
  function weather(d) {
    const w = d.weather, el = document.getElementById("weather");
    if (!w) { el.innerHTML = `<span class="muted">No weather yet.</span>`; return; }
    const hrs = w.hours.slice(-48), max = Math.max(1, ...hrs.map((h) => h.mm || 0));
    const nowIdx = hrs.findIndex((h) => h.kind === "forecast");
    el.innerHTML = `<div class="kv"><span>past 24 h</span><span class="mono">${SC.num(w.rain_24h_mm)} mm</span>
      <span>next 24 h</span><span class="mono">${SC.num(w.forecast_24h_mm)} mm</span>
      <span>peak</span><span class="mono">${SC.num(w.peak_mm_per_h)} mm/h</span>
      <span>threshold</span><span>${w.threshold_crossing_utc ? "rain threshold crossed " + SC.local(w.threshold_crossing_utc, true) + " (not a landslide time)" : "not crossed in forecast"}</span>
      <span>source</span><span class="small">${SC.esc(w.source.split(" (")[0])}${w.stale ? ' <b style="color:var(--bad)">STALE</b>' : ""}${w.simulated ? " (simulated)" : ""}</span></div>
      <div class="rainbars" title="hourly rainfall, past (dark) and forecast (light)">${hrs.map((h, i) => `<div class="${h.kind === "forecast" ? "fc" : ""} ${i === nowIdx ? "now" : ""}" style="height:${((h.mm || 0) / max) * 100}%" title="${SC.local(h.time_utc, true)}: ${h.mm === null ? "missing" : h.mm + " mm"}"></div>`).join("")}</div>
      ${w.error ? `<div class="muted small">${SC.esc(w.error)}</div>` : ""}`;
  }
  function assess(d) {
    const a = d.assessment, el = document.getElementById("assess");
    if (!a) { el.innerHTML = `<span class="muted">No assessment yet.</span>`; return; }
    const named = d.zones.filter((z) => z.likelihood && z.likelihood !== "low");
    el.innerHTML = `<div class="kv"><span>paths at risk</span><span>${named.length ? named.map((z) => `<span class="lk lk-${z.likelihood}">${SC.esc(z.zone_id)} ${z.likelihood}</span>`).join(" ") : "none"}</span>
      <span>timing</span><span>${a.timing_status === "experimental" ? `experimental: ${SC.local(a.timing_window.earliest_utc)} to ${SC.local(a.timing_window.latest_utc)}<br><span class="muted small">${SC.esc(a.timing_window.basis)}</span>` : "unavailable (not enough evidence)"}</span>
      <span>gaps</span><span>${a.coverage_gaps.length ? `<ul class="tight">${a.coverage_gaps.map((g) => `<li>${SC.esc(g)}</li>`).join("")}</ul>` : "none"}</span></div>
      ${named.map((z) => `<div class="small" style="margin-top:6px"><b>${SC.esc(z.zone_id)}</b> ${SC.esc(z.name)}: ${SC.esc((z.reasons || []).join("; "))}</div>`).join("")}
      ${a.notes.length ? `<div class="muted small" style="margin-top:6px">Checks: ${SC.esc(a.notes.join("; "))}</div>` : ""}`;
  }
  function planbar(d) {
    const p = d.plan, ap = d.approved_plan, el = document.getElementById("planbar");
    if (!p) { el.innerHTML = `<span class="muted">No plan yet.</span>`; return; }
    const op = d.role === "operator", c = p.counts;
    const basisText = { "hazard-timing": "from the experimental timing fit", operator: "set by the operator", "policy-default": "policy default for this risk level (demo setting)", none: "none: no deadlines" }[p.horizon_basis] || p.horizon_basis;
    el.innerHTML = `<b>${SC.esc(p.plan_id)}</b> v${p.version} <span class="chip">${SC.esc(p.state)}</span>
      <span class="small">Unsafe from: <b>${p.horizon_utc ? SC.local(p.horizon_utc) : "-"}</b> <span class="muted">(${SC.esc(basisText)})</span></span>
      <span class="chip">exposed <b>${c.exposed}</b></span><span class="chip">need help <b>${c.need_help}</b></span>
      <span class="chip">team assigned <b>${c.team_assigned}</b></span><span class="chip">accepted <b>${c.accepted}</b></span>
      <span class="chip">acknowledged <b>${c.acknowledged}</b></span><span class="chip">evacuated <b>${c.evacuated}</b></span>
      ${c.infeasible ? `<span class="chip" style="color:var(--bad)">infeasible <b>${c.infeasible}</b></span>` : ""}
      <span class="spacer"></span>
      ${ap ? `<span class="small muted">approved: ${SC.esc(ap.plan_id)} by ${SC.esc(ap.approved_by)}</span>` : `<span class="small muted">no plan approved yet</span>`}
      ${op ? `<span class="small">Horizon <input id="hz" type="number" min="5" step="5" placeholder="min"> <button id="hz-set">Set</button> <button id="hz-clear">Clear</button></span>
        <button id="reassess">Reassess now</button>
        <a class="btn" href="/api/plans/${encodeURIComponent(p.plan_id)}/export.csv?token=${encodeURIComponent(SC.token())}">CSV</a>
        <button id="approve" class="primary" ${p.state === "approved" ? "disabled" : ""}>${p.state === "approved" ? "Approved" : "Approve plan v" + p.version}</button>` : ""}`;
    if (op) {
      document.getElementById("approve").onclick = () => act(SC.post(`/api/plans/${p.plan_id}/approve`, { version: p.version }));
      document.getElementById("reassess").onclick = () => act(SC.post("/api/reassess"));
      document.getElementById("hz-set").onclick = () => act(SC.post("/api/horizon", { minutes: Number(document.getElementById("hz").value) }));
      document.getElementById("hz-clear").onclick = () => act(SC.post("/api/horizon", { minutes: null }));
    }
  }
  function table(d) {
    const rows = d.rows.slice().sort((a, b) => String(a[sortKey] || "~").localeCompare(String(b[sortKey] || "~")));
    const det = d.role === "operator" || d.role === "committee";
    const el = document.getElementById("rows");
    if (!rows.length) { el.innerHTML = `<tr><td class="muted">No exposed households in the current plan.</td></tr>`; return; }
    const cols = [["household_id", "Household"], ["likelihood", "Zone"], ["needs_help", "Needs help"], ["prep_min", "Prep / travel"],
      ["prepare_by_utc", "Start packing"], ["leave_by_utc", "Leave by"], ["team_id", "Team / arrive by"], ["destination_id", "Go to"], ["status", "Status"], ["feasible", "Problems"]];
    el.innerHTML = `<tr>${cols.map(([k, l]) => `<th data-k="${k}">${l}${sortKey === k ? " &#9662;" : ""}</th>`).join("")}</tr>` +
      rows.map((r) => `<tr class="${r.feasible === false ? "infeasible" : ""}">
        <td><b>${SC.esc(r.household_id)}</b>${det && r.name ? "<br><span class='small muted'>" + SC.esc(r.name) + "</span>" : ""}<br><span class="small muted">${r.people_count || "?"} people</span></td>
        <td>${SC.esc(r.zone_id || "-")} <span class="lk lk-${SC.esc(r.likelihood)}">${SC.esc(r.likelihood)}</span></td>
        <td>${r.needs_help ? "yes" : "no"}${det && r.assistance && r.assistance.length ? "<br><span class='small'>" + SC.esc(r.assistance.join(", ")) + "</span>" : ""}</td>
        <td class="mono">${SC.num(r.prep_min, 0)} / ${SC.num(r.travel_min, 0)} min</td>
        <td class="mono">${SC.local(r.prepare_by_utc)}</td>
        <td class="mono"><b>${SC.local(r.leave_by_utc)}</b>${r.leave_by_utc ? "<br><span class='small muted'>" + SC.untilText(r.leave_by_utc) + "</span>" : ""}</td>
        <td>${r.team_id ? SC.esc(r.team_id) + (r.assignment_state ? " <span class='small muted'>(" + SC.esc(r.assignment_state) + ")</span>" : "") + "<br><span class='mono small'>by " + SC.local(r.arrival_by_utc) + "</span>" : "-"}</td>
        <td>${SC.esc(r.destination_name || r.destination_id || "-")}</td>
        <td>${SC.esc(r.status)}</td>
        <td class="problems">${SC.esc((r.problems || []).join("; "))}</td></tr>`).join("");
    el.querySelectorAll("th").forEach((th) => (th.onclick = () => { sortKey = th.dataset.k; table(state); }));
  }
  function logs(d) {
    document.getElementById("events").innerHTML = d.events.map((e) => `<div>${SC.local(e.time_utc)} ${SC.esc(e.msg)}</div>`).join("") || "<div class='muted'>-</div>";
    document.getElementById("outbox").innerHTML = d.role !== "operator" ? "<div class='muted'>Sign in as operator to see messages.</div>"
      : (d.outbox.slice().reverse().map((m) => `<div><b>${SC.esc(m.household_id)}</b> [${SC.esc(m.status)}] ${SC.esc(m.text)}</div>`).join("") || "<div class='muted'>No messages yet. Approving a plan sends them.</div>");
  }
  async function act(promise) {
    try { await promise; await refresh(); } catch (e) { alertBox(e.message); }
  }
  function alertBox(msg) {
    const b = document.getElementById("planbar");
    const div = document.createElement("div"); div.className = "problems"; div.textContent = msg; b.appendChild(div);
    setTimeout(() => div.remove(), 6000);
  }

  async function refresh() {
    const d = await SC.get("/api/dashboard");
    state = d;
    SC.clock(d.site.name); SC.modeBadge(d);
    if (!map && window.L) setupMap(d);
    if (map) drawLeaflet(d); else drawSvg(d);
    banner(d); stakes(d); weather(d); assess(d);
    if (!document.activeElement || document.activeElement.id !== "hz") planbar(d);
    table(d); logs(d);
  }
  SC.poll(refresh, 2000);
  setInterval(() => { const el = document.getElementById("cd"); if (el) el.textContent = SC.untilText(el.dataset.t); SC.clock(); }, 1000);
})();
