"""The panel's front end: CSP-compatible, text-only rendering of daemon and model text, size, and the item views.

The contract it renders is docs/panel-api.md. The renderers run under node against a minimal DOM that records how
every node was made, so a string from the API can only ever become a text node.
"""

import gzip
import json
import re
import shutil
import subprocess
from typing import Any

import pytest

from tests.conftest import OPT

SHELL = (OPT / "panel" / "index.html").read_text()
CHANGES = (OPT / "panel" / "changes.js").read_text()
SCRIPT = re.search(r'<script nonce="\{\{NONCE\}\}">(.*)</script>', SHELL, re.S)
BODY = SCRIPT.group(1) if SCRIPT else ""
EVIL = "<img src=x onerror=alert(1)>"
STATES = ("proposed", "waiting", "scheduled", "running", "done", "failed", "cancelled", "expired")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_shell_is_csp_compatible() -> None:
    assert SCRIPT is not None
    # Only the two nonce'd blocks; no external resources, inline handlers or style attributes.
    assert SHELL.count("<script") == 1 and SHELL.count("<style") == 1
    assert 'style nonce="{{NONCE}}"' in SHELL
    assert not re.search(r"<[a-z]+[^>]*\son[a-z]+=", SHELL)
    assert not re.search(r"<[a-z]+[^>]*\sstyle=", SHELL)
    assert not re.search(r"(src|href)=\"(https?:)?//", SHELL)
    for text in (SHELL, CHANGES):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     'setAttribute("style"', "srcdoc"):  # fmt: skip
            assert sink not in text, sink
    assert "<script" not in CHANGES


def test_shell_size_budget() -> None:
    raw = SHELL.encode()
    # Over the ~30 KB design target (no build step, no minifier); this guards against growth.
    assert len(raw) < 70_000 and len(gzip.compress(raw)) < 20_000


def test_changes_script_is_loaded_under_the_nonce_and_in_budget() -> None:
    # The shell loads it itself, with its own nonce; no second <script> tag in the page.
    assert "document.currentScript.nonce" in SHELL and 'h("script", { src: "changes.js"' in SHELL
    assert "s.nonce = NONCE" in SHELL
    # History, System and the chat renderer: budgeted together.
    assert len(gzip.compress(CHANGES.encode())) < 12_000


# A minimal DOM: enough for the renderers, and it records how nodes were made.
_DOM = r"""
class Node { constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this._text = ""; this.style = {};
    this.classList = { toggle() {} }; }
  append(...k) { this.kids.push(...k); } replaceChildren(...k) { this.kids = k; }
  setAttribute(k, v) { this.attrs[k] = v; } addEventListener(k, f) { (this.on = this.on || {})[k] = f; }
  set textContent(v) { this._text = String(v); this.kids = []; }
  get textContent() { return this._text + this.kids.map((k) => k.textContent).join(""); }
  set className(v) { this.attrs.class = v; } get isConnected() { return true; } }
class Text { constructor(t) { this.tag = "#text"; this.text = t; } get textContent() { return this.text; } }
const document = { createElement: (t) => new Node(t), createTextNode: (t) => new Text(t),
  createDocumentFragment: () => new Node("#frag"), createElementNS: (_, t) => new Node(t) };
const dump = (n) => n.tag === "#text" ? { t: n.text }
  : { tag: n.tag, attrs: n.attrs, text: n._text, kids: n.kids.map(dump) };
setInterval = () => 0; setTimeout = () => 0;
Date.now = () => Date.parse("2026-01-30T12:00:00Z");
"""


def _funcs(*names: str) -> str:
    return "".join(re.search(rf"function {name}\(.*?\n}}\n", BODY, re.S).group(0) for name in names)  # type: ignore[union-attr]


def _between(start: str, end: str) -> str:
    return BODY[BODY.index(start) : BODY.index(end)]


# The shell's own helpers and renderers, with the network and navigation stubbed out.
_RENDER = (
    _funcs("h", "icon", "rich", "inline")
    + re.search(r"const P = \{.*?\};\n", BODY, re.S).group(0)  # type: ignore[union-attr]
    + re.search(r"const NS = .*?;\n", BODY).group(0)  # type: ignore[union-attr]
    + re.search(r"const btn = .*?;\n", BODY).group(0)  # type: ignore[union-attr]
    + _between("const pad = ", "async function api(")
    + "const SECTIONS = ['needs', 'running', 'coming', 'today'];\n"
    + _between("const GLYPH = ", "/* A share in percent:")
)
_STUBS = (
    "const S = { state: { tz: 'Europe/London' }, home: null, hist: null, items: {}, open: {}, note: {} };"
    " const els = {}; const wide = { matches: false }; const calls = [];"
    " function go(x) { calls.push(['go', x]); } function ask(x) { calls.push(['ask', x]); }"
    " function act(t, a) { calls.push(['act', a.id]); }"
    " function loadItem() {} function render() {}"
    " function share(v) { return `${v}%`; } function fmtN(n) { return String(n); }\n"
)


def _run(program: str) -> Any:
    out = subprocess.run(
        ["node", "-e", _DOM + _STUBS + program], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(out.stdout)


def _walk(tree: dict[str, Any], texts: list[str] | None = None) -> list[str]:
    """All text in render order; fails on any element an attacker could use, or any HTML-bearing attribute."""
    texts = [] if texts is None else texts
    if "t" in tree:
        texts.append(str(tree["t"]))
        return texts
    assert tree["tag"] not in ("img", "script", "a", "iframe", "style"), tree["tag"]
    assert not {k for k in tree["attrs"] if k.startswith("on") or k in ("href", "src", "style", "srcdoc")}, tree[
        "attrs"
    ]
    texts.append(str(tree["text"]))
    for kid in tree["kids"]:
        _walk(kid, texts)
    return texts


def _text(tree: dict[str, Any]) -> str:
    return "".join(_walk(tree))


def _find(tree: dict[str, Any], pred: Any) -> list[dict[str, Any]]:
    found = [tree] if "t" not in tree and pred(tree) else []
    for kid in tree.get("kids", []):
        found += _find(kid, pred)
    return found


def _item(**over: Any) -> dict[str, Any]:
    base = {
        "id": "u1", "key": "updates:x", "kind": "update", "kicker": EVIL, "ref": EVIL, "title": EVIL,
        "state": "waiting", "gist": EVIL, "at": "2026-01-30T04:12:00Z", "as_of": "2026-01-30T09:41:00Z", "revision": 3,
        "need": {"kind": "approval", "text": EVIL, "primary": {"id": "reboot_now", "label": EVIL},
                 "secondary": [{"id": "later", "label": EVIL, "style": "text"}]},
        "phase": {"names": [EVIL, "Install"], "current": 1},
        "follows": {"id": "u0", "title": EVIL, "state": "done"},
        "facts": [{"label": EVIL, "value": EVIL, "at": "2026-01-30T09:41:00Z"}],
        "agent": {"text": f"{EVIL}\n\n- `{EVIL}`", "sources": [EVIL], "not_read": [EVIL]},
        "change": {"diff": f"+{EVIL}\n-{EVIL}", "files": [EVIL]},
        "footnote": EVIL,
        "events": [{"at": "2026-01-26T04:00:00Z", "actor": "daemon", "text": EVIL},
                   {"at": "2026-01-30T04:07:00Z", "actor": "you", "text": EVIL},
                   {"at": "2026-01-30T04:08:00Z", "actor": "agent", "text": EVIL}],
    }  # fmt: skip
    return base | over


@needs_node
def test_model_output_renders_as_text_only() -> None:
    evil = "<script>alert(1)</script>\n\n- <img src=x onerror=alert(2)>\n- `<a href=javascript:alert(3)>x</a>`\n\nok"
    tree = _run(_funcs("h", "rich", "inline") + f"console.log(JSON.stringify(dump(rich({json.dumps(evil)}))));")
    tags = {n["tag"] for n in _find(tree, lambda n: True)}
    assert tags <= {"#frag", "p", "ul", "li", "code", "br"}
    joined = _text(tree)
    assert "<script>alert(1)</script>" in joined and "<img src=x onerror=alert(2)>" in joined
    assert "<a href=javascript:alert(3)>x</a>" in joined


@needs_node
def test_item_detail_renders_every_field_as_text() -> None:
    """Title, facts, the agent's words, the diff, the timeline: daemon and model text, never markup."""
    tree = _run(_RENDER + f"console.log(JSON.stringify(dump(detail({json.dumps(_item())}))));")
    joined = _text(tree)
    assert joined.count(EVIL) >= 16
    order = [joined.index(x) for x in ("Needs you", "What the daemon knows", "The agent says", "What happened")]
    assert order == sorted(order)
    assert f"Sources: {EVIL}" in joined and f"Not read: {EVIL}" in joined and f"Follows: {EVIL} · Done" in joined
    # The fixed vocabulary is the panel's; who did what is tagged on every timeline entry.
    assert [_text(n) for n in _find(tree, lambda n: "who" in str(n["attrs"].get("class", "")))] == [
        "Daemon", "You", "Agent"]  # fmt: skip
    # One filled button.
    fills = _find(tree, lambda n: n["tag"] == "button" and n["attrs"].get("class") == "btn fill")
    assert len(fills) == 1 and _text(fills[0]) == EVIL
    # Times are the house's: 04:12 UTC is 04:12 in London (GMT); "as of" shows the clock time today.
    assert "as of" in joined


@needs_node
def test_unknown_state_is_never_shown_as_a_known_one() -> None:
    words = _run(
        _RENDER + "console.log(JSON.stringify(['waiting','failed','running','scheduled','done','proposed','cancelled',"
        "'expired','bogus'].map((state) => word({ state }))));"
    )
    assert words == ["Needs you", "Failed", "Running", "Scheduled", "Done", "Proposed", "Cancelled", "Expired",
                     "Unknown state"]  # fmt: skip


@needs_node
def test_home_sections_come_from_the_daemon_in_order() -> None:
    row = {"kind": "check", "title": EVIL, "gist": EVIL, "at": "2026-01-30T05:30:00Z", "revision": 1}
    home: dict[str, Any] = {"as_of": "2026-01-30T09:41:00Z", "needs_count": 1, "calm": None,
            "sections": {"needs": [row | {"id": "a", "state": "waiting"}],
                         "running": [row | {"id": "b", "state": "running"}],
                         "coming": [row | {"id": "c", "state": "scheduled"}],
                         "today": [row | {"id": "d", "state": "done"}]},
            "strip": [{"label": "Health", "value": EVIL}]}  # fmt: skip
    quiet = home | {"needs_count": 0, "calm": EVIL, "sections": {**home["sections"], "needs": []}}
    program = _RENDER + (
        f"S.home = {json.dumps(home)}; const busy = h('div', null, homeList('b'));"
        f"S.home = {json.dumps(quiet)}; const calm = h('div', null, homeList(null));"
        "console.log(JSON.stringify([dump(busy), dump(calm)]));"
    )
    busy, calm = _run(program)
    joined = _text(busy)
    order = [joined.index(x) for x in ("Needs you1", "In progress", "Coming up", "Today", "Health")]
    assert order == sorted(order) and joined.count(EVIL) == 9
    assert [n["attrs"].get("aria-current") for n in _find(busy, lambda n: n["attrs"].get("class") == "row")] == [
        None, "true", None, None]  # fmt: skip
    assert "All quiet" in _text(calm) and EVIL in _text(calm) and "Needs you" not in _text(calm)


def _views(state: str = "{}") -> str:
    """changes.js on the shell's real helpers."""
    exports = (
        "const window = { opsPanel: { h, btn, api: async () => ({ status: 0, data: {} }), toast() {},"
        " render() {}, S, els, go, row, section, icon, share, fmtN, hhmm, dayKey, dayLabel, when, tz: () => 'UTC',"
        " getJSON() {}, loadHistory() {}, loadSystem() {}, loadState() {}, ask, wide, rich, announce() {},"
        " saveDrafts() {}, clock, loadThread() {}, loadHome() {}, markRead() {} } };\n"
    )
    return _RENDER + f"Object.assign(S, {state});\n" + exports + CHANGES + "\nconst V = window.opsViews;\n"


@needs_node
def test_history_groups_by_day_and_folds_routine_entries() -> None:
    item = {"kind": "check", "state": "done", "gist": EVIL, "revision": 1}
    hist = {"filter": "all", "more": True, "cursor": "x", "counts": {"needs": 2}, "items": [
        item | {"id": "a", "title": EVIL, "at": "2026-01-30T09:00:00Z", "state": "waiting"},
        item | {"id": "q1", "title": "daily check", "at": "2026-01-30T05:30:00Z", "quiet": True},
        item | {"id": "q2", "title": "update plan", "at": "2026-01-30T05:29:00Z", "quiet": True},
        item | {"id": "b", "title": EVIL, "at": "2026-01-28T03:10:00Z", "state": "failed"},
    ]}  # fmt: skip
    tree = _run(_views(json.dumps({"hist": hist})) + "console.log(JSON.stringify(dump(h('div', null, V.history()))));")
    joined = _text(tree)
    assert "Needs you 2" in joined and "2 routine entries · daily check, update plan" in joined
    assert "28 January" in joined and joined.count(EVIL) == 4 and "Older" in joined
    # Folded: the routine entries are one line until it is opened.
    assert joined.count("daily check") == 1


@needs_node
def test_chat_cards_are_text_with_fixed_actions() -> None:
    cards = [
        {"status": "proposal", "text": f"{EVIL} v1 → v2", "meta": {"actions": ["updates"]}},
        {"status": "investigation", "text": f"{EVIL}. Step 1 of 2.", "meta": {"task": 7, "actions": ["cancel"]}},
        {"status": "investigation", "text": "Done.", "meta": {"task": 7, "actions": [], "done": True}},
    ]  # fmt: skip
    proposal, running, done = _run(
        _views("{thread: 'abc'}") + f"console.log(JSON.stringify({json.dumps(cards)}.map((t) => dump(V.card(t)))));"
    )
    assert EVIL in _text(proposal) and "Review on Home" in _text(proposal)
    assert "Cancel" in _text(running) and "Review on Home" not in _text(running)
    assert "Cancel" not in _text(done) and "done" in done["attrs"]["class"]


@needs_node
def test_item_ids_cannot_steer_requests_to_other_routes() -> None:
    """An id is only ever the contract's [A-Za-z0-9_-]{1,64}: "../backups/now#" must not become a POST elsewhere."""
    code = re.search(r"const ID_RE = .*?\nconst itemUrl = .*?;\n", BODY, re.S)
    assert code is not None
    for name in ("loadItem", "act"):
        assert "itemUrl(" in _funcs(name), name
    program = code.group(0) + (
        "console.log(JSON.stringify(['u-7f3a', '../backups/now#', 'a/b', 'x'.repeat(65), '', null]"
        ".map((id) => itemUrl(id, '/act'))));"
    )
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout) == ["api/items/u-7f3a/act", None, None, None, None, None]


@needs_node
def test_times_are_shown_in_the_house_time_zone() -> None:
    now = "Date.parse('2026-01-30T16:41:00Z')"
    program = (
        _between("const pad = ", "const clock = ") + "console.log(JSON.stringify(["
        f"when('2026-01-30T11:12:00Z', 'Europe/Berlin', {now}),"
        f"when('2026-01-31T11:00:00Z', 'Europe/Berlin', {now}),"
        f"when('2026-01-26T11:00:00Z', 'Europe/Berlin', {now}),"
        f"dayLabel('2026-01-30T07:00:00Z', 'Europe/Berlin', {now}),"
        f"dayLabel('2026-01-29T08:00:00Z', 'Europe/Berlin', {now}),"
        f"dayLabel('2026-01-28T08:00:00Z', 'Europe/Berlin', {now}),"
        "stamp('2026-01-26T11:00:00Z', 'Europe/Berlin', true),"
        "hhmm('2026-01-30T11:12:00Z', 'Europe/London')]))"
    )
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout) == [
        "12:12", "12:00", "Mon 26", "Today", "Yesterday", "Wednesday 28 January", "Mon 26 · 12:00", "11:12",
    ]  # fmt: skip


def test_history_urls_are_absolute() -> None:
    """A relative URL in pushState/replaceState resolves against <base>, moves the page and breaks every request."""
    calls = re.findall(r'(?:State|\])\(null, "", ([^)]*)\)', SHELL)
    assert len(calls) >= 2 and all(c.startswith("`${location.pathname}") for c in calls)
    # Navigation never uses anchors: an href resolves against <base> too.
    assert 'h("a"' not in SHELL and 'h("a"' not in CHANGES
    # The one anchor is the diagnostics download, from an absolute blob: URL.
    assert 'createElement("a")' not in SHELL and CHANGES.count('createElement("a")') == 1
    assert "URL.createObjectURL(new Blob(" in CHANGES


def test_theme_tokens_follow_ha_and_the_os() -> None:
    assert ':root[data-theme="dark"]{' in SHELL and ':root[data-theme="light"]{' in SHELL
    assert ':root:not([data-theme="light"])' in SHELL
    # The accent's tint is computed from the (bridged) accent and the card, never a fixed colour of its own.
    assert ":root{--soft:color-mix(in srgb,var(--primary) 13%,var(--card))}" in SHELL
    # State words carry the tone of the state, and the glyph tint agrees with it.
    for tone in ("warn", "err", "ok"):
        assert f".row .t .st.{tone}{{color:var(--{tone})}}" in SHELL
        assert f".{tone}.g{{background:var(--{tone}-soft);color:var(--{tone})}}" in SHELL


def test_exported_bindings_are_declared_before_the_export() -> None:
    # A `let els` declared below `window.opsPanel = {..., els, ...}`: a temporal-dead-zone
    # ReferenceError at load stopped the whole panel at "Loading the Ops Agent…".
    lines = SHELL.splitlines()
    export = next(i for i, line in enumerate(lines) if line.startswith("window.opsPanel = {"))
    block = "\n".join(lines[export : export + 4]).split("{", 1)[1].split("}", 1)[0]
    for name in re.findall(r"\b([A-Za-z_]\w*)\b", block):
        declared = [i for i, line in enumerate(lines) if re.match(rf"(?:let|const) {name}\b", line)]
        assert all(i < export for i in declared), f"{name} is declared after window.opsPanel"


def test_accent_colours_never_derive_from_card_or_background_vars() -> None:
    """Tabs and outlined buttons took the card colour and vanished on HA's dark theme."""
    vars_line = re.search(r"const VARS = (\[.*?\]);\n", SHELL, re.S)
    assert vars_line is not None
    pairs = dict(re.findall(r'\["(--[\w-]+)", "(--[\w-]+)"\]', vars_line.group(1)))
    assert pairs["--primary"] == "--primary-color"
    for pattern in (r"\.nav button\[aria-current=\"page\"\]\{([^}]*)\}", r"\.btn\{([^}]*)\}",
                    r"\.btn\.fill\{([^}]*)\}"):  # fmt: skip
        rule = re.search(pattern, SHELL)
        assert rule is not None, pattern
        used = set(re.findall(r"var\((--[\w-]+)\)", rule.group(1)))
        assert used <= {"--primary", "--on-primary"} and "--primary" in used, (pattern, used)


@needs_node
def test_theme_contrast_guard_keeps_readable_accents_only() -> None:
    funcs = _funcs("lum", "contrast", "pick")
    light_token = re.search(r':root\[data-theme="light"\]\{[^}]*--primary:(#[0-9a-f]{6})', SHELL)
    assert light_token is not None
    hexed = light_token.group(1)
    builtin = f"rgb({int(hexed[1:3], 16)}, {int(hexed[3:5], 16)}, {int(hexed[5:7], 16)})"
    # HA's default dark and light themes, as theme() resolves them (rgb strings), and a theme that broke the tabs.
    ha_dark = {"--primary": "rgb(3, 169, 244)", "--card": "rgb(28, 28, 28)", "--fg": "rgb(225, 225, 225)",
               "--muted": "rgb(155, 155, 155)", "--bg": "rgb(17, 17, 17)"}  # fmt: skip
    ha_light = {"--primary": "rgb(3, 169, 244)", "--card": "rgb(255, 255, 255)", "--fg": "rgb(33, 33, 33)",
                "--muted": "rgb(114, 114, 114)", "--bg": "rgb(250, 250, 250)"}  # fmt: skip
    broken = {**ha_dark, "--primary": "rgb(43, 43, 43)", "--card": "rgb(43, 43, 43)"}
    program = funcs + (
        f"const r = [pick({json.dumps(ha_dark)}, true), pick({json.dumps(ha_light)}, false),"
        f" pick({json.dumps(broken)}, true), pick({{}}, true),"
        f" contrast({json.dumps(builtin)}, 'rgb(255, 255, 255)'), contrast('rgb(3, 169, 244)', 'rgb(28, 28, 28)')];"
        "console.log(JSON.stringify(r));"
    )
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True, timeout=30)
    dark, light, bad, empty, builtin_light, builtin_dark = json.loads(out.stdout)
    assert dark == ha_dark  # HA's own dark accent is readable on its card: kept
    assert "--primary" not in light and light["--fg"] == ha_light["--fg"]  # #03a9f4 on white is 2.6:1
    assert "--primary" not in bad and bad["--fg"] == ha_dark["--fg"]
    assert empty == {}
    assert builtin_light >= 4.5 and builtin_dark >= 3


def test_embedded_in_the_ha_app_the_bottom_inset_is_not_added_twice() -> None:
    """The HA app keeps the home indicator's inset outside the ingress iframe."""
    css = SHELL.split("<style", 1)[1].split("</style>", 1)[0]
    rules = dict(re.findall(r"(\.embedded \.[a-z]+)\{([^}]*)\}", css))
    assert rules[".embedded .nav"] == "padding-bottom:6px;background:var(--bg)"
    assert rules[".embedded .composer"] == "padding-bottom:10px" and rules[".embedded .toast"] == "bottom:84px"
    assert not any("env(" in body for body in rules.values())
    # Standalone keeps the inset; the bar stays a fixed-height flex item at the bottom.
    nav = re.search(r"\n\.nav\{([^}]*)\}", css)
    assert nav is not None and "env(safe-area-inset-bottom)" in nav.group(1) and "flex:none" in nav.group(1)
    # Detected first thing, before anything renders; a frame that cannot be inspected counts as embedded.
    start = BODY.index('"use strict";')
    detect = BODY[start : start + 400]
    assert "window.self !== window.top" in detect and 'classList.add("embedded")' in detect and "catch" in detect
    assert detect.count('classList.add("embedded")') == 2
