"""Effect capabilities Claude can call during a run: ack and notify.

These are the first socket verbs that change anything, so each one:
- works only inside an agent run (the validator refuses otherwise);
- carries the final-JSON clamps in its validator: acks only for keys
  handed to this run, at most 30 days, critical items snoozed for at most 3
  days and never accepted; `urgent` only when a hand-off item is critical;
- has per-run and per-day quotas in the registry (one push, one log
  entry per run), on top of the Notifier's daily push buckets;
- records what it did in the run's `RunEffects`, which the health handler
  applies after the run, under the cycle lock, exactly as it applies the
  final JSON.
Text is redacted before it leaves the daemon.
"""

from opsd.baseline import CRITICAL_ACK_MAX_DAYS, WARNING_SNOOZE_MAX_DAYS
from opsd.capabilities import Args, CapabilityError
from opsd.channels import agent_message
from opsd.runctx import Ack, ExecContext, RunContext

MAX_NOTIFY_CHARS = 900
MAX_OPSLOG_CHARS = 4000


def _run(ctx: ExecContext) -> RunContext:
    if ctx.run is None:
        raise CapabilityError(f"{ctx.capability.name} is only available inside an agent run")
    return ctx.run


def validate_ack(ctx: ExecContext, args: Args) -> Args:
    run = _run(ctx)
    key = args.opt_text("key")
    if not key or key not in run.handoff:
        raise CapabilityError("unknown item key: ack only the exact keys listed in this run's hand-off")
    days = max(1, min(30, args.number("days")))
    accept = args.flag("accept")
    if run.handoff[key].severity == "critical":
        if accept:
            ctx.notes.append("accept refused: only the admin can accept a critical item (snoozed instead)")
        accept = False
        if days > CRITICAL_ACK_MAX_DAYS:
            ctx.notes.append(f"critical items can be snoozed for at most {CRITICAL_ACK_MAX_DAYS} days")
        days = min(days, CRITICAL_ACK_MAX_DAYS)
    elif run.handoff[key].severity == "warning" and not accept and days > WARNING_SNOOZE_MAX_DAYS:
        ctx.notes.append(f"warnings can be snoozed for at most {WARNING_SNOOZE_MAX_DAYS} days")
        days = WARNING_SNOOZE_MAX_DAYS
    return args.replace(days=days, accept=accept, reason=(args.opt_text("reason") or "no reason given")[:300])


async def exec_ack(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    ack = Ack(key=args.text("key"), reason=args.text("reason"), days=args.number("days"), accept=args.flag("accept"))
    run.effects.acks.append(ack)
    verb = "accepted" if ack.accept else f"snoozed for {ack.days} day(s)"
    return f"ok: {ack.key} {verb}; applied when the run ends"


def validate_notify(ctx: ExecContext, args: Args) -> Args:
    run = _run(ctx)
    message = (args.opt_text("message") or "").strip()[:MAX_NOTIFY_CHARS]
    if not message:
        raise CapabilityError("message is required")
    urgent = args.flag("urgent")
    if urgent and not run.has_critical:
        ctx.notes.append("urgent downgraded: no critical item in this run")
        urgent = False
    return args.replace(
        message=message, urgent=urgent, title=(args.opt_text("title") or "Ops Agent").strip() or "Ops Agent"
    )


async def exec_notify(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    sink = ctx.env.effects
    if sink is None:
        raise CapabilityError("notifications are not available")
    redactor = ctx.env.redactor()
    message = agent_message(
        redactor.redact(args.text("title")),
        redactor.redact(args.text("message")),
        urgent=args.flag("urgent"),
        budget_class=run.budget_class,
    )
    outcome = await sink.send_message(message)
    run.effects.notify_outcomes.append(outcome)
    if outcome in ("sent", "relayed"):
        return f"ok: push {outcome}"
    if outcome == "held":
        return "ok: quiet hours; the push is held and goes out when they end"
    return f"push not delivered: {outcome}"
