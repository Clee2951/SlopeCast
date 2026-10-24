// Resident view: one household's instruction in large type. Reads /api/me/plan.
// Shows the approved plan only. "I've seen this" and "We have left" are separate events.
"use strict";
(() => {
  const app = document.getElementById("app");
  const T = {
    en: { noPlan: "No evacuation instruction has been issued.", noPlanSub: "The committee has not approved a plan yet. Keep your phone on and follow official announcements.",
      none: "No action needed right now.", noneSub: "You are not in an area the committee has asked to evacuate. Keep your phone on.",
      leave: "Leave by", go: "Go to", bring: "Bring", help: "Help", seen: "I've seen this", left: "We have left",
      seenDone: "Thanks, the committee knows you've seen this.", leftDone: "Marked as evacuated. Stay at the shelter until told it is safe.",
      noDeadline: "Prepare to leave", requested: "Help requested; a team has not confirmed yet.", assigned: (t, at) => `${t} has accepted and plans to arrive by ${at}.` },
    "zh-Hant": { noPlan: "目前尚未發布撤離指示。", noPlanSub: "委員會尚未核准計畫。請保持手機開機並留意官方公告。",
      none: "目前不需要撤離。", noneSub: "您不在委員會要求撤離的區域。請保持手機開機。",
      leave: "請在此時間前離開", go: "前往", bring: "請攜帶", help: "協助", seen: "我已看到", left: "我們已離開",
      seenDone: "謝謝，委員會已知道您看到通知。", leftDone: "已標記為撤離。請留在避難所直到通知安全。",
      noDeadline: "請準備離開", requested: "已申請協助，救援隊尚未確認。", assigned: (t, at) => `${t} 已確認，預計 ${at} 前抵達。` },
  };
  let lang = null;

  async function send(kind) {
    try { await SC.post("/api/progress", { kind, event_id: `${kind}-${SC.token()}-${Date.now()}` }); await refresh(); }
    catch (e) { alert(e.message); }
  }
  window.scSeen = () => send("acknowledged");
  window.scLeft = () => send("evacuated");
  window.scLang = (l) => { lang = l; refresh(); };

  async function refresh() {
    let d;
    try { d = await SC.get("/api/me/plan"); }
    catch (e) { app.innerHTML = `<p class="pad">${SC.esc(e.message)}. Open with <code>?view=resident&token=resident-h02</code> (tokens are in data/demo/users.json).</p>`; return; }
    SC.clock(); SC.modeBadge(d);
    const t = T[lang || d.language] || T.en;
    const prog = d.progress || [];
    let body;
    if (!d.plan_id) {
      body = `<div class="big">${t.noPlan}</div><p class="muted">${t.noPlanSub}</p>`;
    } else if (!d.action_needed) {
      body = `<div class="big">${t.none}</div><p class="muted">${t.noneSub}</p>`;
    } else {
      const help = d.help ? (d.help.state === "accepted" || d.help.state === "arrived" ? t.assigned(SC.esc(d.help.team), SC.local(d.help.arrival_by_utc)) : t.requested) : "";
      body = `<div class="muted">${SC.esc(d.zone || "")}</div>
        ${d.leave_by_utc ? `<div>${t.leave}</div><div class="leave">${SC.local(d.leave_by_utc)}</div><div class="muted">${SC.untilText(d.leave_by_utc)}</div>` : `<div class="leave" style="font-size:30px">${t.noDeadline}</div>`}
        <p class="big">${t.go}: <b>${SC.esc(d.destination ? d.destination.name : "-")}</b></p>
        <p class="big">${t.bring}:</p><ul>${d.checklist.map((c) => `<li>${SC.esc(c)}</li>`).join("")}</ul>
        ${help ? `<p class="big">${t.help}: ${help}</p>` : ""}
        <div class="actions">
          <button class="primary" onclick="scSeen()" ${prog.includes("acknowledged") ? "disabled" : ""}>${t.seen}</button>
          <button onclick="scLeft()" ${prog.includes("evacuated") ? "disabled" : ""}>${t.left}</button>
        </div>
        ${prog.includes("evacuated") ? `<p class="ok-text">${t.leftDone}</p>` : prog.includes("acknowledged") ? `<p class="ok-text">${t.seenDone}</p>` : ""}`;
    }
    app.innerHTML = `<div class="resident"><div class="card">
      <div class="muted small">${SC.esc(d.name)} (${SC.esc(d.household_id)}) · ${d.plan_id ? "plan " + SC.esc(d.plan_id) : "no approved plan yet"}
        · <a href="#" onclick="scLang('en');return false">English</a> · <a href="#" onclick="scLang('zh-Hant');return false">中文</a></div>
      ${body}</div>
      ${d.message ? `<p class="muted small" style="margin-top:12px">Message received: ${SC.esc(d.message)}</p>` : ""}</div>`;
  }
  SC.poll(refresh, 3000);
})();
