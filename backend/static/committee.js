// Rescue committee: households needing help, who is going, and whether they can make it.
// Accept jobs, record arrivals and evacuations. Reads /api/committee/queue.
"use strict";
(() => {
  const LEVEL_TEXT = { monitoring: "Monitoring", elevated: "Elevated concern", urgent_review: "Urgent review" };
  const LEVEL_ZH = { monitoring: "監測中", elevated: "警戒提高", urgent_review: "緊急評估" };
  const app = document.getElementById("app");
  let built = false;

  function shell() {
    app.innerHTML = `
      <section id="cbanner" class="banner rise" style="--i:0"></section>
      <div class="grid">
        <div class="card rise" style="--i:1;padding:0">
          <div style="padding:18px 22px 6px"><h2>Assistance queue <span class="num" id="qcount"></span><span class="aside">earliest team deadline first</span></h2></div>
          <div id="queue" class="tickets"></div>
        </div>
        <div class="stack">
          <div class="card rise" style="--i:2"><h2>Teams</h2><div id="teams"></div></div>
          <div class="card rise" style="--i:3"><h2>Not yet acknowledged <span class="aside">high likelihood</span></h2><div id="unack" class="unack"></div></div>
          <div class="card rise" style="--i:4"><h2>Recent events</h2><div id="cevents" class="log"></div></div>
        </div>
      </div>`;
    // one listener for every ticket button
    document.getElementById("queue").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      b.disabled = true;
      if (b.dataset.act === "accept") window.scAccept(b.dataset.id);
      else window.scProgress(b.dataset.act, b.dataset.hid, b.dataset.team);
    });
    built = true;
  }

  async function act(p, okMsg) { try { await p; if (okMsg) SC.toast(okMsg, "ok"); SC.changed("queue", null); await refresh(); } catch (e) { SC.toast(e.message); SC.changed("queue", null); await refresh(); } }
  window.scAccept = (id) => act(SC.post(`/api/assignments/${encodeURIComponent(id)}/accept`), "Job accepted.");
  window.scProgress = (kind, hid, team) => act(SC.post("/api/progress", { kind, household_id: hid, team_id: team || null, event_id: `${kind}-${hid}-${Date.now()}` }),
    kind === "team_arrived" ? `Team arrival recorded for ${hid}.` : `${hid} marked as evacuated.`);

  function bannerHtml(d) {
    const a = d.assessment, p = d.plan;
    return `<div><div class="eyebrow">Rescue committee · hazard level</div>
        <div class="level">${a ? SC.esc(LEVEL_TEXT[a.risk_level] || a.risk_level) : "No assessment"}</div>
        <div class="level-zh">${a ? SC.esc(LEVEL_ZH[a.risk_level] || "") : ""}</div></div>
      <div class="summary">${a ? SC.esc(a.summary) : ""}</div>
      <div class="planstate">${p ? `<div class="pid">${SC.esc(p.plan_id)} <span style="font-size:18px">v${p.version}</span></div><div><span class="chip state-${SC.esc(p.state)}">${SC.esc(p.state)}</span></div>` : `<div class="pid">No plan</div>`}
        <div style="margin-top:8px">${d.approved_plan ? "approved plan: <b>" + SC.esc(d.approved_plan.plan_id) + "</b>" : "not yet approved by the operator"}</div></div>`;
  }

  function ticket(r) {
    const secs = SC.secondsUntil(r.arrival_by_utc);
    const pct = secs === null ? 0 : Math.max(4, Math.min(100, 100 - (secs / 7200) * 100));   // bar fills over the last two hours
    const btns = [];
    if (r.assignment_id && r.assignment_state === "proposed") btns.push(`<button class="primary" data-act="accept" data-id="${SC.esc(r.assignment_id)}">Accept job</button>`);
    if (r.assignment_state === "accepted") btns.push(`<button class="ink" data-act="team_arrived" data-hid="${SC.esc(r.household_id)}" data-team="${SC.esc(r.team_id || "")}">Arrived</button>`);
    if (r.status !== "evacuated") btns.push(`<button data-act="evacuated" data-hid="${SC.esc(r.household_id)}" data-team="${SC.esc(r.team_id || "")}">Evacuated</button>`);
    return `<div class="ticket ${r.feasible === false ? "infeasible" : ""}">
      <div class="when"><div class="k">team arrive by</div><div class="t">${SC.local(r.arrival_by_utc)}</div>
        <div class="u" ${r.arrival_by_utc ? `data-until="${SC.esc(r.arrival_by_utc)}"` : ""}>${r.arrival_by_utc ? SC.untilText(r.arrival_by_utc) : "no team time"}</div>
        ${r.arrival_by_utc ? `<div class="urg"><i style="width:${pct}%"></i></div>` : ""}</div>
      <div class="who"><div class="name">${SC.esc(r.household_id)}<small>${SC.esc(r.name || "")} · ${r.people_count || "?"} people</small></div>
        <div class="meta"><span class="lk lk-${SC.esc(r.likelihood)}">${SC.esc(r.zone_id)} ${SC.esc(r.likelihood)}</span></div>
        <div class="needs">${(r.assistance || []).map((x) => `<span class="need">${SC.esc(x)}</span>`).join("") || '<span class="muted small">no stated assistance</span>'}</div>
        ${(r.checklist || []).length ? `<div class="bring">Bring: ${SC.esc(r.checklist.join(", "))}</div>` : ""}</div>
      <div class="plan">
        <div class="row"><span>team</span><span>${SC.esc(r.team_name || "none available")}${r.assignment_state ? ` <span class="astate ${SC.esc(r.assignment_state)}">${SC.esc(r.assignment_state)}</span>` : ""}</span></div>
        <div class="row"><span>leave by</span><span class="mono"><b>${SC.local(r.leave_by_utc)}</b></span></div>
        <div class="row"><span>go to</span><span>${SC.esc(r.destination_name || "-")}</span></div>
        <div class="row"><span>status</span><span class="status ${r.status === "evacuated" ? "evacuated" : ""}">${SC.esc(r.status)}</span></div>
        ${r.feasible === false ? `<div class="problems">${SC.esc((r.problems || []).slice(-1)[0] || "infeasible")}</div>` : ""}</div>
      <div class="acts">${btns.join("")}</div></div>`;
  }

  function teamHtml(t) {
    const seats = Math.max(0, (t.seats || 0) - (t.wheelchair_spaces || 0));
    return `<div class="team"><div class="tid">${SC.esc(t.team_id)}</div><div>
      <div class="tname">${SC.esc(t.name)}</div>
      <div class="seats">${"<i></i>".repeat(seats)}${'<i class="wc"></i>'.repeat(t.wheelchair_spaces || 0)}<span>${SC.esc(t.vehicle)}, ${t.seats} seats, ${t.wheelchair_spaces} wheelchair</span></div>
      <div class="jobs">active: ${SC.esc(t.active_jobs.join(", ") || "none")}</div></div></div>`;
  }

  async function refresh() {
    let d;
    try { d = await SC.get("/api/committee/queue"); }
    catch (e) {
      built = false;
      app.innerHTML = `<div class="gate"><h1>Committee<br>sign-in needed</h1><p>${SC.esc(e.message)}. Open this view with <code>?view=committee&amp;token=committee-demo</code>.</p>
        <p><a class="btn" href="${SC.link("committee", "committee-demo")}">Open as committee (demo)</a></p></div>`;
      return;
    }
    if (!built) shell();
    SC.clock(); SC.modeBadge(d);
    const b = document.getElementById("cbanner");
    if (SC.changed("cbanner", [d.assessment, d.plan, d.approved_plan])) {
      b.className = "banner rise" + (d.assessment ? " lv-" + d.assessment.risk_level : "");
      b.innerHTML = bannerHtml(d);
    }
    document.getElementById("qcount").textContent = d.queue.length || "";
    if (SC.changed("queue", d.queue)) {
      document.getElementById("queue").innerHTML = d.queue.length ? d.queue.map(ticket).join("")
        : `<div class="empty-state"><b>All clear</b>Nobody currently needs assistance.</div>`;
    }
    if (SC.changed("teams", d.teams)) document.getElementById("teams").innerHTML = d.teams.map(teamHtml).join("") || `<span class="muted">No teams.</span>`;
    if (SC.changed("unack", d.unacknowledged_high)) document.getElementById("unack").innerHTML = d.unacknowledged_high.length
      ? d.unacknowledged_high.map((r) => `<div><b>${SC.esc(r.household_id)}</b>${SC.esc(r.name || "")}: message sent, no reply. Consider a door knock.</div>`).join("")
      : `<span class="muted">None.</span>`;
    if (SC.changed("cevents", d.events)) document.getElementById("cevents").innerHTML = d.events.map((e) => `<div><time>${SC.local(e.time_utc)}</time><span>${SC.esc(e.msg)}</span></div>`).join("") || "<div class='plain muted'>-</div>";
  }
  SC.poll(refresh, 2000);
})();
