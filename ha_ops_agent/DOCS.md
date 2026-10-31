# HA Ops Agent

Unattended maintenance for Home Assistant: a daily health check, a push only
when something changed, soaked updates with a backup, a health diff and
rollback, and one admin chat.

Full documentation, the house profile format and the safety model:
https://github.com/dipeshc/ha-ops-agent#readme

## Quick start

1. Make sure automatic backups are set up with encryption (Settings > System
   > Backups).
2. Set up AI access in `claude.token` (today a Claude Code setup token from
   `claude setup-token`) and `notify.service` (your Companion app, e.g.
   `mobile_app_my_phone`; its owner is the agent's admin).
3. Optionally add a house profile, `ops/profile.yaml` in your configuration
   directory (see the example in the repository).
4. Start the add-on and open **Ops Agent** in the sidebar.

It starts in `agent.mode: observe` and changes nothing. Switch to `act`, and
`updates.auto_install: on`, when you have read what it would do.

This add-on is not affiliated with Home Assistant, the Open Home Foundation
or Anthropic. Support: GitHub issues, best effort.
