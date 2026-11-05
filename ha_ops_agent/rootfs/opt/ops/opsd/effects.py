"""The push the AI can make during a run: `notify.admin` (spec capabilities.md K11).

Run-only, clamped in its validator (a push only for a reason class that needs the admin, else a quiet History
note), quota-limited, recorded in `RunEffects`; every text redacted.
"""

from opsd.capabilities import Args, CapabilityError
from opsd.channels import agent_message
from opsd.policy import Origin
from opsd.runctx import ExecContext, RunContext

MAX_NOTIFY_CHARS = 900
# Why the agent's push needs the admin: the only classes that reach the phone. `note` keeps the text as a quiet
# History note instead; any other reason, or none, is refused (`validate_notify`).
PUSH_REASONS = frozenset({"needs_action", "physical_task", "security"})
NOTE = "note"
# History notes a run may write (they take no push quota).
MAX_NOTES_PER_RUN = 3


def _run(ctx: ExecContext) -> RunContext:
    if ctx.run is None:
        raise CapabilityError(f"{ctx.capability.name} is only available inside an agent run")
    return ctx.run


def validate_notify(ctx: ExecContext, args: Args) -> Args:
    run = _run(ctx)
    message = (args.opt_text("message") or "").strip()[:MAX_NOTIFY_CHARS]
    if not message:
        raise CapabilityError("message is required")
    urgent = args.flag("urgent")
    if urgent and not run.has_critical:
        ctx.notes.append("urgent downgraded: no critical item in this run")
        urgent = False
    reason = args.opt_text("reason") or ""
    if reason not in PUSH_REASONS and reason != NOTE:
        # Refused before any quota is taken: the run may try again with a valid class.
        raise CapabilityError(f"--reason is required: {', '.join(sorted(PUSH_REASONS))} (a push: the admin must act"
                              f" or decide), or {NOTE} (a History note, no push)")  # fmt: skip
    if reason == NOTE:
        if run.counts.get("notify.note", 0) >= MAX_NOTES_PER_RUN:
            raise CapabilityError(f"at most {MAX_NOTES_PER_RUN} History notes per run")
        urgent = False
    return args.replace(
        message=message, urgent=urgent, reason=reason,
        title=(args.opt_text("title") or "Ops Agent").strip() or "Ops Agent",
    )  # fmt: skip


async def exec_notify(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    sink = ctx.env.effects
    if sink is None:
        raise CapabilityError("notifications are not available")
    redactor = ctx.env.redactor()
    title, body = redactor.redact(args.text("title")), redactor.redact(args.text("message"))
    if args.opt_text("reason") == NOTE:
        # For information only: the admin need not act, so it goes to History, never to the phone.
        run.counts["notify.note"] = run.counts.get("notify.note", 0) + 1
        await sink.note_message(title, body)
        run.effects.notify_outcomes.append("noted")
        return "ok: kept as a History note, not pushed"
    message = agent_message(title, body, urgent=args.flag("urgent"), budget_class=run.budget_class)
    # A push from the agent's own run (triage) offers no Reply: what it says is the model's, not the admin's
    # conversation, so it cannot invite an answer into the chat.
    outcome = await sink.send_message(message, reply=run.principal.origin is not Origin.AGENT_SELF)
    run.effects.notify_outcomes.append(outcome)
    if outcome in ("sent", "relayed"):
        return f"ok: push {outcome}"
    if outcome == "held":
        return "ok: quiet hours; the push is held and goes out when they end"
    return f"push not delivered: {outcome}"
