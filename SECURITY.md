# Security policy

## Reporting a vulnerability

Report privately through GitHub:
[Security > Report a vulnerability](https://github.com/dipeshc/ha-ops-agent/security/advisories/new).
Do not open a public issue, and do not include real tokens, API keys, backup
passwords or camera URLs in a report.

Include the add-on version, what an attacker needs (network position, a Home
Assistant login, a crafted device name, ...), what they gain, and steps or a
proof of concept.

## Scope

In scope: anything in this repository, in particular a way to

- make the agent act on the house without the admin's approval where it
  should ask (an update, a config change, a reboot);
- make it operate, or reload the integration of, a lock, an alarm panel, a
  garage door or anything else that opens the house;
- let the Claude process escape its unprivileged account, read the
  Supervisor token, `/data`, the live config or backups, or call the
  Supervisor or Core directly;
- make the agent change itself, the Supervisor or the host outside the
  approved operating-system update flow;
- turn untrusted text (device names, logs, release notes, chat messages)
  into instructions the agent follows;
- reach the panel's actions as someone other than a Home Assistant admin,
  or forge an approval;
- leak a secret into a log, a push, a chat reply, git or the diagnostics
  export.

Out of scope: Home Assistant itself, the Supervisor, HAOS and Claude Code
(report those upstream), attacks that need an admin's own session or root on
the host, and denial of service by an admin.

## What to expect

This is a one-person project with best-effort support. You can expect an
acknowledgement within a week, an assessment after that, and a fix in a
patch release for confirmed issues, credited in the advisory unless you
prefer otherwise. Only the latest release is supported.
