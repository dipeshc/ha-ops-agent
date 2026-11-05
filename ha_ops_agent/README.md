# HA Ops Agent

Unattended maintenance for Home Assistant. It checks the system every day,
tells you only when something changed, installs updates that have soaked
with a backup, a health diff and rollback, and talks with you on every
item it shows. An AI model handles the parts that need judgement; plain,
tested code does everything that acts, and anything risky waits for your
approval.

- **Daily health check** against an accepted baseline: nothing new means
  no AI run and no push.
- **Nightly updates**: soaked patches and minors install one at a time
  after an encrypted backup, with a rollback if health gets worse. Majors,
  HAOS and firmware ask first.
- **Proactive maintenance**: deprecations and removals that affect your
  setup become one-tap fix proposals before they break anything.
- **A conversation on every item**, in the panel or in reply to its push,
  and questions about anything else. It never approves anything itself.
- **Never** operates locks, alarm panels or garage doors.

See the **Documentation** tab to set it up, and
https://github.com/dipeshc/ha-ops-agent for the full safety model.
