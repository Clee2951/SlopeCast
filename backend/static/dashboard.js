// Central dashboard: projected landslide paths, who is inside them, how long they have.
// Displays stored results from /api/dashboard; it never recalculates risk or deadlines.
"use strict";
(() => {
  const LEVEL_TEXT = { monitoring: "Monitoring", elevated: "Elevated concern", urgent_review: "Urgent review" };
  const LEVEL_ZH = { monitoring: "監測中", elevated: "警戒提高", urgent_review: "緊急評估" };
  const LK_COLOR = { high: "#E2401B", medium: "#E39B12", low: "#9C978A" };
  const ST_COLOR = { ok: "#3F6B4E", degraded: "#E39B12", unavailable: "#9C978A", "no-data": "#9C978A" };
  const HOT_TREND = 0.5;          // deg/h shown in vermilion; display only, the hazard engine decides risk
  const app = document.getElementById("app");
  let state = null, map = null, layer = null, sortKey = "leave_by_utc", fitted = false, tileFallback = false, userMoved = false, lastBounds = null;

  app.innerHTML = `
    <section id="banner" class="banner rise" style="--i:0"><div><div class="level">…</div></div></section>
    <section id="ruler" class="ruler rise" style="--i:1"></section>
    <div class="grid">
      <div class="card mapcard rise" style="--i:2"><h2>Projected landslide paths &amp; homes</h2><div id="map"></div>
        <div class="legend">
          <span><i style="background:repeating-linear-gradient(45deg,${LK_COLOR.high} 0 3px,#f6d6cc 3px 6px)"></i>path likely hit</span>
          <span><i class="dot" style="background:${LK_COLOR.high}"></i>exposed home</span>
          <span><i style="background:repeating-linear-gradient(45deg,${LK_COLOR.medium} 0 2px,#f7ead0 2px 6px)"></i>possible</span>
          <span><i class="dot" style="background:#16181A;box-shadow:0 0 0 2px ${LK_COLOR.high}"></i>needs help</span>
          <span><i style="border-style:dashed;background:transparent"></i>not indicated</span>
          <span><i class="dot" style="background:transparent;border:2px solid #3F6B4E"></i>not exposed</span>
          <span><i style="background:#24508A;border-color:#24508A"></i>shelter</span>
          <span><i style="border:0;width:auto;height:auto;color:#16181A">▲</i>sensor stake</span>
        </div>
      </div>
      <div class="stack">
        <div class="card rise" style="--i:3"><h2>Sensor stakes <span class="aside" id="stake-count"></span></h2><div id="stakes" class="stakes"></div></div>
        <div class="card rise" style="--i:4"><h2>Rainfall <span class="aside">Apify weather</span></h2><div id="weather"></div></div>
        <div class="card rise" style="--i:5"><h2>Hazard engine</h2><div id="assess"></div></div>
      </div>
    </div>
    <div id="planbar" class="planbar rise" style="--i:6"></div>
    <div class="tablehead rise" style="--i:7"><h2>Exposed households <span class="aside">click a column to sort</span></h2></div>
    <div class="tablewrap rise" style="--i:7"><table id="rows"></table></div>
    <div class="grid rise" style="--i:8">
      <div class="card"><h2>Event log</h2><div id="events" class="log"></div></div>
      <div class="card"><h2>Resident message outbox <span class="aside">simulated</span></h2><div id="outbox" class="log"></div></div>
    </div>`;

  // ---------------------------------------------------------------- map
  function setupMap(d) {
    if (!window.L) return;
    map = L.map("map", { zoomControl: false, scrollWheelZoom: false }).setView(d.site.center, d.site.zoom || 16);
    // topographic basemap (contours suit a slope); falls back to OpenStreetMap if its tiles fail
    const topo = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", { maxZoom: 17, attribution: "&copy; OpenStreetMap, SRTM | &copy; OpenTopoMap" });
    topo.on("tileerror", () => {
      if (tileFallback) return; tileFallback = true;
      map.removeLayer(topo);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap" }).addTo(map);
    });
    topo.addTo(map);
    L.control.zoom({ position: "topright" }).addTo(map);
    layer = L.layerGroup().addTo(map);
    map.on("dragstart", () => { userMoved = true; });
    if (window.ResizeObserver) new ResizeObserver(() => { map.invalidateSize(); if (lastBounds && !userMoved) map.fitBounds(lastBounds, { padding: [30, 30] }); }).observe(document.getElementById("map"));
    map.on("click", () => map.scrollWheelZoom.enable());
  }
  function stakeIcon(n) {
    const c = ST_COLOR[n.status] || "#9C978A";
    const moving = (n.tilt_trend_deg_per_h || 0) >= HOT_TREND;
    return L.divIcon({ className: "", iconSize: [26, 26], iconAnchor: [13, 20],
      html: `<div class="stake-pin">${moving ? '<div class="ring"></div>' : ""}<svg viewBox="0 0 26 26"><path d="M13 3 L23 21 L3 21 Z" fill="${c}" stroke="#16181A" stroke-width="1.5" stroke-linejoin="round"/></svg><span>${SC.esc(n.node_id)}${n.simulated ? "*" : ""}</span></div>` });
  }
  function drawLeaflet(d) {
    layer.clearLayers();
    const bounds = [];
    d.zones.forEach((z) => {
      const lk = z.likelihood || "low";
      const latlngs = z.ring.map(([lon, lat]) => [lat, lon]);
      latlngs.forEach((p) => bounds.push(p));
      L.polygon(latlngs, { className: "zone zone-" + lk, color: lk === "low" ? "#77756D" : LK_COLOR[lk], weight: lk === "low" ? 1 : 2.5, fillColor: LK_COLOR[lk], fillOpacity: lk === "low" ? 0.06 : 0.4 })
        .bindTooltip(`<b>${SC.esc(z.zone_id)} ${SC.esc(z.name)}</b><br>${SC.esc(lk)} likelihood<br>${SC.esc((z.reasons || []).join("; "))}<br><i>${SC.esc(z.severity_if_hit)}</i><br><small>${SC.esc(z.origin)}</small>`, { sticky: true })
        .addTo(layer);
      if (lk !== "low") L.tooltip({ permanent: true, direction: "center", className: "zone-label" }).setLatLng(latlngs[0]).setContent(z.zone_id).addTo(layer);
    });
    d.buildings.forEach((b) => {
      const evac = b.status === "evacuated";
      const fill = !b.exposed ? "transparent" : evac ? "#3F6B4E" : b.needs_help ? "#16181A" : LK_COLOR.high;
      const stroke = !b.exposed ? "#3F6B4E" : b.needs_help && !evac ? LK_COLOR.high : "#F7F4EC";
      L.circleMarker([b.lat, b.lon], { radius: b.exposed ? 7 : 5, color: stroke, weight: b.needs_help ? 3 : 2, fillColor: fill, fillOpacity: 1 })
        .bindTooltip(`<b>${SC.esc(b.households.join(", "))}</b><br>${b.exposed ? "exposed (" + SC.esc(b.likelihood) + ")" : "not exposed"}${b.needs_help ? "<br>needs help" : ""}${b.status ? "<br>" + SC.esc(b.status) : ""}`)
        .addTo(layer);
    });
    d.shelters.forEach((s) => {
      L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "", iconAnchor: [12, 10], html: `<div class="shelter-pin">${SC.esc(s.shelter_id)}</div>` }) })
        .bindTooltip(`<b>${SC.esc(s.name)}</b><br>${s.wheelchair_accessible ? "wheelchair accessible" : "not accessible"}; route ${SC.esc(s.route_status)}`).addTo(layer);
      bounds.push([s.lat, s.lon]);
    });
    d.nodes.forEach((n) => {
      L.marker([n.lat, n.lon], { icon: stakeIcon(n) })
        .bindTooltip(`<b>${SC.esc(n.node_id)}</b> ${SC.esc(n.sector)} (${SC.esc(n.status)})<br>tilt ${SC.num(n.tilt_deg, 2)}°, trend ${SC.num(n.tilt_trend_deg_per_h)}°/h<br>wetness ${SC.num(n.wetness_index, 2)}`).addTo(layer);
      bounds.push([n.lat, n.lon]);
    });
    if (bounds.length) lastBounds = bounds;
    if (!fitted && bounds.length) { map.fitBounds(bounds, { padding: [30, 30] }); fitted = true; }
  }
  function drawSvg(d) {           // offline fallback: same layers without a basemap
    const el = document.getElementById("map");
    const pts = [];
    d.zones.forEach((z) => z.ring.forEach(([lon, lat]) => pts.push([lat, lon])));
    d.nodes.forEach((n) => pts.push([n.lat, n.lon])); d.shelters.forEach((s) => pts.push([s.lat, s.lon]));
    if (!pts.length) return;
    const la = pts.map((p) => p[0]), lo = pts.map((p) => p[1]);
    const W = el.clientWidth || 600, H = el.clientHeight || 520, pad = 36;
    const minLa = Math.min(...la), maxLa = Math.max(...la), minLo = Math.min(...lo), maxLo = Math.max(...lo);
    const k = Math.min((W - 2 * pad) / ((maxLo - minLo) * 0.92 || 1), (H - 2 * pad) / ((maxLa - minLa) || 1));
    const X = (lon) => pad + (lon - minLo) * 0.92 * k, Y = (lat) => H - pad - (lat - minLa) * k;
    let s = `<svg class="svgmap" width="${W}" height="${H}">`;
    d.zones.forEach((z) => {
      const lk = z.likelihood || "low";
      const fill = lk === "high" ? "url(#hatch-high)" : lk === "medium" ? "url(#hatch-medium)" : "none";
      s += `<polygon points="${z.ring.map(([lon, lat]) => X(lon) + "," + Y(lat)).join(" ")}" fill="${fill}" stroke="${LK_COLOR[lk]}" stroke-width="${lk === "low" ? 1 : 2}" ${lk === "low" ? 'stroke-dasharray="4 4"' : ""}><title>${SC.esc(z.zone_id + " " + z.name + ": " + lk)}</title></polygon>`;
      s += `<text x="${X(z.ring[0][0]) + 4}" y="${Y(z.ring[0][1]) + 12}">${SC.esc(z.zone_id)}</text>`;
    });
    d.buildings.forEach((b) => { s += `<circle cx="${X(b.lon)}" cy="${Y(b.lat)}" r="${b.exposed ? 6 : 4}" fill="${!b.exposed ? "none" : b.needs_help ? "#16181A" : LK_COLOR.high}" stroke="${!b.exposed ? "#3F6B4E" : b.needs_help ? LK_COLOR.high : "#fff"}" stroke-width="2"><title>${SC.esc(b.households.join(", "))}</title></circle>`; });
    d.shelters.forEach((h) => { s += `<rect x="${X(h.lon) - 9}" y="${Y(h.lat) - 7}" width="18" height="14" fill="#24508A"/><text x="${X(h.lon) + 12}" y="${Y(h.lat) + 4}">${SC.esc(h.shelter_id)}</text>`; });
    d.nodes.forEach((n) => { s += `<path d="M${X(n.lon)} ${Y(n.lat) - 9} l8 14 h-16 z" fill="${ST_COLOR[n.status] || "#9C978A"}" stroke="#16181A"/><text x="${X(n.lon) + 11}" y="${Y(n.lat)}">${SC.esc(n.node_id)}</text>`; });
    el.innerHTML = s + `</svg><div class="offline-map-note">offline map (no basemap)</div>`;
  }

  // ---------------------------------------------------------------- level band
  function banner(d) {
    const a = d.assessment, el = document.getElementById("banner");
    if (!a) {
      el.className = "banner";
      el.innerHTML = `<div><div class="eyebrow">Hazard level</div><div class="level">Waiting<br>for data</div></div><div class="summary">No assessment yet. Plug in the stake or start a replay.</div><div></div>`;
      return;
    }
    el.className = "banner rise lv-" + a.risk_level;
    const cd = d.countdown;
    const engine = a.engine.startsWith("claude") ? "Assessed by Claude (" + SC.esc(a.engine.slice(7)) + "), checked by rules"
      : a.engine === "rules-fallback" ? "Claude unavailable: rule-based fallback" : "Rule-based engine";
    el.innerHTML = `<div><div class="eyebrow">Hazard level · ${SC.local(a.created_at_utc)}</div>
        <div class="level">${SC.esc(LEVEL_TEXT[a.risk_level] || a.risk_level)}</div>
        <div class="level-zh">${SC.esc(LEVEL_ZH[a.risk_level] || "")}</div>
        <div class="engine">${engine}</div></div>
      <div class="summary">${SC.esc(a.summary)}</div>
      <div class="countdown">${cd ? `<div class="label">${SC.esc(cd.label)}</div><div class="big" id="cd" data-clock="${SC.esc(cd.time_utc)}">${SC.clockText(cd.time_utc)}</div><div class="by">by ${SC.local(cd.time_utc)} site time</div>`
        : `<div class="label">Countdown</div><div class="none">No leave-by<br>deadline set</div>`}</div>`;
  }

  // ---------------------------------------------------------------- timeline ruler: every deadline on one axis
  function ruler(d) {
    const el = document.getElementById("ruler");
    const p = d.plan, a = d.assessment, w = d.weather;
    const t0 = SC.now().getTime();
    const times = [];
    (d.rows || []).forEach((r) => { if (r.leave_by_utc) times.push(Date.parse(r.leave_by_utc)); if (r.arrival_by_utc) times.push(Date.parse(r.arrival_by_utc)); });
    if (p && p.horizon_utc) times.push(Date.parse(p.horizon_utc));
    if (a && a.timing_window) times.push(Date.parse(a.timing_window.latest_utc));
    const thr = w && w.threshold_crossing_utc ? Date.parse(w.threshold_crossing_utc) : null;
    if (thr) times.push(thr);
    const head = `<div class="ruler-head"><h2>Time to act <span class="aside">now → unsafe-from, site time</span></h2><div class="spacer"></div>
      <div class="ruler-key"><span><i style="background:var(--vermilion)"></i>leave by</span><span><i style="background:var(--ink)"></i>leave by, needs help</span>
      <span><i style="background:var(--indigo);border-radius:50%"></i>team arrives by</span><span><i style="background:repeating-linear-gradient(-45deg,var(--vermilion) 0 2px,transparent 2px 5px)"></i>unsafe</span>
      <span><i style="border:1px dashed var(--ink)"></i>experimental timing</span></div></div>`;
    if (!times.length) { el.innerHTML = head + `<svg viewBox="0 0 1000 60" preserveAspectRatio="none" style="height:44px"><line class="axis" x1="0" x2="1000" y1="30" y2="30"/><text class="empty" x="0" y="20">No deadlines in the current plan.</text></svg>`; return; }
    const W = Math.max(600, el.clientWidth - 56), H = 118, top = 18, base = 78;
    // the axis runs from the earliest deadline (or now) to the latest; missed deadlines sit left of "now"
    const lo = Math.min(t0, ...times), hi = Math.max(t0 + 30 * 60e3, ...times);
    const pad = (hi - lo) * 0.04;
    const start = lo === t0 ? t0 : lo - pad, span = hi + pad - start, t1 = start + span;
    const X = (t) => Math.max(0, Math.min(W, ((t - start) / span) * W));
    const nx = X(t0);
    let s = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">`;
    if (nx > 2) s += `<rect x="0" y="${top}" width="${nx}" height="${base - top}" fill="var(--ink)" opacity=".07"><title>Past: deadlines already missed</title></rect>` + (nx > 150 ? `<text x="${nx - 6}" y="${base - 6}" text-anchor="end">missed</text>` : "");
    // grid: every 15/30/60 min depending on span
    const step = span > 6 * 3600e3 ? 3600e3 : span > 2 * 3600e3 ? 1800e3 : 900e3;
    for (let t = Math.ceil(start / step) * step; t < t1; t += step) {
      const x = X(t);
      s += `<line class="grid" x1="${x}" x2="${x}" y1="${top}" y2="${base}"/><text x="${x}" y="${base + 16}" text-anchor="middle">${SC.local(new Date(t).toISOString())}</text>`;
    }
    if (p && p.horizon_utc) {
      const hx = X(Date.parse(p.horizon_utc));
      const rightSide = hx < W - 170;
      s += `<rect class="unsafe" x="${hx}" y="${top}" width="${W - hx}" height="${base - top}"/><line class="unsafe-edge" x1="${hx}" x2="${hx}" y1="${top - 6}" y2="${base}"/>
        <text class="lab" x="${rightSide ? hx + 6 : hx - 6}" y="${top - 6}" text-anchor="${rightSide ? "start" : "end"}" style="fill:var(--vermilion-deep)">UNSAFE FROM ${SC.local(p.horizon_utc)}</text>`;
    }
    if (a && a.timing_window) {
      const x1 = X(Date.parse(a.timing_window.earliest_utc)), x2 = X(Date.parse(a.timing_window.latest_utc));
      s += `<rect class="window" x="${x1}" y="${top + 4}" width="${Math.max(2, x2 - x1)}" height="${base - top - 8}"><title>Experimental failure window: ${SC.esc(a.timing_window.basis || "")}</title></rect>`;
    }
    if (thr) {
      const x = X(thr);
      s += `<g><path class="rain" d="M${x} ${base - 26} c-5 7 -6 10 -6 13 a6 6 0 0 0 12 0 c0 -3 -1 -6 -6 -13z"><title>Rain threshold crossed ${SC.local(w.threshold_crossing_utc, true)} (not a landslide time)</title></path></g>`;
    }
    // households: leave-by ticks, staggered in three lanes so neighbours stay readable
    const rows = (d.rows || []).filter((r) => r.leave_by_utc).sort((x, y) => Date.parse(x.leave_by_utc) - Date.parse(y.leave_by_utc));
    const laneEnd = [-1e9, -1e9, -1e9, -1e9];   // label lanes: a label is drawn only where it fits
    rows.forEach((r) => {
      const x = X(Date.parse(r.leave_by_utc));
      let lane = laneEnd.findIndex((e) => x - e > 26);
      const labelled = lane >= 0;
      if (!labelled) lane = 3; else laneEnd[lane] = x;
      const y1 = top + 8 + lane * 10;
      const cls = "tick-leave" + (r.status === "evacuated" ? " done" : r.needs_help ? " help" : "");
      s += `<g class="hit"><title>${SC.esc(r.household_id)} leave by ${SC.local(r.leave_by_utc)}${r.needs_help ? " (needs help)" : ""}, ${SC.esc(r.status)}</title>
        <line class="${cls}" x1="${x}" x2="${x}" y1="${y1}" y2="${base}"/>
        ${labelled ? `<text x="${x}" y="${y1 - 3}" text-anchor="middle" style="fill:var(--ink);font-size:9.5px">${SC.esc(r.household_id)}</text>` : ""}</g>`;
      if (r.arrival_by_utc) {
        const ax = X(Date.parse(r.arrival_by_utc));
        s += `<circle class="tick-arrive" cx="${ax}" cy="${base - 5}" r="4"><title>${SC.esc(r.team_id || "team")} arrives at ${SC.esc(r.household_id)} by ${SC.local(r.arrival_by_utc)}</title></circle>`;
      }
    });
    s += `<line class="axis" x1="0" x2="${W}" y1="${base}" y2="${base}"/><line class="now" x1="${Math.max(1, nx)}" x2="${Math.max(1, nx)}" y1="${top}" y2="${base + 22}"/><text class="lab" x="${Math.max(1, nx) + 4}" y="${base + 30}">NOW</text></svg>`;
    el.innerHTML = head + s;
  }

  // ---------------------------------------------------------------- side panels
  function stakes(d) {
    document.getElementById("stake-count").textContent = d.nodes.length + (d.nodes.length === 1 ? " stake" : " stakes");
    document.getElementById("stakes").innerHTML = d.nodes.map((n) => {
      const tilt = n.tilt_deg, trend = n.tilt_trend_deg_per_h, wet = n.wetness_index;
      const hot = (trend || 0) >= HOT_TREND;
      const why = (n.reasons || []).filter((r) => !String(r).startsWith("info:"));
      const age = n.last_seen_age_s === null || n.last_seen_age_s === undefined ? "" : n.last_seen_age_s < 90 ? Math.round(n.last_seen_age_s) + " s ago" : Math.round(n.last_seen_age_s / 60) + " min ago";
      return `<div class="stake">
        <div class="id">${SC.esc(n.node_id)}<small>${n.simulated ? "simulated" : SC.esc(n.source || "")}</small></div>
        <div class="head"><span class="dot st-${SC.esc(n.status)}"></span><span class="sector">${SC.esc(n.sector)}</span><span class="muted small">${SC.esc(n.status)}${age ? " · " + age : ""}</span></div>
        <div class="meters">
          <div class="meter"><div class="k">tilt</div><div class="v">${SC.num(tilt, 2)}<small>°</small></div><div class="bar"><i style="width:${Math.min(100, ((tilt || 0) / 2) * 100)}%"></i></div></div>
          <div class="meter ${hot ? "hot" : ""}"><div class="k">trend</div><div class="v">${trend > 0 ? "↗" : trend < 0 ? "↘" : ""}${SC.num(trend)}<small>°/h</small></div><div class="bar"><i style="width:${Math.min(100, (Math.abs(trend || 0) / 2) * 100)}%"></i></div></div>
          <div class="meter wet"><div class="k">wetness</div><div class="v">${SC.num(wet, 2)}</div><div class="bar"><i style="width:${Math.min(100, (wet || 0) * 100)}%"></i></div></div>
        </div>
        ${why.length ? `<div class="why">${SC.esc(why.join("; "))}</div>` : ""}
      </div>`;
    }).join("") || `<span class="muted">No stakes configured.</span>`;
  }
  function weather(d) {
    const w = d.weather, el = document.getElementById("weather");
    if (!w) { el.innerHTML = `<span class="muted">No weather yet.</span>`; return; }
    const hrs = (w.hours || []).slice(-48), max = Math.max(1, ...hrs.map((h) => h.mm || 0));
    const nowIdx = hrs.findIndex((h) => h.kind === "forecast");
    const W = 480, H = 86, bw = W / Math.max(1, hrs.length);
    let bars = "";
    hrs.forEach((h, i) => {
      const x = i * bw, title = `${SC.local(h.time_utc, true)}: ${h.mm === null || h.mm === undefined ? "missing" : h.mm + " mm"}`;
      if (h.mm === null || h.mm === undefined) { bars += `<rect class="miss" x="${x + 0.5}" y="${H - 18}" width="${bw - 1}" height="2"><title>${SC.esc(title)}</title></rect>`; return; }
      const bh = Math.max(1, ((h.mm || 0) / max) * (H - 22));
      bars += `<rect class="${h.kind === "forecast" ? "fc" : "past"}" x="${x + 0.5}" y="${H - 16 - bh}" width="${Math.max(1, bw - 1.2)}" height="${bh}"><title>${SC.esc(title)}</title></rect>`;
    });
    if (nowIdx >= 0) bars += `<line class="now" x1="${nowIdx * bw}" x2="${nowIdx * bw}" y1="0" y2="${H - 14}"/><text x="${nowIdx * bw + 3}" y="9">now</text>`;
    if (w.threshold_crossing_utc) {
      const ti = hrs.findIndex((h) => Date.parse(h.time_utc) >= Date.parse(w.threshold_crossing_utc));
      if (ti >= 0) bars += `<line class="thr" x1="${ti * bw}" x2="${ti * bw}" y1="0" y2="${H - 14}"/>`;
    }
    if (hrs.length) bars += `<text x="0" y="${H - 2}">${SC.local(hrs[0].time_utc)}</text><text x="${W}" y="${H - 2}" text-anchor="end">${SC.local(hrs[hrs.length - 1].time_utc)}</text>`;
    el.innerHTML = `<div class="rainnums">
        <div><div class="v">${SC.num(w.rain_24h_mm, 0)}<small>mm</small></div><div class="k">past 24 h</div></div>
        <div class="fc"><div class="v">${SC.num(w.forecast_24h_mm, 0)}<small>mm</small></div><div class="k">next 24 h</div></div>
        <div><div class="v">${SC.num(w.peak_mm_per_h, 0)}<small>mm/h</small></div><div class="k">peak</div></div></div>
      <svg class="rainsvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-label="hourly rainfall, past solid, forecast hatched">${bars}</svg>
      <div class="threshold ${w.threshold_crossing_utc ? "crossed" : ""}">${w.threshold_crossing_utc ? "Rain threshold crossed " + SC.local(w.threshold_crossing_utc, true) + " <span class='muted'>(not a landslide time)</span>" : "Rain threshold not crossed in forecast"}</div>
      <div class="wsource">${SC.esc(w.source || "")}${w.stale ? ' <span class="tag stale">stale</span>' : ""}${w.error ? " · " + SC.esc(w.error) : ""}</div>`;
  }
  function assess(d) {
    const a = d.assessment, el = document.getElementById("assess");
    if (!a) { el.innerHTML = `<span class="muted">No assessment yet.</span>`; return; }
    const named = d.zones.filter((z) => z.likelihood && z.likelihood !== "low");
    const claude = a.engine.startsWith("claude"), fb = a.engine === "rules-fallback";
    el.innerHTML = `<div class="engine-line ${fb ? "fallback" : ""}"><span class="seal">${claude ? "C" : "R"}</span>
        <span>${claude ? "Claude judged the paths · rules checked it" : fb ? "Claude unavailable · rule-based fallback" : "Rule-based engine"}</span></div>
      <div class="kv"><span>paths</span><span>${named.length ? named.map((z) => `<span class="lk lk-${z.likelihood}">${SC.esc(z.zone_id)} ${z.likelihood}</span>`).join(" ") : "none at risk"}</span>
      <span>timing</span><span>${a.timing_status === "experimental" && a.timing_window ? `<span class="tag exp">experimental</span> ${SC.local(a.timing_window.earliest_utc)} to ${SC.local(a.timing_window.latest_utc)}<br><span class="muted small">${SC.esc(a.timing_window.basis)}</span>` : "unavailable <span class='muted small'>(not enough evidence)</span>"}</span>
      <span>gaps</span><span>${a.coverage_gaps.length ? `<ul class="tight">${a.coverage_gaps.map((g) => `<li>${SC.esc(g)}</li>`).join("")}</ul>` : "none"}</span></div>
      ${named.map((z) => `<div class="zone-why"><b>${SC.esc(z.zone_id)}</b>${SC.esc(z.name)}: <span class="muted">${SC.esc((z.reasons || []).join("; "))}</span></div>`).join("")}
      ${a.notes.length ? `<div class="checks">Checks: ${SC.esc(a.notes.join("; "))}</div>` : ""}`;
  }

  // ---------------------------------------------------------------- plan bar + operator controls
  function planbar(d) {
    const p = d.plan, ap = d.approved_plan, el = document.getElementById("planbar");
    if (!p) { el.innerHTML = `<span class="muted">No plan yet.</span>`; return; }
    const op = d.role === "operator", c = p.counts;
    const basisText = { "hazard-timing": "from the experimental timing fit", operator: "set by the operator", "policy-default": "policy default for this risk level (demo setting)", none: "none: no deadlines" }[p.horizon_basis] || p.horizon_basis;
    const n = (k, label, bad) => `<div class="${bad ? "bad" : ""}"><b>${c[k] || 0}</b><span>${label}</span></div>`;
    el.innerHTML = `
      <div class="planid"><span class="pid">${SC.esc(p.plan_id)}</span><span class="ver">v${p.version}</span><span class="chip state-${SC.esc(p.state)}">${SC.esc(p.state)}</span></div>
      <div class="unsafe-from"><span class="muted small">Unsafe from</span><b>${p.horizon_utc ? SC.local(p.horizon_utc) : "-"}</b><span class="muted small">${SC.esc(basisText)}</span></div>
      <div class="tally">${n("exposed", "exposed")}${n("need_help", "need help")}${n("team_assigned", "team assigned")}${n("accepted", "accepted")}${n("acknowledged", "seen")}${n("evacuated", "evacuated")}${c.infeasible ? n("infeasible", "infeasible", true) : ""}</div>
      <span class="spacer"></span>
      <div>
        <div class="approved-by">${ap ? `approved: <b>${SC.esc(ap.plan_id)}</b> v${ap.version || ""} by ${SC.esc(ap.approved_by)}` : "no plan approved yet"}</div>
        ${op ? `<div class="controls" style="margin-top:8px">
          <span class="hz"><label for="hz">Horizon</label><input id="hz" type="number" min="5" step="5" placeholder="min"><button id="hz-set">Set</button><button id="hz-clear">Clear</button></span>
          <button id="reassess">Reassess now</button>
          <a class="btn" href="/api/plans/${encodeURIComponent(p.plan_id)}/export.csv?token=${encodeURIComponent(SC.token())}">CSV ↓</a>
          <button id="approve" class="primary" ${p.state === "approved" ? "disabled" : ""}>${p.state === "approved" ? "✓ Approved" : "Approve plan v" + p.version}</button>
        </div>` : ""}
      </div>
      ${p.state === "approved" ? `<div class="stamp" aria-hidden="true"><div><span class="zh">核准</span>APPROVED<br>v${p.version}</div></div>` : ""}`;
    if (op) {
      document.getElementById("approve").onclick = () => act(SC.post(`/api/plans/${p.plan_id}/approve`, { version: p.version }), `Plan v${p.version} approved. Messages queued.`);
      document.getElementById("reassess").onclick = () => act(SC.post("/api/reassess"), "Reassessment requested.");
      document.getElementById("hz-set").onclick = () => {
        const v = Number(document.getElementById("hz").value);
        if (!v) { SC.toast("Enter the horizon in minutes (5 min to 72 h)."); return; }
        act(SC.post("/api/horizon", { minutes: v }), `Horizon set to ${v} min; replanning.`);
      };
      document.getElementById("hz-clear").onclick = () => act(SC.post("/api/horizon", { minutes: null }), "Operator horizon cleared.");
      document.getElementById("hz").onkeydown = (e) => { if (e.key === "Enter") document.getElementById("hz-set").click(); };
    }
  }

  // ---------------------------------------------------------------- household table
  function table(d) {
    const rows = d.rows.slice().sort((a, b) => String(a[sortKey] || "~").localeCompare(String(b[sortKey] || "~")));
    const det = d.role === "operator" || d.role === "committee";
    const el = document.getElementById("rows");
    if (!rows.length) { el.innerHTML = `<tbody><tr><td class="muted">No exposed households in the current plan.</td></tr></tbody>`; return; }
    const cols = [["household_id", "Household"], ["likelihood", "Zone"], ["needs_help", "Needs help"], ["prep_min", "Prep / travel"],
      ["prepare_by_utc", "Start packing"], ["leave_by_utc", "Leave by"], ["team_id", "Team / arrive by"], ["destination_id", "Go to"], ["status", "Status"], ["feasible", "Problems"]];
    el.innerHTML = `<thead><tr>${cols.map(([k, l]) => `<th data-k="${k}" class="${sortKey === k ? "sorted" : ""}">${l}${sortKey === k ? " ↓" : ""}</th>`).join("")}</tr></thead><tbody>` +
      rows.map((r) => `<tr class="${r.feasible === false ? "infeasible" : ""}">
        <td><span class="hid">${SC.esc(r.household_id)}</span>${det && r.name ? "<br><span class='small'>" + SC.esc(r.name) + "</span>" : ""}<br><span class="small muted">${r.people_count || "?"} people</span></td>
        <td>${SC.esc(r.zone_id || "-")} <span class="lk lk-${SC.esc(r.likelihood)}">${SC.esc(r.likelihood)}</span></td>
        <td>${r.needs_help ? "<b>yes</b>" : "no"}${det && r.assistance && r.assistance.length ? "<br><span class='small'>" + SC.esc(r.assistance.join(", ")) + "</span>" : ""}</td>
        <td class="mono">${SC.num(r.prep_min, 0)} / ${SC.num(r.travel_min, 0)} min</td>
        <td><span class="t">${SC.local(r.prepare_by_utc)}</span></td>
        <td><span class="t leave">${SC.local(r.leave_by_utc)}</span>${r.leave_by_utc ? `<br><span class="small muted mono" data-until="${SC.esc(r.leave_by_utc)}">${SC.untilText(r.leave_by_utc)}</span>` : ""}</td>
        <td>${r.team_id ? SC.esc(r.team_id) + (r.assignment_state ? " <span class='astate " + SC.esc(r.assignment_state) + "'>" + SC.esc(r.assignment_state) + "</span>" : "") + "<br><span class='mono small'>by " + SC.local(r.arrival_by_utc) + "</span>" : "-"}</td>
        <td>${SC.esc(r.destination_name || r.destination_id || "-")}</td>
        <td><span class="status ${r.status === "evacuated" ? "evacuated" : ""}">${SC.esc(r.status)}</span></td>
        <td class="problems">${SC.esc((r.problems || []).join("; "))}</td></tr>`).join("") + "</tbody>";
    el.querySelectorAll("th").forEach((th) => (th.onclick = () => { sortKey = th.dataset.k; SC.changed("table", null); table(state); }));
  }
  function logs(d) {
    document.getElementById("events").innerHTML = d.events.map((e) => `<div><time>${SC.local(e.time_utc)}</time><span>${SC.esc(e.msg)}</span></div>`).join("") || "<div class='plain muted'>-</div>";
    document.getElementById("outbox").innerHTML = d.role !== "operator" ? "<div class='plain muted'>Sign in as operator to see messages.</div>"
      : (d.outbox.slice().reverse().map((m) => `<div><time>${SC.esc(m.household_id)}</time><span><b>[${SC.esc(m.status)}]</b> ${SC.esc(m.text)}</span></div>`).join("") || "<div class='plain muted'>No messages yet. Approving a plan sends them.</div>");
  }
  async function act(promise, okMsg) {
    try { await promise; if (okMsg) SC.toast(okMsg, "ok"); await refresh(); } catch (e) { SC.toast(e.message); }
  }

  async function refresh() {
    const d = await SC.get("/api/dashboard");
    state = d;
    SC.clock(d.site.name); SC.modeBadge(d);
    if (!map && window.L) setupMap(d);
    if (SC.changed("map", [d.zones, d.buildings, d.nodes.map((n) => [n.node_id, n.status, n.tilt_trend_deg_per_h >= HOT_TREND]), d.shelters])) { if (map) drawLeaflet(d); else drawSvg(d); }
    if (SC.changed("banner", [d.assessment, d.countdown])) banner(d);
    ruler(d);
    stakes(d);
    if (SC.changed("weather", d.weather)) weather(d);
    if (SC.changed("assess", [d.assessment, d.zones.map((z) => [z.zone_id, z.likelihood, z.reasons])])) assess(d);
    if ((!document.activeElement || document.activeElement.id !== "hz") && SC.changed("plan", [d.plan, d.approved_plan, d.role])) planbar(d);
    if (SC.changed("table", [d.rows, d.role])) table(d);
    if (SC.changed("logs", [d.events, d.outbox, d.role])) logs(d);
  }
  SC.poll(refresh, 2000);
  window.addEventListener("resize", () => { if (state) ruler(state); });
})();
