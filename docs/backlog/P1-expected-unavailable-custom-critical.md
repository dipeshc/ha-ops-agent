---
by: owner (spec review 0.17.0)
spec: docs/spec/health.md custom:<domain>
---

# An expected-unavailable entity still makes its custom integration critical

**What's wrong.** An entity listed in `health.expected_unavailable` is
excluded from the `entities:<platform>` check but still counts as down in
the `custom:<domain>` check, so a custom integration whose only entities
are expected-unavailable raises a critical item and an urgent push.

**Evidence.** `ha_ops_agent/rootfs/opt/ops/opsd/baseline.py:179` builds
`known = baseline.unavailable | expected_unavailable` for the entities
check only. `baseline.py:202-215` computes `settled["unavailable"]` from
`snapshot.unavailable` and `info["unavailable"]` (`health.py:380-384`,
`derive_custom_integrations`), neither of which subtracts
`expected_unavailable`; `_custom_ok` (`baseline.py:72-77`) then sees all
entities down. The baseline seed (`baseline.py:91,108`) likewise excludes
every custom entity from `unavailable` regardless of the option.

**Consequence.** A false critical (`custom:<domain>`), an urgent
safety-class push, and a triage run, every day until the admin accepts
the item, for a device the admin already declared normally off.

**Acceptance criteria.**
- `diff()` with `expected_unavailable` naming every down entity of a
  custom domain raises no `custom:<domain>` item; naming only some of them
  raises it only if the rest still bring the count to all entities.
- The seed treats an expected-unavailable custom entity as accepted.
- A new test in `tests/test_health_baseline.py`, e.g.
  `test_expected_unavailable_entities_do_not_break_a_custom_integration`.
- `docs/spec/health.md`: remove the "Currently violated" note from the
  `custom:<domain>` row in the same commit.

**Exact files to change.** `opsd/baseline.py` (`diff`, `seed`), possibly
`opsd/health.py` (`derive_custom_integrations` taking the expected set),
`tests/test_health_baseline.py`, `docs/spec/health.md`.

**Suggested approach.** Subtract the expected set when computing `settled`
in `diff` (count only entities of the domain that are down and not
expected), and pass the same set into `seed` so `custom_broken` and
`custom_unavailable` ignore expected entities. Keep `snapshot.unavailable`
itself untouched: `opsctl health --diff` and the panel still list those
entities as unavailable, they just are not news.
