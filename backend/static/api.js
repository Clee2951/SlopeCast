// Shared browser helpers: the only code that talks to the backend.
// Adds the demo token, polls, reports outages, formats times in the site's local time,
// ticks every [data-until] countdown once a second, and shows in-page notices.
"use strict";
const SC = (() => {
  const qs = new URLSearchParams(location.search);
  let offset = 8;               // site UTC offset in hours; updated from every response
  let serverSkewMs = 0;         // server clock minus browser clock (replays run on a simulated clock)
  let siteName = "";
  const seen = {};              // last rendered signature per key, so unchanged data is not redrawn

  function param(name) { return qs.get(name); }
  function token() { return qs.get("token") || ""; }

  async function request(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    if (token()) headers["Authorization"] = "Bearer " + token();
    let res;
    try {
      res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
    } catch (e) {
      document.getElementById("offline-banner").classList.remove("hidden");
      throw e;
    }
    document.getElementById("offline-banner").classList.add("hidden");
    const data = (res.headers.get("Content-Type") || "").includes("json") ? await res.json() : await res.text();
    if (!res.ok) { const err = new Error((data && data.error) || res.statusText); err.status = res.status; throw err; }
    if (data && typeof data === "object") {
      if (data.utc_offset_hours !== undefined) offset = data.utc_offset_hours;
      if (data.server_time_utc) serverSkewMs = Date.parse(data.server_time_utc) - Date.now();
    }
    return data;
  }
  const get = (p) => request("GET", p);
  const post = (p, b) => request("POST", p, b || {});

  function poll(fn, ms) {
    let busy = false;
    const run = async () => { if (busy) return; busy = true; try { await fn(); } catch (e) { console.warn(e); } busy = false; };
    run();
    return setInterval(run, ms);
  }

  function now() { return new Date(Date.now() + serverSkewMs); }
  function local(iso, withDate) {
    if (!iso) return "-";
    const d = new Date(Date.parse(iso) + offset * 3600e3);
    const hh = String(d.getUTCHours()).padStart(2, "0"), mm = String(d.getUTCMinutes()).padStart(2, "0");
    return withDate ? `${d.getUTCMonth() + 1}/${d.getUTCDate()} ${hh}:${mm}` : `${hh}:${mm}`;
  }
  function secondsUntil(iso) { return iso ? Math.round((Date.parse(iso) - now().getTime()) / 1000) : null; }
  function untilText(iso) {
    if (!iso) return "-";
    const s = secondsUntil(iso);
    const a = Math.abs(s), h = Math.floor(a / 3600), m = Math.floor((a % 3600) / 60), sec = a % 60;
    const t = h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m}:${String(sec).padStart(2, "0")}`;
    return s >= 0 ? `in ${t}` : `${t} ago`;
  }
  // big-clock form: "1:24:07" or "24:07"; negative values are shown as overdue by the caller
  function clockText(iso) {
    const s = secondsUntil(iso);
    if (s === null) return "--:--";
    const a = Math.abs(s), h = Math.floor(a / 3600), m = Math.floor((a % 3600) / 60), sec = a % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(sec).padStart(2, "0");
  }
  function esc(v) {
    return String(v === null || v === undefined ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function num(v, d) { return v === null || v === undefined ? "-" : Number(v).toFixed(d === undefined ? 1 : d); }
  function link(view, tok) { return `/?view=${view}${tok ? "&token=" + encodeURIComponent(tok) : ""}`; }

  // true when `value` differs from what was last rendered under `key`
  function changed(key, value) {
    const s = JSON.stringify(value);
    if (seen[key] === s) return false;
    seen[key] = s; return true;
  }

  function renderNav(view) {
    const t = token();
    const nav = document.getElementById("view-links");
    const items = [["dashboard", "Dashboard"], ["committee", "Committee"], ["resident", "Resident"]];
    nav.innerHTML = items.map(([v, label]) => {
      const tok = v === "committee" ? (t.startsWith("operator") ? "committee-demo" : t)
        : v === "resident" ? (t.startsWith("resident-") ? t : "resident-h02") : t;
      return `<a class="${v === view ? "active" : ""}" href="${link(v, tok)}">${label}</a>`;
    }).join("");
  }
  function clock(name) {
    if (name) siteName = name;
    const el = document.getElementById("site-clock");
    if (el) el.innerHTML = `<b>${local(now().toISOString())}</b> site time`;
    const sn = document.getElementById("site-name");
    if (sn && siteName && sn.textContent !== siteName) sn.textContent = siteName;
  }
  function modeBadge(d) {
    const el = document.getElementById("mode-badge");
    if (!el || !d) return;
    if (d.mode === "replay") {
      const r = d.replay || {};
      const pct = r.done ? 100 : Math.min(100, (100 * (r.minute || 0)) / (r.duration_min || 1));
      el.className = "badge replay";
      el.style.setProperty("--p", pct + "%");
      el.textContent = `REPLAY ${r.done ? "finished" : "min " + Math.round(r.minute || 0) + "/" + (r.duration_min || "?")} at ${r.speed || "?"}x`;
      el.title = (r.name || "") + ": " + (r.description || "");
    } else {
      el.className = "badge live"; el.textContent = "LIVE"; el.title = "Live stakes over USB";
    }
  }

  // in-page notice instead of alert(); kind: "error" | "ok"
  function toast(msg, kind) {
    const host = document.getElementById("toasts");
    if (!host) return;
    const div = document.createElement("div");
    div.className = "toast " + (kind || "error");
    div.setAttribute("role", kind === "ok" ? "status" : "alert");
    div.textContent = msg;
    host.appendChild(div);
    setTimeout(() => { div.classList.add("out"); setTimeout(() => div.remove(), 400); }, 5200);
  }

  // one ticker for every live countdown and the clock
  function tick() {
    document.querySelectorAll("[data-until]").forEach((el) => { el.textContent = untilText(el.dataset.until); });
    document.querySelectorAll("[data-clock]").forEach((el) => {
      const s = secondsUntil(el.dataset.clock);
      el.textContent = (s !== null && s < 0 ? "−" : "") + clockText(el.dataset.clock);
      el.classList.toggle("overdue", s !== null && s < 0);
    });
    clock();
  }
  setInterval(tick, 1000);

  return { param, token, get, post, poll, local, untilText, clockText, secondsUntil, esc, num, link, renderNav, clock, modeBadge, now, changed, toast, tick };
})();
