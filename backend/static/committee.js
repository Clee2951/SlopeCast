// Rescue committee: households needing help, who is going, and whether they can make it.
// Accept jobs, record arrivals and evacuations. Reads /api/committee/queue.
"use strict";
(() => {
  const app = document.getElementById("app");
  app.innerHTML = `
    <section id="cbanner" class="banner" style="background:#555"></section>
    <div class="grid">
      <div class="card"><h2>Assistance queue (earliest deadline first)</h2><div class="tablewrap" style="padding:0"><table id="queue"></table></div></div>
      <div class="stack">
        <div class="card"><h2>Teams</h2><div id="teams"></div></div>
        <div class="card"><h2>Not yet acknowledged (high likelihood)</h2><div id="unack"></div></div>
        <div class="card"><h2>Recent events</h2><div id="cevents" class="log"></div></div>
      </div>
    </div>`;

  async function act(p) { try { await p; await refresh(); } catch (e) { alert(e.message); } }
  window.scAccept = (id) => act(SC.post(`/api/assignments/${encodeURIComponent(id)}/accept`));
  window.scProgress = (kind, hid, team) => act(SC.post("/api/progress", { kind, household_id: hid, team_id: team || null, event_id: `${kind}-${hid}-${Date.now()}` }));

  async function refresh() {
    let d;
    try { d = await SC.get("/api/committee/queue"); }
    catch (e) { app.innerHTML = `<p class="pad">${SC.esc(e.message)}. Open this view with <code>?view=committee&token=committee-demo</code>.</p>`; return; }
    SC.clock(); SC.modeBadge(d);
    const a = d.assessment, p = d.plan;
    const b = document.getElementById("cbanner");
    b.style.background = a ? getComputedStyle(document.documentElement).getPropertyValue("--" + a.risk_level) : "#555";
    b.innerHTML = `<div class="level">${a ? SC.esc(a.risk_level.replace("_", " ")) : "no assessment"}</div>
      <div class="summary">${a ? SC.esc(a.summary) : ""}</div>
      <div class="countdown"><small>${p ? SC.esc(p.plan_id) + " v" + p.version + " (" + SC.esc(p.state) + ")" : "no plan"}</small><br>
      <small>${d.approved_plan ? "approved plan: " + SC.esc(d.approved_plan.plan_id) : "not yet approved by the operator"}</small></div>`;
    const q = document.getElementById("queue");
    q.innerHTML = d.queue.length ? `<tr><th>Household</th><th>Needs</th><th>Team</th><th>Arrive by</th><th>Leave by</th><th>Go to</th><th>Status</th><th></th></tr>` +
      d.queue.map((r) => `<tr class="${r.feasible === false ? "infeasible" : ""}">
        <td><b>${SC.esc(r.household_id)}</b><br><span class="small muted">${SC.esc(r.name || "")}, ${r.people_count || "?"} people</span><br><span class="lk lk-${SC.esc(r.likelihood)}">${SC.esc(r.zone_id)} ${SC.esc(r.likelihood)}</span></td>
        <td class="small">${SC.esc((r.assistance || []).join(", ") || "-")}<br><span class="muted">${SC.esc((r.checklist || []).join(", "))}</span></td>
        <td>${SC.esc(r.team_name || "none available")}<br><span class="small muted">${SC.esc(r.assignment_state || "")}</span></td>
        <td class="mono"><b>${SC.local(r.arrival_by_utc)}</b><br><span class="small muted">${r.arrival_by_utc ? SC.untilText(r.arrival_by_utc) : ""}</span></td>
        <td class="mono">${SC.local(r.leave_by_utc)}</td>
        <td class="small">${SC.esc(r.destination_name || "-")}</td>
        <td class="small">${SC.esc(r.status)}${r.feasible === false ? `<div class="problems">${SC.esc((r.problems || []).slice(-1)[0] || "infeasible")}</div>` : ""}</td>
        <td>${r.assignment_id && r.assignment_state === "proposed" ? `<button class="primary" onclick="scAccept('${SC.esc(r.assignment_id)}')">Accept</button>` : ""}
            ${r.assignment_state === "accepted" ? `<button onclick="scProgress('team_arrived','${SC.esc(r.household_id)}','${SC.esc(r.team_id || "")}')">Arrived</button>` : ""}
            ${r.status !== "evacuated" ? `<button onclick="scProgress('evacuated','${SC.esc(r.household_id)}','${SC.esc(r.team_id || "")}')">Evacuated</button>` : ""}</td></tr>`).join("")
      : `<tr><td class="muted">Nobody currently needs assistance.</td></tr>`;
    document.getElementById("teams").innerHTML = d.teams.map((t) => `<div style="margin-bottom:6px"><b>${SC.esc(t.team_id)}</b> ${SC.esc(t.name)}<br>
      <span class="small muted">${SC.esc(t.vehicle)}, ${t.seats} seats, ${t.wheelchair_spaces} wheelchair; active: ${SC.esc(t.active_jobs.join(", ") || "none")}</span></div>`).join("");
    document.getElementById("unack").innerHTML = d.unacknowledged_high.length
      ? d.unacknowledged_high.map((r) => `<div>${SC.esc(r.household_id)} ${SC.esc(r.name || "")}: message sent, no reply. Consider a door knock.</div>`).join("")
      : `<span class="muted">None.</span>`;
    document.getElementById("cevents").innerHTML = d.events.map((e) => `<div>${SC.local(e.time_utc)} ${SC.esc(e.msg)}</div>`).join("");
  }
  SC.poll(refresh, 2000);
})();
