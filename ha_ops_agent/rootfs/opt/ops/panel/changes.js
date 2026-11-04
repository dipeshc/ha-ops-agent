"use strict";
/* History, Ask, the System sheet and the conversation on an item.
   Loaded by index.html under its nonce. Everything from the API is rendered as text nodes, never parsed as HTML. */
(() => {
const P = window.opsPanel;
const { h, btn, api, toast, S, go, row, section } = P;

/* ---------------------------------------------------------------- History: one row per item, by day, quiet folds */
const FILTERS = [["all", "All"], ["needs", "Needs you"], ["changes", "Changes"], ["questions", "Questions"], ["checks", "Checks"]];
let asked = false;
function history(selected) {
  const hs = S.hist, f = hs ? hs.filter : S.arg || "all", n = hs ? hs.counts.needs : 0;
  if (!hs && !asked) { asked = true; P.loadHistory(f); }
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Filter" }, FILTERS.map(([id, label]) =>
    h("button", { "aria-pressed": String(f === id), onclick: () => go(id === "all" ? "#history" : `#history/${id}`, true) }, label, id === "needs" && n ? ` ${n}` : "")));
  if (!hs) return [seg, h("p", { class: "empty", text: "Loading…" })];
  if (!hs.items.length) {
    return [seg, h("div", { class: "calm" }, h("div", null, h("h2", { text: "Nothing yet" }),
      h("p", { text: f === "all" ? "What the agent does appears here, one line per item." : f === "questions" ? "Your questions appear here with their answers." : "Nothing in this filter." }),
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
  const b = up.backup || {}, bl = b.last || {}, u = st.usage || {}, today = u.today || {};
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
        ? [kv("Credential", "API key: no subscription windows, run caps and the cost caps apply")]
        : [kv("Run time, 7 days", `${P.minutes(u.run_time_7d_s)} of ${P.minutes(u.run_time_7d_cap_s)} min`),
           kv("Run time, 5 hours", `${P.minutes(u.run_time_5h_s)} of ${P.minutes(u.run_time_5h_cap_s)} min`),
           u.account_7d_percent != null ? kv("Account, 7 days", `${u.account_7d_percent} %`) : null]),
      u.daily_max_usd ? kv("Cost today", `$${(u.cost_today_usd || 0).toFixed(2)} of $${Number(u.daily_max_usd).toFixed(2)}`) : null,
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

/* ---------------------------------------------------------------- Ask: the questions, newest first, and the one place to start one */
const { els } = P;
const MAX = 2000, WARN_AT = 1800;
const RULE = "Something on Home? Open the item and ask there instead; the answer stays with it.";
function ask(selected) {
  const qs = S.asks, list = !qs ? [h("p", { class: "empty", text: "Loading…" })]
    : !qs.length ? [h("div", { class: "calm q" }, h("span", { class: "g" }, P.icon("chat")), h("div", null, h("h2", { text: "Ask anything" }),
      h("p", { text: "For anything on Home, open the item and ask there; the answer stays with it. This is for the rest: the house, the agent, what to do next. Each question keeps its own answer and follow-ups." })))]
    : [section("Your questions", null, qs.map((q) => P.row(q, q.id === selected))), h("p", { class: "empty", text: RULE })];
  return [...list, h("div", { class: "askc" }, composer(null))];
}

/* ---------------------------------------------------------------- the conversation on an item (state and live updates stay in
   index.html; this renders it). Model text goes through rich(): text nodes only. */
const CARD = {
  ai_disabled: ["AI is off", "power"], share_cap: ["Run-time budget used for this window", "clock"],
  run_cap_reached: ["Run limit reached", "clock"], usage_limited: ["AI usage limit", "clock"],
  account_busy: ["Subscription busy", "clock"], skipped_busy: ["Agent busy", "clock"],
  chat_cap: ["Message limit reached", "clock"], busy: ["Not sent", "alert"], interrupted: ["Interrupted", "alert"], error: ["Run failed", "alert"],
  investigation: ["Investigation", "search"], share_reserve: ["Saving budget for monitoring", "clock"], cost_cap: ["Daily cost cap reached", "clock"],
  proposal: ["Back up now?", "alert"],
};
function convo(t) {
  const c = S.conv, q = t.kind === "question";
  if (!c.loaded) return h("div", { class: "thread" }, h("p", { class: "empty", text: "Loading the conversation…" }));
  const log = h("div", { class: "msgs", role: "log", "aria-label": q ? "Conversation" : "About this item", "aria-live": "polite" });
  if (c.more) log.append(btn("Load earlier", () => P.loadConv(c.turns.find((x) => typeof x.id === "number").id), "text"));
  let day = "";
  for (const turn of c.turns) {
    const iso = turn.at || new Date().toISOString(), k = P.dayKey(iso, P.tz());
    if (k !== day && c.turns.length > 1) { day = k; log.append(h("div", { class: "daysep", text: P.dayLabel(iso, P.tz()) })); }
    log.append(...turnEls(turn)); /* turnEls never returns null: append(null) would render the text "null" */
  }
  if (c.inflight) log.append(working());
  const has = c.turns.length || c.inflight;
  return h("section", { class: "thread", "aria-label": q ? "Conversation" : "About this item" },
    has ? h("h3", { text: q ? "Conversation" : "About this item" }) : null, has ? log : null,
    c.failed ? h("p", { class: "ro", role: "alert", text: "The conversation could not be loaded; retrying when the daemon answers." }) : null,
    c.readOnly ? h("p", { class: "ro", text: "The one chat of earlier versions, kept read only. Ask something new on Ask." })
      : h("div", { class: "dock" }, composer(t)));
}
async function remember(turn) {
  S.menuFor = null;
  const text = prompt("Remember as a house fact (edit it first; it is proposed as a change you approve):", turn.text.slice(0, 500));
  if (text === null || !S.conv) { P.render(); return; }
  const r = await api("POST", P.itemUrl(S.conv.item, "/remember"), { turn_id: turn.id, text: text.slice(0, 500) });
  toast(r.status === 201 ? "Proposed. It appears on Home under Needs you." : r.data.message || "Could not save it.");
  P.loadHome();
}
function turnMenu(turn) {
  const open = S.menuFor === turn.id;
  return [h("button", { "aria-label": "Message options", "aria-expanded": String(open), onclick: () => { S.menuFor = open ? null : turn.id; P.render(); } }, "···"),
    open ? h("div", { class: `btns meta ${turn.role === "admin" ? "r" : "l"}` },
      btn("Copy", () => copy(turn.text), "text"),
      typeof turn.id === "number" && !(S.conv || {}).readOnly ? btn("Remember this", () => remember(turn), "text") : null,
      turn.run_id ? h("span", { text: `Run ${turn.run_id}` }) : null) : null];
}
function turnEls(turn) {
  return turnParts(turn).filter((el) => el !== null && el !== undefined && el !== false);
}
const hasText = (turn) => typeof turn.text === "string" && turn.text.trim() !== "";
function turnParts(turn) {
  if (!hasText(turn)) return [];
  const at = turn.at ? P.hhmm(turn.at, P.tz()) : "";
  if (turn.role === "admin") {
    const out = [h("div", { class: `msg me ${turn.status === "sending" ? "sending" : ""}`, text: turn.text })];
    const st = { sending: "Sending…", queued: "Sent", running: "Sent", ok: "Sent", refused: "Sent", failed: "Not answered" }[turn.status] || "";
    const [mb, menu] = turnMenu(turn);
    out.push(h("div", { class: "meta r" }, [at, st, turn.meta && turn.meta.via ? "via notification" : ""].filter(Boolean).join(" · "), mb), menu);
    if (turn.status === "unsent") out.push(h("button", { class: "fail", onclick: () => resend(turn) }, "Not sent: tap to retry"));
    return out;
  }
  if (turn.role === "agent") {
    const m = turn.meta || {};
    const bits = [at, m.duration_s ? `${Math.round(m.duration_s)} s` : "", m.push === "skipped_watching" ? "no push (you were here)" : "",
      m.refused && `Refused: ${m.refused.join(", ")}`].filter(Boolean);
    const [mb, menu] = turnMenu(turn);
    return [h("div", { class: "msg ag" }, h("div", { class: "tag" }, P.icon("spark"), "Agent"), P.rich(turn.text),
      m.sources && m.sources.length ? h("div", { class: "src", text: `Sources: ${m.sources.join(", ")}` }) : null),
    h("div", { class: "meta l" }, bits.join(" · "), mb), menu];
  }
  if (turn.status === "summary") {
    return [h("details", { class: "ctx" }, h("summary", { text: `Earlier messages summarised (${(turn.meta || {}).turns || "?"} turns)` }), h("div", { text: turn.text }))];
  }
  if (turn.status === "link") return [link(turn)];
  return [card(turn)];
}
/* What a run started or proposed is an item of its own: the card opens it, it never acts. */
function link(turn) {
  const id = (turn.meta || {}).item, cur = S.items[id];
  return h("button", { class: "link", onclick: () => P.open(id) }, h("span", { class: "g q" }, P.icon("doc")),
    h("span", { class: "t" }, h("b", { text: turn.text }), cur ? h("span", { text: cur.gist ? `${cur.state} · ${cur.gist}` : cur.state }) : null), P.icon("chev"));
}
function card(turn) {
  const m = turn.meta || {}, [title, ic] = CARD[turn.status] || ["Notice", "alert"];
  const err = ["error", "busy", "interrupted"].includes(turn.status);
  const acts = m.resolved || (S.conv || {}).readOnly ? [] : (m.actions || []);
  const btns = [];
  const ref = turn.reply_to;
  if (acts.includes("turn_on_retry")) btns.push(btn("Turn AI on & retry", () => turnOnAndRetry(ref), "fill"));
  if (acts.includes("backup")) btns.push(btn("Back up now", async () => {
    const r = await api("POST", "api/backups/now", {});
    toast(r.status === 200 ? "Backing up… The result appears on Home." : r.data.message || "Not possible now.");
  }, "fill"));
  if (acts.includes("cancel")) btns.push(btn("Stop", () => api("POST", P.itemUrl(S.conv.item, "/cancel"), { task_id: m.task }), "danger"));
  if (acts.includes("retry")) btns.push(h("button", { class: `btn ${acts.includes("turn_on_retry") ? "" : "fill"}`, onclick: () => retry(ref) }, "Retry now"));
  let auto = null;
  if (acts.includes("cancel_auto") || acts.includes("enable_auto")) {
    const sw = h("input", { type: "checkbox", class: "sw", role: "switch", "aria-label": "Retry automatically", onchange: (e) => autoRetry(ref, e.target.checked) });
    sw.checked = !!m.auto_retry;
    auto = h("label", { class: "auto" }, "Auto-retry", sw);
  }
  return h("div", { class: `ccard ${turn.status} ${err ? "err" : ""} ${m.resolved || m.done ? "done" : ""}`, role: "status" },
    h("div", { class: "k" }, P.icon(ic), title),
    h("p", { text: turn.text }),
    m.resolved ? h("p", { class: "src", text: "Answered after a retry." }) : m.retrying ? h("p", { class: "src", text: "Retrying…" }) : null,
    btns.length || auto ? h("div", { class: "btns" }, btns, auto) : null);
}
const WAIT = { triage: "the health check", chat: "another conversation", followup: "a follow-up", summary: "a summary", investigate: "an investigation", maintenance_plan: "the maintenance plan" };
function working() {
  const f = S.conv.inflight, started = f.started_at ? new Date(f.started_at) : new Date();
  let label = "Working";
  if (f.state === "queued") label = f.waiting_for ? `Queued: waiting for ${WAIT[f.waiting_for] || "another run"}` : `Queued${f.position > 1 ? ` (#${f.position})` : ""}`;
  else if (f.state === "waiting") label = `Waiting for ${WAIT[f.waiting_for] || "another run"} to finish`;
  else if (f.state === "summarising") label = "Summarising earlier messages";
  if (label !== els.lastLabel) { els.lastLabel = label; P.announce(label); }
  const time = h("time", { text: P.clock((Date.now() - started) / 1000) });
  els.timer = { el: time, started };
  const steps = (f.steps || []).map((x, i, a) => h("li", { class: i === a.length - 1 ? "now" : "", text: x }));
  const slow = Date.now() - started > 90000;
  return h("div", { class: "working", "aria-label": label },
    h("div", { class: "dots", "aria-hidden": "true" }, h("i"), h("i"), h("i")),
    h("div", { class: "lbl" }, h("span", { text: label }), time),
    steps.length ? h("ul", { class: "steps" }, steps) : null,
    slow ? h("div", { class: "src", text: "Taking longer than usual. You'll get a push if you leave." }) : null);
}
/* One box per conversation (`t`), or Ask's (`null`): a real labelled textarea, its draft kept for this tab only. */
function composer(t) {
  const key = t ? `i:${t.id}` : "ask", label = !t ? "Ask about the house or the agent" : t.kind === "question" ? "Ask a follow-up" : "Ask about this item";
  const ta = h("textarea", { rows: "1", "aria-label": label, placeholder: label, maxlength: String(MAX + 200) });
  ta.value = S.drafts[key] || "";
  const count = h("div", { class: "count", "aria-live": "polite" });
  const send = h("button", { class: "send", "aria-label": "Send" }, P.icon("send"));
  const busy = () => !!t && !!S.conv && (!!S.conv.inflight || S.conv.turns.some((x) => x.role === "admin" && ["sending", "queued", "running"].includes(x.status)));
  const sync = () => {
    S.drafts[key] = ta.value; P.saveDrafts();
    ta.style.height = "auto"; ta.style.height = `${Math.min(160, ta.scrollHeight)}px`;
    const n = ta.value.length, wait = busy();
    count.textContent = n >= WARN_AT ? `${n} / ${MAX}` : "";
    count.className = `count ${n > MAX ? "over" : ""}`;
    send.disabled = wait || !ta.value.trim() || n > MAX;
    send.setAttribute("aria-label", wait ? "Send (waiting for the answer)" : "Send");
  };
  const fire = () => (t ? submit(ta, key) : askNew(ta));
  ta.addEventListener("input", sync);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer:fine)").matches) { e.preventDefault(); if (!send.disabled) fire(); }
  });
  send.addEventListener("click", fire);
  sync();
  return h("div", null, count, h("div", { class: "composer" }, ta, send));
}
function clientId() {
  const b = new Uint8Array(12); crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
/* Ask: the question becomes an item, and its page opens at once to watch the answer arrive there. */
async function askNew(ta) {
  const text = ta.value.trim();
  if (!text || text.length > MAX || els.asking) return;
  els.asking = true;
  const body = { text, client_id: clientId() };
  const r = await api("POST", "api/ask", body);
  els.asking = false;
  if (!r.data.item || r.status >= 400) { toast(r.data.message || "Not sent: try again."); return; }
  S.drafts.ask = ""; P.saveDrafts(); ta.value = "";
  const it = r.data.item;
  P.put(it);
  S.conv = { item: it.id, thread: r.data.turn ? r.data.turn.thread : null, turns: r.data.turn ? [r.data.turn] : [], more: false, loaded: true, readOnly: false, info: null,
    inflight: r.status === 202 ? { turn_id: r.data.turn.id, state: "queued", position: r.data.position, steps: [], started_at: new Date().toISOString() } : null };
  S.asks = [it, ...(S.asks || []).filter((x) => x.id !== it.id)];
  S.from = "ask";
  P.open(it.id);
}
async function submit(ta, key) {
  const text = ta.value.trim(), c = S.conv;
  if (!text || text.length > MAX || !c || c.readOnly) return;
  const turn = { id: null, client_id: clientId(), thread: c.thread, role: "admin", text, status: "sending", at: new Date().toISOString(), meta: {} };
  S.drafts[key] = ""; P.saveDrafts(); ta.value = "";
  c.turns.push(turn); els.scrollDown = true; P.render();
  await deliver(turn, c);
}
async function deliver(turn, c) {
  const r = await api("POST", P.itemUrl(c.item, "/messages"), { text: turn.text, client_id: turn.client_id });
  if (r.data.turn) Object.assign(turn, r.data.turn);
  if (r.status === 202 || r.status === 200) {
    if (r.status === 202) c.inflight = c.inflight || { turn_id: turn.id, state: "queued", position: r.data.position, steps: [], started_at: new Date().toISOString() };
  } else if (r.status === 409 || r.status === 413 || r.status === 400 || r.status === 404) {
    c.turns = c.turns.filter((x) => x !== turn); S.drafts[`i:${c.item}`] = turn.text; P.saveDrafts();
    toast(r.data.message || "That message cannot be sent.");
  } else if (r.status !== 503) {
    turn.status = "unsent";
  }
  P.render();
}
function resend(turn) { turn.status = "sending"; P.render(); deliver(turn, S.conv); }
async function retry(turnId) {
  const r = await api("POST", P.itemUrl(S.conv.item, "/retry"), { turn_id: turnId });
  if (r.status !== 202) toast({ cap: "Too many retries today.", not_retryable: "Already answered or retrying.", busy: "The agent is busy; try again shortly." }[r.data.error] || "Could not retry.");
}
async function autoRetry(turnId, on) {
  const r = await api("POST", P.itemUrl(S.conv.item, "/autoretry"), { turn_id: turnId, on });
  if (r.status !== 200) { toast("Automatic retry is not possible any more."); P.loadConv(); }
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

window.opsViews = { history, system, ask, convo, card, turnEls };
})();
