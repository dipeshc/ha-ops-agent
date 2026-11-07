# HA Ops Agent

Unattended maintenance for Home Assistant: a daily health check, a push only
when something changed, soaked updates with a backup, a health diff and
rollback, and a conversation with the agent on every item.

Full documentation, the house profile format and the safety model:
https://github.com/dipeshc/ha-ops-agent#readme

## Quick start

1. Make sure automatic backups are set up with encryption (Settings > System
   > Backups).
2. Set up AI access in `claude.token` (a Claude Code setup token from
   `claude setup-token`, or an Anthropic API key) and `notify.service` (your Companion app, e.g.
   `mobile_app_my_phone`; its owner is the agent's admin).
3. Optionally add a house profile, `ops/profile.yaml` in your configuration
   directory (see the example in the repository).
4. Start the add-on and open **Ops Agent** in the sidebar.

It starts in `agent.mode: observe` and changes nothing. Switch to `act`, and
`updates.auto_install: on`, when you have read what it would do.

Upgrading: keep "Create backup" on when you update the add-on; that backup
is the way back, since an older version refuses a store a newer one
upgraded. 0.24.0 removes the `llm.provider` option (a saved value is
ignored; the AI switch is the off switch) and reads `claude.plan: auto` as
`pro`. See the CHANGELOG and the README's Upgrading section.

Maintenance can list maintained alternatives for an abandoned custom
integration that is at stake. They come from HACS's catalog on your install
(read only) and GitHub's public repository facts; the agent never installs,
removes or switches anything for this.

What reaches *Needs you*: only a critical health result, an approval, a
removal deadline within 30 days, a grounded finding of the agent naming
what you must do, a warning undecided for 7 days, something Home Assistant
says needs you by hand, or a failure nothing retries. Each item says why.
When the agent could not decide, the item waits under *Coming up* and is
checked again the next day; it never asks on that account.

Support: GitHub issues, best effort.
