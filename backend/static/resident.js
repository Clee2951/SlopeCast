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
      noDeadline: "Prepare to leave", requested: "Help requested; a team has not confirmed yet.", assigned: (t, at) => `${t} has accepted and plans to arrive by ${at}.`,
      title: "Evacuate", waitTitle: "Stand by", calmTitle: "All clear here", prepare: "Start packing by", route: "Route ↗", plan: "plan", noApproved: "no approved plan yet",
      received: "Message received", household: "Household" },
    "zh-Hant": { noPlan: "目前尚未發布撤離指示。", noPlanSub: "委員會尚未核准計畫。請保持手機開機並留意官方公告。",
      none: "目前不需要撤離。", noneSub: "您不在委員會要求撤離的區域。請保持手機開機。",
      leave: "請在此時間前離開", go: "前往", bring: "請攜帶", help: "協助", seen: "我已看到", left: "我們已離開",
      seenDone: "謝謝，委員會已知道您看到通知。", leftDone: "已標記為撤離。請留在避難所直到通知安全。",
      noDeadline: "請準備離開", requested: "已申請協助，救援隊尚未確認。", assigned: (t, at) => `${t} 已確認，預計 ${at} 前抵達。`,
      title: "請撤離", waitTitle: "請待命", calmTitle: "目前安全", prepare: "請在此時間前開始準備", route: "路線 ↗", plan: "計畫", noApproved: "尚未核准計畫",
      received: "已收到訊息", household: "住戶" },
  };
  let lang = null, last = null, sending = false;

  async function send(kind) {
    if (sending) return; sending = true;
    try { await SC.post("/api/progress", { kind, event_id: `${kind}-${SC.token()}-${Date.now()}` }); await refresh(); }
    catch (e) { SC.toast(e.message); }
    sending = false;
  }
  window.scSeen = () => send("acknowledged");
  window.scLeft = () => send("evacuated");
  window.scLang = (l) => { lang = l; if (last) render(last); };

  app.innerHTML = `<div class="resident rise" id="res"></div>`;
  const root = document.getElementById("res");
  root.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-r]");
    if (!b) return;
    if (b.dataset.r === "seen") window.scSeen();
    else if (b.dataset.r === "left") window.scLeft();
    else window.scLang(b.dataset.r);
  });

  function render(d) {
    const code = lang || d.language;
    const t = T[code] || T.en;
    const zh = code === "zh-Hant";
    const prog = d.progress || [];
    let strip, body;
    if (!d.plan_id) {
      strip = `<div class="r-strip wait"><div class="title">${t.waitTitle}</div></div>`;
      body = `<div class="r-calm"><div class="big">${t.noPlan}</div><p>${t.noPlanSub}</p></div>`;
    } else if (!d.action_needed) {
      strip = `<div class="r-strip calm"><div class="title">${t.calmTitle}</div></div>`;
      body = `<div class="r-calm"><div class="big">${t.none}</div><p>${t.noneSub}</p></div>`;
    } else {
      const help = d.help ? (d.help.state === "accepted" || d.help.state === "arrived" ? t.assigned(SC.esc(d.help.team), SC.local(d.help.arrival_by_utc)) : t.requested) : "";
      const dest = d.destination;
      const route = dest && dest.lat !== undefined ? `<a href="https://www.google.com/maps/dir/?api=1&destination=${dest.lat},${dest.lon}" target="_blank" rel="noopener">${t.route}</a>` : "";
      strip = `<div class="r-strip"><div class="zone">${SC.esc(d.zone || "")}</div><div class="title">${t.title}</div></div>`;
      body = `<div class="r-body">
        ${d.leave_by_utc ? `<div class="r-leave"><div class="k">${t.leave}</div><div class="leave">${SC.local(d.leave_by_utc)}</div><div class="until" data-until="${SC.esc(d.leave_by_utc)}">${SC.untilText(d.leave_by_utc)}</div></div>
          ${d.prepare_by_utc ? `<div class="r-prep">${t.prepare} <b>${SC.local(d.prepare_by_utc)}</b></div>` : ""}`
          : `<div class="r-leave"><div class="leave" style="font-size:64px;line-height:1">${t.noDeadline}</div></div>`}
        <div class="r-sec"><div class="k">${t.go}</div><div class="v">${SC.esc(dest ? dest.name : "-")}${route}</div></div>
        <div class="r-sec"><div class="k">${t.bring}</div><ul class="r-list">${d.checklist.map((c) => `<li>${SC.esc(c)}</li>`).join("")}</ul></div>
        ${help ? `<div class="r-sec"><div class="k">${t.help}</div><div class="r-help">${help}</div></div>` : ""}
        <div class="actions">
          <button class="ink" data-r="seen" ${prog.includes("acknowledged") ? "disabled" : ""}>${prog.includes("acknowledged") ? "✓ " : ""}${t.seen}</button>
          <button class="go" data-r="left" ${prog.includes("evacuated") ? "disabled" : ""}>${prog.includes("evacuated") ? "✓ " : ""}${t.left}</button>
        </div>
        ${prog.includes("evacuated") ? `<p class="ok-text">${t.leftDone}</p>` : prog.includes("acknowledged") ? `<p class="ok-text">${t.seenDone}</p>` : ""}</div>`;
    }
    root.className = "resident" + (zh ? " zh-ui" : "");
    document.documentElement.lang = zh ? "zh-Hant" : "en";
    root.innerHTML = `
      <div class="r-meta"><span><b>${SC.esc(d.name)}</b> (${SC.esc(d.household_id)}) · ${d.plan_id ? t.plan + " " + SC.esc(d.plan_id) : t.noApproved}</span>
        <span class="seg" role="group" aria-label="Language"><button data-r="en" aria-pressed="${!zh}">English</button><button data-r="zh-Hant" aria-pressed="${zh}">中文</button></span></div>
      <div class="r-card">${strip}${body}</div>
      ${d.message ? `<p class="r-msg">${t.received}: ${SC.esc(d.message)}</p>` : ""}`;
  }

  async function refresh() {
    let d;
    try { d = await SC.get("/api/me/plan"); }
    catch (e) { root.innerHTML = `<div class="gate"><h1>Resident<br>link needed</h1><p>${SC.esc(e.message)}. Open with <code>?view=resident&amp;token=resident-h02</code> (tokens are in data/demo/users.json).</p></div>`; SC.changed("res", null); return; }
    SC.clock(); SC.modeBadge(d);
    last = d;
    const { server_time_utc, replay, ...stable } = d;      // ignore fields that change every poll
    if (SC.changed("res", [stable, lang])) render(d);
  }
  SC.poll(refresh, 3000);
})();
