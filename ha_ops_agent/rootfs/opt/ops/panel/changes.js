"use strict";
/* History, the System sheet and the chat views.
   Loaded by index.html under its nonce. Everything from the API is rendered as text nodes, never parsed as HTML. */
(() => {
const P = window.opsPanel;
const { h, btn, api, toast, S, go, row, section } = P;

/* ---------------------------------------------------------------- History: one row per item, by day, quiet folds */
const FILTERS = [["all", "All"], ["needs", "Needs you"], ["changes", "Changes"], ["checks", "Checks"]];
let asked = false;
function history(selected) {
  const hs = S.hist, f = hs ? hs.filter : S.arg || "all", n = hs ? hs.counts.needs : 0;
  if (!hs && !asked) { asked = true; P.loadHistory(f); }
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Filter" }, FILTERS.map(([id, label]) =>
    h("button", { "aria-pressed": String(f === id), onclick: () => go(id === "all" ? "#history" : `#history/${id}`, true) }, label, id === "needs" && n ? ` ${n}` : "")));
  if (!hs) return [seg, h("p", { class: "empty", text: "Loading…" })];
  if (!hs.items.length) {
    return [seg, h("div", { class: "calm" }, h("div", null, h("h2", { text: "Nothing yet" }),
      h("p", { text: f === "all" ? "What the agent does appears here, one line per item." : "Nothing in this filter." }),
      f === "all" ? h("div", { class: "btns" }, btn("Run a health check", runCheck)) : null))];
  }
  const z = P.tz(), days = [];
  for (const t of hs.items) {
    const k = P.dayKey(t.at, z);
    if (!days.length || days.at(-1).k !== k) days.push({ k, at: t.at, items: [] });
    days.at(-1).items.push(t);
  }
  return [seg, days.map((d) => section(P.dayLabel(d.at, z), null, dayRows(d, selected))),
    hs.more ? h("div", { class: "btns" }, btn("Older", () => P.loadHistory(f, true), "text")) : null];
}
/* A day's routine entries fold into one line where the first of them was; tapping it shows them. */
function dayRows(d, selected) {
  const quiet = d.items.filter((t) => t.quiet), key = `q${d.k}`, open = !!S.open[key], out = [];
  for (const t of d.items) {
    if (!t.quiet) out.push(row(t, t.id === selected, true));
    else if (t === quiet[0]) {
      out.push(h("button", { class: "row quiet", "aria-expanded": String(open), onclick: () => { S.open[key] = !open; P.render(); } },
        h("span", { class: "t" }, h("b", { text: `${quiet.length} routine ${quiet.length === 1 ? "entry" : "entries"} · ${quiet.map((x) => x.title).join(", ")}` })),
        h("span", { class: "r" }, h("span", { class: "tnum", text: P.hhmm(t.at, P.tz()) }), P.icon("chev"))));
      if (open) out.push(quiet.map((x) => row(x, x.id === selected, true)));
    }
  }
  return out;
}
async function runCheck() {
  const r = await api("POST", "api/run", {});
  toast(r.data && r.data.started ? "Started; the result appears on Home." : "Not started: one is running or today's limit is reached.");
}

/* ---------------------------------------------------------------- System: the former Status, usage, rules and switches */
const kv = (label, value, sub) => h("div", { class: "kv" }, h("span", { text: label }), h("span", { text: value == null || value === "" ? "–" : String(value) }), sub ? h("small", { text: sub }) : null);
function swRow(label, on, change, disabled, sub) {
  const sw = h("input", { type: "checkbox", class: "sw", role: "switch", "aria-label": label, onchange: change });
  sw.checked = !!on; sw.disabled = !!disabled;
  return h("label", { class: "kv" }, h("span", { text: label }), sw, sub ? h("small", { text: sub }) : null);
}
const buttons = (...kids) => h("div", { class: "kv b" }, kids);
const note = (key) => (S.note[key] ? h("small", { role: "status", text: S.note[key] }) : null);
const say = (key, text) => { S.note[key] = text; P.render(); };
function system() {
  const st = S.state || {}, sys = S.sys || {}, o = (sys.ops || {}).status || {}, up = sys.updates || {}, c = up.control || {};
  const hp = (sys.ops || {}).profile || {};
  const b = up.backup || {}, bl = b.last || {}, u = st.usage || {}, cap = u.max_share_percent || 5, today = u.today || {};
  const z = P.tz(), at = (iso) => (iso ? `${P.when(iso, z)}` : "");
  const rules = sys.rules ? sys.rules.rules : null, held = st.held || {};
  const out = [
    section("Agent", null, [
      swRow("AI", st.ai && st.ai.on, toggleAi),
      kv("Mode", o.mode ? `${o.mode[0].toUpperCase()}${o.mode.slice(1)}` : st.mode),
      swRow("Automatic updates", c.option === "on" && !c.paused, toggleUpdates, c.option !== "on",
        c.effective ? `Nightly at ${c.window}: backup, install, health check, rollback.` : c.reason ? `Report only: ${c.reason}.` : null),
      kv("Version", st.version),
      kv("Self-test", [o.selftest, at(o.selftest_at)].filter(Boolean).join(" ")),
      buttons(btn("Run the self-test", selftest), btn("Download diagnostics", diagnostics), note("agent")),
    ]),
    section("Budget", null, [
      ...(st.ai && st.ai.credential === "api_key"
        ? [kv("Credential", "API key: no subscription windows, run caps and the per-run cost cap apply")]
        : [kv("5-hour share", `${P.share(u.agent_share_5h_percent, u.agent_share_5h_estimated)} of ${cap}%`),
           kv("7-day share", `${P.share(u.agent_share_7d_percent, u.agent_share_7d_estimated)} of ${cap}%`)]),
      kv("Runs today", `${today.runs || 0}`),
      kv("Chat messages today", `${(st.chat || {}).used || 0} of ${(st.chat || {}).cap || "–"}`),
      kv("Tokens today", P.fmtN(today.tokens)),
    ]),
    section("Backups", null, [
      kv("Last the agent started", b.running ? "running…" : bl.state ? `${bl.state === "ok" ? "complete" : bl.state} ${at(bl.finished_at || bl.started_at)}` : "none",
        bl.state === "failed" ? bl.detail : null),
      buttons(backupBtn(b), b.blocked && !b.running ? h("small", { text: b.blocked }) : null, note("backup")),
    ]),
    section("Checks", null, [
      kv("Health", [o.core_down_since ? `Core down since ${at(o.core_down_since)}` : o.last_result, at(o.last_health_at)].filter(Boolean).join(", ")),
      kv("Next daily check", o.next_health_check),
      kv("Accepted as normal", rules ? `${rules.length} rule${rules.length === 1 ? "" : "s"}` : ""),
      rules && rules.length ? h("details", { class: "kv b", open: S.open.rules || null, ontoggle: (e) => { S.open.rules = e.target.open; } },
        h("summary", { text: "Show the rules" }), rules.map(ruleLine)) : null,
      buttons(btn("Run a health check", runCheck)),
    ]),
    section("House profile", null, [
      kv("Profile", hp.loaded ? hp.path : hp.problems && hp.problems.length ? "refused" : "none (generic agent)",
        hp.problems && hp.problems.length ? hp.problems.join("; ") : null),
      kv("Git proposals", hp.git ? "on" : "off"),
      kv("House overlays", (hp.overlays || []).join(", ") || "none"),
    ]),
    section("Notifications", null, [
      kv("Pushes", hp.notify_problem ? "off: the device is not an admin's" : hp.push_target === false ? "off: no notify service (add-on option or house profile)" : "only when you are needed",
        hp.notify_problem || null),
      st.quiet ? kv("Quiet hours", `${st.quiet.from} to ${st.quiet.to}`) : null,
      held.count ? kv("Held until quiet hours end", String(held.count)) : null,
    ]),
  ];
  return out;
}
function ruleLine(r) {
  return h("div", { class: "kv" }, h("span", null, h("b", { text: r.key }), h("small", { text: ` · suppressed while ${r.verifier_text}. ${r.reason}` })),
    r.state === "active" ? btn("Revoke", () => revokeRule(r), "text") : h("span", { text: r.state }));
}
async function revokeRule(r) {
  if (!confirm(`Revoke the rule for ${r.key}? It is reported again.`)) return;
  const res = await api("POST", `api/suppressions/${r.id}/revoke`, {});
  toast(res.status === 200 ? `Revoked.${res.data.proposal ? " Approve the file change on Home." : ""}` : "Could not revoke it.");
  P.loadSystem();
}
function backupBtn(b) {
  const el = btn(b.running ? "Backing up…" : "Back up now", backupNow);
  el.disabled = !!b.blocked || !!b.running;
  return el;
}
async function backupNow() {
  const r = await api("POST", "api/backups/now", {});
  say("backup", r.status === 200 ? "Backing up… (a few minutes; the result appears on Home)" : r.data.message || "Not possible now.");
  P.loadSystem();
}
async function diagnostics() {
  const r = await api("GET", "api/diagnostics");
  if (r.status !== 200) { say("agent", "Could not build the diagnostics."); return; }
  const url = URL.createObjectURL(new Blob([JSON.stringify(r.data, null, 2)], { type: "application/json" }));
  // A download, not navigation: the blob URL is absolute, so the page's <base> does not apply.
  Object.assign(document.createElement("a"), { href: url, download: `ha-ops-agent-diagnostics-${String(r.data.generated_at).slice(0, 10)}.json` }).click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  say("agent", "Diagnostics downloaded. Secrets are removed; read it before you share it.");
}
async function selftest() {
  say("agent", "Running the self-test…");
  const r = await api("POST", "api/selftest", {});
  say("agent", r.status === 200 ? `Self-test: ${(r.data.result || {}).ok ? "pass" : "FAIL"}` : "The self-test could not run.");
  P.loadSystem();
}
async function toggleAi(ev) {
  const on = ev.target.checked;
  if (!on && !confirm("Stop AI? A run in progress is stopped.")) { ev.target.checked = true; return; }
  const r = await api("POST", "api/ai", { on });
  if (r.status === 200) { S.state = { ...S.state, ...r.data.state }; P.render(); }
  else { toast(r.data.message || "Could not change AI."); ev.target.checked = !on; }
}
async function toggleUpdates(ev) {
  const on = ev.target.checked;
  if (!on && !confirm("Pause automatic updates? Tonight's window will only report.")) { ev.target.checked = true; return; }
  const r = await api("POST", "api/updates/auto", { on });
  if (r.status === 200) P.loadSystem();
  else { toast(r.data.message || "Could not change automatic updates."); ev.target.checked = !on; }
}

/* ---------------------------------------------------------------- Chat: the one conversation (state and live
   updates stay in index.html; this renders it). Model text goes through rich(): text nodes only. */
const { els } = P;
const MAX = 2000, WARN_AT = 1800;
const CARD = {
  ai_disabled: ["AI is off", "power"], share_cap: ["Agent share used for this window", "clock"],
  run_cap_reached: ["Run limit reached", "clock"], usage_limited: ["AI usage limit", "clock"],
  account_busy: ["Subscription busy", "clock"], skipped_busy: ["Agent busy", "clock"],
  chat_cap: ["Chat limit reached", "clock"], busy: ["Not sent", "alert"], interrupted: ["Interrupted", "alert"], error: ["Run failed", "alert"],
  investigation: ["Investigation", "search"], share_reserve: ["Saving budget for monitoring", "clock"], proposal: ["Approval requested", "alert"],
};
function chat(content, banners) {
  let v = els.thread;
  if (!v || v.id !== S.thread || !v.root.isConnected) {
    const log = h("div", { class: "log" });
    v = els.thread = { id: S.thread, log, root: log, comp: S.thread ? composer() : null, scrollDown: true };
    content.replaceChildren(...banners(), log, v.comp ? v.comp.el : h("p", { class: "empty", text: "Loading the chat…" }));
  }
  const body = v.log, nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
  const chat = h("div", { class: "chat", role: "log", "aria-label": "Conversation" });
  if (S.more) chat.append(btn("Load earlier", () => P.loadThread(S.turns.find((t) => typeof t.id === "number").id), "text"));
  let day = "";
  for (const t of S.turns) {
    const iso = t.at || new Date().toISOString(), k = P.dayKey(iso, P.tz());
    if (k !== day) { day = k; chat.append(h("div", { class: "daysep", text: P.dayLabel(iso, P.tz()) })); }
    chat.append(...turnEls(t)); /* turnEls never returns null: append(null) would render the text "null" */
  }
  if (S.inflight) chat.append(working());
  body.replaceChildren(chat);
  if (v.comp) v.comp.sync();
  if (nearBottom || v.scrollDown || els.scrollDown) { body.scrollTop = body.scrollHeight; v.scrollDown = false; els.scrollDown = false; }
}
async function remember(t) {
  S.menuFor = null;
  const text = prompt("Remember as a house fact (edit it first; it is proposed as a change you approve):", t.text.slice(0, 500));
  if (text === null) { P.render(); return; }
  const r = await api("POST", `api/threads/${t.thread}/remember`, { turn_id: t.id, text: text.slice(0, 500) });
  toast(r.status === 201 ? "Proposed. It appears on Home under Needs you." : r.data.message || "Could not save it.");
  P.loadHome();
}
function turnMenu(t) {
  const open = S.menuFor === t.id;
  return [h("button", { "aria-label": "Message options", "aria-expanded": String(open), onclick: () => { S.menuFor = open ? null : t.id; P.render(); } }, "···"),
    open ? h("div", { class: `btns meta ${t.role === "admin" ? "r" : "l"}` },
      btn("Copy", () => copy(t.text), "text"),
      typeof t.id === "number" ? btn("Remember this", () => remember(t), "text") : null,
      t.run_id ? h("span", { text: `Run ${t.run_id}` }) : null) : null];
}
function turnEls(t) {
  return turnParts(t).filter((el) => el !== null && el !== undefined && el !== false);
}
const hasText = (t) => typeof t.text === "string" && t.text.trim() !== "";
function turnParts(t) {
  if (!hasText(t)) return [];
  const at = t.at ? P.hhmm(t.at, P.tz()) : "";
  if (t.role === "admin") {
    const out = [h("div", { class: `msg me ${t.status === "sending" ? "sending" : ""}`, text: t.text })];
    const st = { sending: "Sending…", queued: "Sent", running: "Sent", ok: "Sent", refused: "Sent", failed: "Not answered" }[t.status] || "";
    const [mb, menu] = turnMenu(t);
    out.push(h("div", { class: "meta r" }, [at, st, t.meta && t.meta.via ? "via notification" : ""].filter(Boolean).join(" · "), mb), menu);
    if (t.status === "unsent") out.push(h("button", { class: "fail", onclick: () => resend(t) }, "Not sent: tap to retry"));
    return out;
  }
  if (t.role === "agent") {
    const m = t.meta || {};
    const bits = [at, m.duration_s ? `${Math.round(m.duration_s)} s` : "", m.push === "skipped_watching" ? "no push (you were here)" : "",
      m.refused && `Refused: ${m.refused.join(", ")}`].filter(Boolean);
    const [mb, menu] = turnMenu(t);
    return [h("div", { class: "msg ag" }, h("div", { class: "tag" }, P.icon("spark"), "Agent"), P.rich(t.text),
      m.sources && m.sources.length ? h("div", { class: "src", text: `Sources: ${m.sources.join(", ")}` }) : null),
    h("div", { class: "meta l" }, bits.join(" · "), mb), menu];
  }
  if (t.status === "summary") {
    return [h("details", { class: "ctx" }, h("summary", { text: `Earlier messages summarised (${(t.meta || {}).turns || "?"} turns)` }), h("div", { text: t.text }))];
  }
  return [card(t)];
}
function card(t) {
  const m = t.meta || {}, [title, ic] = CARD[t.status] || ["Notice", "alert"];
  const err = ["error", "busy", "interrupted"].includes(t.status);
  const acts = m.resolved ? [] : (m.actions || []);
  const btns = [];
  const turn = t.reply_to;
  if (acts.includes("turn_on_retry")) btns.push(btn("Turn AI on & retry", () => turnOnAndRetry(turn), "fill"));
  if (acts.includes("updates")) btns.push(btn("Review on Home", () => go("#home"), "fill"));
  if (acts.includes("backup")) btns.push(btn("Back up now", async () => {
    const r = await api("POST", "api/backups/now", {});
    toast(r.status === 200 ? "Backing up… The result appears on Home." : r.data.message || "Not possible now.");
  }, "fill"));
  if (acts.includes("cancel")) btns.push(btn("Cancel", () => api("POST", `api/threads/${S.thread}/cancel`, { task_id: m.task })));
  if (acts.includes("retry")) btns.push(h("button", { class: `btn ${acts.includes("turn_on_retry") ? "" : "fill"}`, onclick: () => retry(turn) }, "Retry now"));
  let auto = null;
  if (acts.includes("cancel_auto") || acts.includes("enable_auto")) {
    const sw = h("input", { type: "checkbox", class: "sw", role: "switch", "aria-label": "Retry automatically", onchange: (e) => autoRetry(turn, e.target.checked) });
    sw.checked = !!m.auto_retry;
    auto = h("label", { class: "auto" }, "Auto-retry", sw);
  }
  return h("div", { class: `ccard ${t.status} ${err ? "err" : ""} ${m.resolved || m.done ? "done" : ""}`, role: "status" },
    h("div", { class: "k" }, P.icon(ic), title),
    h("p", { text: t.text }),
    m.resolved ? h("p", { class: "src", text: "Answered after a retry." }) : m.retrying ? h("p", { class: "src", text: "Retrying…" }) : null,
    btns.length || auto ? h("div", { class: "btns" }, btns, auto) : null);
}
const WAIT = { triage: "the health check", chat: "another chat", followup: "a follow-up", summary: "a summary", investigate: "an investigation" };
function working() {
  const f = S.inflight, started = f.started_at ? new Date(f.started_at) : new Date();
  let label = "Working";
  if (f.state === "queued") label = f.waiting_for ? `Queued: waiting for ${WAIT[f.waiting_for] || "another run"}` : `Queued${f.position > 1 ? ` (#${f.position})` : ""}`;
  else if (f.state === "waiting") label = `Waiting for ${WAIT[f.waiting_for] || "another run"} to finish`;
  else if (f.state === "summarising") label = "Summarising earlier messages";
  if (label !== els.lastLabel) { els.lastLabel = label; P.announce(label); }
  const time = h("time", { text: P.clock((Date.now() - started) / 1000) });
  els.timer = { el: time, started };
  const steps = (f.steps || []).map((s, i, a) => h("li", { class: i === a.length - 1 ? "now" : "", text: s }));
  const slow = Date.now() - started > 90000;
  return h("div", { class: "working", "aria-label": label },
    h("div", { class: "dots", "aria-hidden": "true" }, h("i"), h("i"), h("i")),
    h("div", { class: "lbl" }, h("span", { text: label }), time),
    steps.length ? h("ul", { class: "steps" }, steps) : null,
    slow ? h("div", { class: "src", text: "Taking longer than usual. You'll get a push if you leave." }) : null);
}
function composer() {
  const id = S.thread;
  const ta = h("textarea", { rows: "1", "aria-label": "Message the agent", placeholder: "Message the agent", maxlength: String(MAX + 200) });
  ta.value = S.drafts[id] || "";
  const count = h("div", { class: "count" });
  const send = h("button", { class: "send", "aria-label": "Send" }, P.icon("send"));
  const sync = () => {
    const busy = !!S.inflight || S.turns.some((t) => t.role === "admin" && ["sending", "queued", "running"].includes(t.status));
    S.drafts[id] = ta.value; P.saveDrafts();
    ta.style.height = "auto"; ta.style.height = `${Math.min(160, ta.scrollHeight)}px`;
    const n = ta.value.length;
    count.textContent = n >= WARN_AT ? `${n} / ${MAX}` : "";
    count.className = `count ${n > MAX ? "over" : ""}`;
    send.disabled = busy || !ta.value.trim() || n > MAX;
    send.setAttribute("aria-label", busy ? "Send (waiting for the answer)" : "Send");
  };
  ta.addEventListener("input", sync);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer:fine)").matches) { e.preventDefault(); if (!send.disabled) submit(ta); }
  });
  send.addEventListener("click", () => submit(ta));
  els.composer = ta;
  return { el: h("div", null, count, h("div", { class: "composer" }, ta, send)), sync, ta };
}
function clientId() {
  const b = new Uint8Array(12); crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
async function submit(ta) {
  const text = ta.value.trim();
  if (!text || text.length > MAX || !S.thread) return;
  const turn = { id: null, client_id: clientId(), thread: S.thread, role: "admin", text, status: "sending", at: new Date().toISOString(), meta: {} };
  S.drafts[S.thread] = ""; P.saveDrafts(); ta.value = "";
  S.turns.push(turn); els.scrollDown = true; P.render();
  await deliver(turn);
  if (els.composer) els.composer.focus();
}
async function deliver(turn) {
  const r = await api("POST", `api/threads/${turn.thread}/messages`, { text: turn.text, client_id: turn.client_id });
  if (r.data.turn) { Object.assign(turn, r.data.turn); }
  if (r.status === 202 || r.status === 200) {
    if (r.status === 202) S.inflight = S.inflight || { turn_id: turn.id, state: "queued", position: r.data.position, steps: [], started_at: new Date().toISOString() };
  } else if (r.status === 409) {
    S.turns = S.turns.filter((t) => t !== turn); restoreDraft(turn); toast(r.data.message || "Wait for the answer first.");
  } else if (r.status === 413 || r.status === 400) {
    S.turns = S.turns.filter((t) => t !== turn); restoreDraft(turn); toast(r.data.message || "That message cannot be sent.");
  } else if (r.status !== 503) {
    turn.status = "unsent";
  }
  P.render();
}
function restoreDraft(turn) {
  S.drafts[turn.thread] = turn.text; P.saveDrafts();
  if (els.thread && els.thread.id === turn.thread && els.thread.comp && !els.thread.comp.ta.value) els.thread.comp.ta.value = turn.text;
}
function resend(turn) { turn.status = "sending"; P.render(); deliver(turn); }
async function retry(turnId) {
  const r = await api("POST", `api/threads/${S.thread}/retry`, { turn_id: turnId });
  if (r.status !== 202) toast({ cap: "Too many retries today.", not_retryable: "Already answered or retrying.", busy: "The agent is busy; try again shortly." }[r.data.error] || "Could not retry.");
}
async function autoRetry(turnId, on) {
  const r = await api("POST", `api/threads/${S.thread}/autoretry`, { turn_id: turnId, on });
  if (r.status !== 200) { toast("Automatic retry is not possible any more."); P.loadThread(); }
}
async function turnOnAndRetry(turnId) {
  const r = await api("POST", "api/ai", { on: true });
  if (r.status === 200) retry(turnId);
  else toast(r.data.message || "Could not turn AI on.");
}
function copy(text) {
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast("Copied."), () => toast("Copy is not available here."));
  S.menuFor = null; P.render();
}

window.opsViews = { history, system, chat, card, turnEls };
})();
