"""What a proposed config file may contain: a fail-closed allowlist over the parsed YAML.

Line rules (`changes.check_content`) are only a pre-filter; this is the check.
The whole file is walked, not just the added lines:

- No anchors, aliases or merge keys (found in the YAML event stream).
- Each file kind has a fixed shape: an automation (`automations/**`), a list
  of template blocks (`templates/*`), or a package with `alert:` and
  `template:` only (`alerts/*`). Unknown keys are refused.
- Every action step is one of the known step types (call, condition,
  choose, if, repeat, parallel, sequence, delay, wait_template,
  wait_for_trigger, variables, stop), recursed into; anything else
  (`event:`, `scene:`, device actions, `use_blueprint`) is refused.
- Every call names, as a literal string, an action in `ALLOWED_DOMAINS` /
  `FIXED_ACTIONS` or the admin's notify services. Its targets are literal
  entity ids only (in `target:`, `entity_id:` or `data.entity_id`): no
  templates, `all`/`none`, areas, devices, floors or labels; each must
  exist in the entity registry and belong to the action's domain.
- `device_id`, `area_id`, `floor_id`, `label_id`, `event`, `scene`,
  `service_template` and `data_template` are refused anywhere.
- Alert `notifiers` are only the admin's.
"""

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

ALLOWED_DOMAINS = frozenset({
    "light", "fan", "climate", "media_player", "input_boolean", "input_number", "input_select", "input_text",
    "input_datetime", "input_button", "counter", "timer", "persistent_notification",
})  # fmt: skip
FIXED_ACTIONS = frozenset({"logbook.log", "system_log.write"})
ADMIN_GROUP = "critical_alerts"
# Actions that take no entity target at all.
UNTARGETED = frozenset({"notify", "persistent_notification", "logbook", "system_log"})
STEP_TYPES = ("action", "service", "condition", "choose", "if", "repeat", "parallel", "sequence", "delay",
              "wait_template", "wait_for_trigger", "variables", "stop")  # fmt: skip
FORBIDDEN_KEYS = frozenset({"device_id", "area_id", "floor_id", "label_id", "event", "scene", "service_template",
                            "data_template", "use_blueprint"})  # fmt: skip
AUTOMATION_KEYS = frozenset({"id", "alias", "description", "mode", "max", "max_exceeded", "initial_state", "trace",
                             "variables", "trigger_variables", "trigger", "triggers", "condition", "conditions",
                             "action", "actions"})  # fmt: skip
# No `image` or `weather` templates: they make the admin's browser fetch a URL.
TEMPLATE_BLOCK_KEYS = frozenset({"trigger", "triggers", "condition", "conditions", "action", "actions", "variables",
                                 "unique_id", "sensor", "binary_sensor", "button", "number", "select", "light",
                                 "fan"})  # fmt: skip
TEMPLATE_ENTITY_DOMAINS = frozenset({"sensor", "binary_sensor", "button", "number", "select", "light", "fan"})
# Keys that point a browser at something (a picture or a URL), refused anywhere in a template entity.
_FETCH_KEY_RE = re.compile(r"(^|_)(url|picture|image)(_|$)")
# climate.set_temperature: literal setpoints in a sane band, in either unit (no template, no extremes).
SETPOINT_KEYS = ("temperature", "target_temp_high", "target_temp_low")
SETPOINT_RANGES = ((10.0, 30.0), (50.0, 86.0))
# Template entity keys that hold action sequences.
TEMPLATE_ACTION_KEYS = frozenset({
    "turn_on", "turn_off", "press", "set_value", "select_option", "set_level", "set_temperature", "set_rgb",
    "set_hs", "set_effect", "set_percentage", "set_preset_mode", "set_oscillating", "set_direction", "action",
    "actions",
})  # fmt: skip
PACKAGE_KEYS = frozenset({"alert", "template"})
ALERT_KEYS = frozenset({"name", "done_message", "entity_id", "state", "repeat", "can_acknowledge", "skip_first",
                        "message", "title", "notifiers", "data"})  # fmt: skip
_ENTITY_RE = re.compile(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+")
_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+")
MAX_NODES = 20_000
# Media players: volume and transport only (no URLs, sources or groups to steer).
MEDIA_ACTIONS = frozenset({"media_player.volume_set", "media_player.volume_mute", "media_player.volume_up",
                           "media_player.volume_down", "media_player.media_play", "media_player.media_pause",
                           "media_player.media_stop", "media_player.turn_on", "media_player.turn_off"})  # fmt: skip
# Helpers an existing automation may react to: driving one is driving that automation.
HELPER_DOMAINS = frozenset({"input_boolean", "input_button", "input_number", "input_select", "input_text",
                            "input_datetime", "counter", "timer"})  # fmt: skip


NOTIFY_KEYS = frozenset({"message", "title", "data"})
PUSH_KEYS = frozenset({"tag", "group", "push", "actions"})


def _literal(value: Any) -> bool:
    return isinstance(value, str) and not _templated(value)


def push_data(data: Any) -> str | None:
    """Companion push data, shared by notify calls and alerts: a fail-closed allowlist, or why not.

    Anything that makes the phone fetch or open something (image, video, attachment, url,
    clickAction, a URI with a scheme or host, templated values) is refused; an in-app link
    (`action: URI` to a path such as `/history?...`) is allowed.
    """
    if data is None:
        return None
    if not isinstance(data, dict):
        return "push data must be a mapping"
    unknown = set(map(str, data)) - PUSH_KEYS
    if unknown:
        return f"push data may hold {sorted(PUSH_KEYS)} only (found {sorted(unknown)})"
    for key in ("tag", "group"):
        if key in data and not _literal(data[key]):
            return f"push {key} must be literal text"
    push = data.get("push")
    if push is not None and (not isinstance(push, dict) or set(map(str, push)) != {"interruption-level"}
                             or not _literal(push["interruption-level"])):  # fmt: skip
        return "push: may hold a literal interruption-level only"
    actions = data.get("actions")
    if actions is None:
        return None
    if not isinstance(actions, list):
        return "push actions must be a list"
    for item in actions:
        if not isinstance(item, dict) or set(map(str, item)) != {"action", "title", "uri"}:
            return "push actions must be {action: URI, title, uri} links into the app"
        uri = item["uri"]
        if item["action"] != "URI" or not _literal(item["title"]) or not _literal(uri):
            return "push actions must be literal URI links (action: URI, a title and a path)"
        if not uri.startswith("/") or uri.startswith("//") or "\\" in uri or ":" in uri.split("?", 1)[0]:
            return f"push links must be paths inside Home Assistant like /history?... (found {uri[:60]!r})"
    return None


def _fetch_key(value: Any) -> str | None:
    """The first mapping key at any depth that names a picture or URL (`entity_picture`, `url`, ...)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and _FETCH_KEY_RE.search(key):
                    return key
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _has_template(value: Any) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and _templated(item):
            return True
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


class ConfigRefused(ValueError):
    """The file is outside the allowlist; the message is safe to show the model and the admin."""


@dataclass(slots=True)
class Calls:
    """The actions a file calls and the entities they target (for the admin's summary)."""

    actions: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)


class _Strict(yaml.SafeLoader):
    """SafeLoader for proposals: `parse` refuses every explicit tag before this ever constructs one."""


class _Loader(yaml.SafeLoader):
    """Lenient reader for existing files only (summaries, helper watchers): HA's own tags as plain values."""


def _any_tag(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_Loader.add_multi_constructor("!", _any_tag)


class _Opaque(yaml.SafeLoader):
    """For comparing two versions of a file: a tag stays an opaque `(tag, value)` pair, never resolved."""


def _opaque_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    return (f"!{suffix}", _any_tag(loader, suffix, node))


_Opaque.add_multi_constructor("!", _opaque_tag)


def parse_opaque(text: str) -> Any:
    """Parse any HA YAML for comparison: tags kept opaque (`!include x` -> `("!include", "x")`), aliases
    resolved, duplicate keys refused (a later duplicate would silently replace the earlier block)."""
    try:
        _unique_keys(yaml.compose(text, Loader=_Opaque))
        return yaml.load(text, Loader=_Opaque)  # noqa: S506 - a SafeLoader subclass
    except (yaml.YAMLError, RecursionError, ValueError) as err:
        if isinstance(err, ConfigRefused):
            raise
        raise ConfigRefused(f"the YAML does not parse: {str(err)[:200]}") from err


def parse(text: str) -> Any:
    """Parse plain YAML only: one implicit document, no directives, tags, anchors, aliases, merge or duplicate keys.

    Tags are where HA reads secrets and the environment (`!secret`, `!env_var`, `!include`, and any
    `%TAG` shorthand for them), so a proposal may not carry one at all.
    """
    try:
        documents = 0
        for event in yaml.parse(text, Loader=_Strict):
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise ConfigRefused("one YAML document per file")
                if event.explicit or event.version or event.tags:
                    raise ConfigRefused("YAML directives (%YAML, %TAG) and document markers are not allowed")
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ConfigRefused("YAML anchors and aliases are not allowed")
            if isinstance(event, (yaml.ScalarEvent, yaml.SequenceStartEvent, yaml.MappingStartEvent)) and (
                event.tag is not None
            ):
                raise ConfigRefused("explicit YAML tags (!secret, !include, !input, !!str, ...) are not allowed")
            if isinstance(event, yaml.ScalarEvent) and event.value == "<<" and event.implicit[0]:
                raise ConfigRefused("YAML merge keys (<<) are not allowed")
        _unique_keys(yaml.compose(text, Loader=_Strict))
        return yaml.load(text, Loader=_Strict)  # noqa: S506 - a SafeLoader subclass
    except (yaml.YAMLError, RecursionError, ValueError) as err:
        if isinstance(err, ConfigRefused):
            raise
        raise ConfigRefused(f"the YAML does not parse: {str(err)[:200]}") from err


def _unique_keys(root: yaml.Node | None) -> None:
    stack = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode):
                    raise ConfigRefused("mapping keys must be plain scalars")
                if key.value in seen:
                    raise ConfigRefused(f"duplicate key {key.value[:60]!r}")
                seen.add(key.value)
                stack.append(value)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)


_BLOCK_RE = re.compile(r"^(\s*)(?:-\s+)*(?:[^#'\"]*?:\s*)?[|>][-+0-9]*\s*(?:#.*)?$")


def line_problem(text: str) -> str | None:
    """Pre-filter: no line starting with `%`, no `!` outside quoted or block scalars (tags, directives)."""
    block_indent: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        stripped = line.lstrip()
        if stripped.startswith("%"):
            return f"line {number}: YAML directives are not allowed"
        quote: str | None = None
        for index, char in enumerate(line):
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == "#" and (index == 0 or line[index - 1] in " \t"):
                break
            elif char == "!":
                return f"line {number}: `!` outside a quoted string (YAML tags are not allowed)"
        if _BLOCK_RE.match(line):
            block_indent = indent
    return None


def _templated(value: str) -> bool:
    return "{{" in value or "{%" in value or "{#" in value


class _Walker:
    def __init__(self, path: str, *, entities: frozenset[str] | None, admin_notify: str,
                 guarded: frozenset[str] | None = frozenset()) -> None:  # fmt: skip
        self.path = path
        self.entities = entities
        self.guarded = guarded
        self.admin_notify = admin_notify.removeprefix("notify.")
        self.calls = Calls()
        self.nodes = 0

    def refuse(self, why: str) -> ConfigRefused:
        return ConfigRefused(f"{self.path}: {why}")

    # ------------------------------------------------------------ anywhere

    def scan(self, value: Any) -> None:
        """Forbidden keys anywhere, and every call-shaped mapping checked wherever it sits."""
        stack = [value]
        while stack:
            self.nodes += 1
            if self.nodes > MAX_NODES:
                raise self.refuse("the file is too complex to check")
            item = stack.pop()
            if isinstance(item, dict):
                bad = FORBIDDEN_KEYS & {str(k) for k in item}
                if bad:
                    raise self.refuse(f"{', '.join(sorted(bad))} is not allowed (devices, areas, events, scenes,"
                                      " templated actions and blueprints are out of bounds)")  # fmt: skip
                # A push link item ({action: URI, title, uri}) is not a call; `push_data` checks it where
                # it sits, and as an action step it is still refused by `step`.
                link = item.get("action") == "URI" and set(map(str, item)) <= {"action", "title", "uri"}
                for key in ("action", "service"):
                    if isinstance(item.get(key), str) and not link:
                        self.call(item, item[key])
                # An event trigger's match data (`event_data: {action: released}`) is compared, never
                # executed; anywhere else an `event_data` value is scanned like everything else.
                trigger = item.get("platform") == "event" or item.get("trigger") == "event"
                stack.extend(v for k, v in item.items() if not (trigger and k == "event_data"))
            elif isinstance(item, list):
                stack.extend(item)

    # ------------------------------------------------------------- steps

    def steps(self, value: Any) -> None:
        if value is None:
            return
        for step in value if isinstance(value, list) else [value]:
            self.step(step)

    def step(self, step: Any) -> None:
        if not isinstance(step, dict):
            raise self.refuse("an action step must be a mapping")
        kind = next((k for k in STEP_TYPES if k in step), None)
        if kind is None:
            raise self.refuse(f"unknown action step {sorted(map(str, step))[:4]}; only plain service calls and"
                              " control flow are allowed")  # fmt: skip
        if kind in ("action", "service"):
            if not isinstance(step[kind], str):
                raise self.refuse(f"`{kind}:` in a step must name one action")
            self.call(step, step[kind])
        elif kind == "choose":
            options = step["choose"]
            for option in options if isinstance(options, list) else [options]:
                if not isinstance(option, dict):
                    raise self.refuse("choose options must be mappings")
                self.steps(option.get("sequence"))
            self.steps(step.get("default"))
        elif kind == "if":
            self.steps(step.get("then"))
            self.steps(step.get("else"))
        elif kind == "repeat":
            if not isinstance(step["repeat"], dict):
                raise self.refuse("repeat must be a mapping")
            self.steps(step["repeat"].get("sequence"))
        elif kind == "parallel":
            branches = step["parallel"]
            for branch in branches if isinstance(branches, list) else [branches]:
                if isinstance(branch, dict) and set(branch) <= {"sequence", "alias", "enabled"}:
                    self.steps(branch.get("sequence"))
                else:
                    self.step(branch)
        elif kind == "sequence":
            self.steps(step["sequence"])

    # -------------------------------------------------------------- calls

    def allowed_action(self, name: str) -> bool:
        domain = name.split(".", 1)[0]
        notify = {f"notify.{self.admin_notify}", f"notify.{ADMIN_GROUP}"}
        return domain in ALLOWED_DOMAINS or name in FIXED_ACTIONS or name in notify

    def call(self, step: dict[Any, Any], name: str) -> None:
        if _templated(name) or not _NAME_RE.fullmatch(name):
            raise self.refuse(f"actions must be literal names (found {name[:80]!r})")
        if not self.allowed_action(name):
            raise self.refuse(
                f"{name} is not an action the agent may propose (lights, fans, climate, media players, input"
                " helpers, counters, timers, notes and the admin's notifications)"
            )
        self.calls.actions.add(name)
        domain = name.split(".", 1)[0]
        data = step.get("data")
        if domain == "media_player":
            if name not in MEDIA_ACTIONS:
                raise self.refuse(f"{name} is not allowed (media players: {', '.join(sorted(MEDIA_ACTIONS))})")
            if _has_template(data):
                raise self.refuse(f"{name}: media player data cannot use templates")
        if domain == "notify":
            self.notify_data(name, data)
        if domain == "climate":
            self.climate_data(name, data)
        targets: list[Any] = []
        if "entity_id" in step:
            targets.append(step["entity_id"])
        target = step.get("target")
        if target is not None:
            if not isinstance(target, dict) or set(map(str, target)) - {"entity_id"}:
                raise self.refuse(f"{name}: targets must be explicit entity ids (no areas, devices, floors, labels)")
            targets.append(target.get("entity_id"))
        if data is not None and not isinstance(data, dict):
            raise self.refuse(f"{name}: data must be a mapping")
        if isinstance(data, dict) and "entity_id" in data:
            targets.append(data["entity_id"])
        for entity in self.entity_ids(name, targets):
            if domain in UNTARGETED:
                raise self.refuse(f"{name} takes no entity targets")
            if entity.split(".", 1)[0] != domain:
                raise self.refuse(f"{name} cannot target {entity} (another domain)")
            if self.entities is None:
                raise self.refuse("the entity registry is unavailable, so targets cannot be checked; try later")
            if entity not in self.entities:
                raise self.refuse(f"{entity} is not in the entity registry")
            if domain in HELPER_DOMAINS:
                if self.guarded is None:
                    raise self.refuse(f"{entity}: existing automations could not be read, so helper targets are"
                                      " refused")  # fmt: skip
                if entity in self.guarded:
                    raise self.refuse(f"{entity}: an existing automation that does more than the agent may (for"
                                      " example switches, covers or locks) reacts to it")  # fmt: skip
            self.calls.entities.add(entity)

    def climate_data(self, name: str, data: Any) -> None:
        """Climate data: literal values only; setpoints within 10-30 °C or 50-86 °F."""
        if _has_template(data):
            raise self.refuse(f"{name}: climate data cannot use templates")
        if name != "climate.set_temperature" or not isinstance(data, dict):
            return
        for key in SETPOINT_KEYS:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise self.refuse(f"{name}: {key} must be a literal number")
            if not any(low <= value <= high for low, high in SETPOINT_RANGES):
                raise self.refuse(f"{name}: {key} {value} is outside 10-30 °C / 50-86 °F")

    def notify_data(self, name: str, data: Any) -> None:
        """A notify call: message and title, plus push data from the shared allowlist (`push_data`)."""
        if data is None:
            return
        if not isinstance(data, dict) or set(map(str, data)) - NOTIFY_KEYS:
            raise self.refuse(f"{name}: notify data may hold {sorted(NOTIFY_KEYS)} only")
        problem = push_data(data.get("data"))
        if problem is not None:
            raise self.refuse(f"{name}: {problem}")

    def entity_ids(self, name: str, targets: Iterable[Any]) -> list[str]:
        found: list[str] = []
        for target in targets:
            if target is None:
                continue
            for entity in target if isinstance(target, list) else [target]:
                if not isinstance(entity, str) or _templated(entity) or entity in ("all", "none"):
                    raise self.refuse(f"{name}: entity ids must be literal (found {str(entity)[:60]!r})")
                if not _ENTITY_RE.fullmatch(entity):
                    raise self.refuse(f"{name}: {entity[:60]!r} is not an entity id")
                found.append(entity)
        return found

    # -------------------------------------------------------------- files

    def automation(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise self.refuse("an automation file holds one automation (a mapping)")
        unknown = set(map(str, data)) - AUTOMATION_KEYS
        if unknown:
            raise self.refuse(f"unknown automation keys {sorted(unknown)}")
        self.steps(data.get("action"))
        self.steps(data.get("actions"))

    def template_blocks(self, data: Any) -> None:
        if not isinstance(data, list):
            raise self.refuse("a template file is a list of template blocks")
        for block in data:
            if not isinstance(block, dict):
                raise self.refuse("template blocks must be mappings")
            unknown = set(map(str, block)) - TEMPLATE_BLOCK_KEYS
            if unknown:
                raise self.refuse(f"template blocks may define {sorted(TEMPLATE_ENTITY_DOMAINS)} only (found"
                                  f" {sorted(unknown)})")  # fmt: skip
            self.steps(block.get("action"))
            self.steps(block.get("actions"))
            for domain in TEMPLATE_ENTITY_DOMAINS & set(map(str, block)):
                items = block[domain]
                for item in items if isinstance(items, list) else [items]:
                    if not isinstance(item, dict):
                        raise self.refuse(f"{domain} templates must be mappings")
                    fetch = _fetch_key(item)
                    if fetch is not None:
                        raise self.refuse(f"{domain} templates cannot set {fetch!r} (pictures and URLs are refused)")
                    for key in TEMPLATE_ACTION_KEYS & set(map(str, item)):
                        self.steps(item[key])

    def package(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise self.refuse("an alerts file is a package mapping")
        unknown = set(map(str, data)) - PACKAGE_KEYS
        if unknown:
            raise self.refuse(f"an alerts package may hold alert: and template: only (found {sorted(unknown)})")
        if data.get("template") is not None:
            self.template_blocks(data["template"])
        alerts = data.get("alert") or {}
        if not isinstance(alerts, dict):
            raise self.refuse("alert: must be a mapping of alerts")
        allowed = {self.admin_notify, ADMIN_GROUP}
        for name, alert in alerts.items():
            if not isinstance(alert, dict):
                raise self.refuse(f"alert {name} must be a mapping")
            unknown = set(map(str, alert)) - ALERT_KEYS
            if unknown:
                raise self.refuse(f"alert {name}: unknown keys {sorted(unknown)}")
            problem = push_data(alert.get("data"))
            if problem is not None:
                raise self.refuse(f"alert {name}: {problem}")
            notifiers = alert.get("notifiers") or []
            for notifier in notifiers if isinstance(notifiers, list) else [notifiers]:
                if not isinstance(notifier, str) or notifier not in allowed:
                    raise self.refuse(f"alert notifiers must be {sorted(allowed)} (found {str(notifier)[:40]!r})")
                self.calls.actions.add(f"notify.{notifier}")


def check_file(path: str, text: str, *, entities: frozenset[str] | None, admin_notify: str,
               guarded: frozenset[str] | None = frozenset()) -> Calls:  # fmt: skip
    """Refuse anything outside the allowlist; return what the file calls.

    `guarded`: helper entities that existing automations which do more than the allowlist react to
    (None: they could not all be read, so every helper target is refused).
    """
    problem = line_problem(text)
    if problem is not None:
        raise ConfigRefused(f"{path}: {problem}")
    data = parse(text)
    walker = _Walker(path, entities=entities, admin_notify=admin_notify, guarded=guarded)
    walker.scan(data)
    top = path.split("/", 1)[0]
    if top == "automations":
        walker.automation(data)
    elif top == "templates":
        walker.template_blocks(data)
    elif top == "alerts":
        walker.package(data)
    else:
        raise walker.refuse("not a config file the agent may change")
    return walker.calls


_HELPER_ID_RE = re.compile(rf"\b(?:{'|'.join(sorted(HELPER_DOMAINS))})\.[a-z0-9_]+")


def guarded_helpers(texts: Iterable[str | None], *, admin_notify: str) -> frozenset[str] | None:
    """Helpers referenced by existing automations that call anything outside the allowlist (None if unreadable)."""
    checker = _Walker("", entities=None, admin_notify=admin_notify)
    guarded: set[str] = set()
    for text in texts:
        if text is None:
            return None
        try:
            yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass
        except yaml.YAMLError, RecursionError, ValueError:
            return None
        calls = collect(text)
        if any(not checker.allowed_action(a) for a in calls.actions):
            guarded.update(_HELPER_ID_RE.findall(text))
    return frozenset(guarded)


def collect(text: str | None) -> Calls:
    """Best effort, never refusing: what an existing file calls (for the before/after summary)."""
    calls = Calls()
    if not text:
        return calls
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError, RecursionError, ValueError:
        return calls
    stack = [data]
    seen = 0
    while stack and seen < MAX_NODES:
        seen += 1
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("action", "service"):
                if isinstance(item.get(key), str):
                    calls.actions.add(item[key])
                    for source in (item.get("entity_id"), (item.get("target") or {}) if isinstance(
                            item.get("target"), dict) else {}, item.get("data")):  # fmt: skip
                        value = source.get("entity_id") if isinstance(source, dict) else source
                        for entity in value if isinstance(value, list) else [value]:
                            if isinstance(entity, str):
                                calls.entities.add(entity)
            notifiers = item.get("notifiers")
            if isinstance(notifiers, list):
                calls.actions.update(f"notify.{n}" for n in notifiers if isinstance(n, str))
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return calls


# ----------------------------------------------------- configuration.yaml

# Blocks of configuration.yaml an update fix never edits: access, exposure, the frontend, anything
# that runs commands or reaches out, the database, and what opens the house.
CONFIGURATION_DENY = frozenset({
    "homeassistant", "http", "frontend", "cloud", "lovelace", "panel_custom", "panel_iframe", "shell_command",
    "command_line", "rest_command", "rest", "python_script", "pyscript", "recorder", "logger", "automation",
    "homekit", "alexa", "google_assistant", "lock", "alarm_control_panel", "cover", "mqtt", "default_config",
    "api", "auth", "conversation", "intent_script", "notify", "script", "scene", "group", "person", "zone",
    "webhook", "websocket_api",
})  # fmt: skip
CONFIGURATION_MAX_LINES = 40
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")
_RISKY_RE = re.compile(
    r"://|\b(?:api_key|token|password|secret|ssl_\w+|keyfile|certfile|url|host|ip_address|port|username)\b"
    r"|!\w+|&\w+|\*\w+",
    re.IGNORECASE,
)


def _blocks(lines: list[str]) -> list[str | None]:
    """The top-level key each line belongs to (None before the first key)."""
    owner: list[str | None] = []
    current: str | None = None
    for line in lines:
        found = _TOP_KEY_RE.match(line)
        if found:
            current = found.group(1)
        owner.append(current)
    return owner


def check_configuration_edit(old: str, new: str, *, domains: frozenset[str]) -> None:
    """An update fix's edit of configuration.yaml: in place, inside the blocks of `domains` only.

    The top-level keys stay exactly the same (no block added, removed or renamed); every changed
    line lies inside a block named by one of `domains` and outside `CONFIGURATION_DENY`; at most
    `CONFIGURATION_MAX_LINES` lines change; added lines carry no URL, credential, host, YAML tag,
    anchor or alias; the result has no duplicate keys at the top level.
    """
    old_lines, new_lines = old.splitlines(), new.splitlines()
    old_keys = [m.group(1) for line in old_lines if (m := _TOP_KEY_RE.match(line))]
    new_keys = [m.group(1) for line in new_lines if (m := _TOP_KEY_RE.match(line))]
    if old_keys != new_keys:
        raise ConfigRefused("configuration.yaml: a fix may not add, remove or rename top-level keys")
    if len(set(new_keys)) != len(new_keys):
        raise ConfigRefused("configuration.yaml: duplicate top-level keys")
    owners = _blocks(old_lines)
    changed = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += max(i2 - i1, j2 - j1)
        span = owners[i1:i2] if i2 > i1 else owners[max(0, i1 - 1) : i1]
        for block in set(span) or {None}:
            if block is None or block in CONFIGURATION_DENY or block not in domains:
                raise ConfigRefused(f"configuration.yaml: a fix for {', '.join(sorted(domains)) or '?'} may not "
                                    f"change the {block or 'top'} block")  # fmt: skip
        for line in new_lines[j1:j2]:
            if _TOP_KEY_RE.match(line):
                raise ConfigRefused("configuration.yaml: a fix may not change a top-level key line")
            if _RISKY_RE.search(line):
                raise ConfigRefused(
                    f"configuration.yaml: the fix adds a line that is not allowed ({line.strip()[:60]!r})"
                )
    if changed == 0:
        raise ConfigRefused("configuration.yaml: the fix changes nothing")
    if changed > CONFIGURATION_MAX_LINES:
        raise ConfigRefused(f"configuration.yaml: a fix changes at most {CONFIGURATION_MAX_LINES} lines")
    # The line view above can be fooled (a quoted key, a flow mapping): the parsed files decide.
    before, after = parse_opaque(old), parse_opaque(new)
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ConfigRefused("configuration.yaml: not a mapping")
    if list(before) != list(after):
        raise ConfigRefused("configuration.yaml: the parsed top-level keys changed")
    outside = [k for k in before if (k not in domains or k in CONFIGURATION_DENY) and before[k] != after[k]]
    if outside:
        raise ConfigRefused(f"configuration.yaml: a fix may not change the {str(outside[0])[:40]} block")
    if before == after:
        raise ConfigRefused("configuration.yaml: the fix changes nothing once parsed")
