import pytest

from opsd.config import SELF_SLUG
from opsd.policy import Bump, Decision, Tier, UpdateInfo, classify, parse_version, update_kind, version_bump
from tests.conftest import SAMPLE_PROFILE


@pytest.mark.parametrize(
    ("installed", "latest", "expected"),
    [
        ("2026.10.3", "2026.10.4", Bump.PATCH),
        ("2026.8.3", "2026.10.3", Bump.MINOR),
        ("v14.0.2", "v15.0.0", Bump.MAJOR),
        ("0.1.10", "0.1.11", Bump.PATCH),
        ("0.1.11", "0.2.0", Bump.MAJOR),
        ("v1.2.1", "v1.2.2", Bump.PATCH),
        ("24.1.7", "24.2.0", Bump.MINOR),
        ("1.0", "1.0.0", Bump.NONE),
        ("2026-01-09", "2026-02-01", Bump.UNKNOWN),
        ("v3.0.0.4867_2505072123", "v3.0.0.5000_2601010000", Bump.UNKNOWN),
        (None, "1.0.0", Bump.UNKNOWN),
    ],
)
def test_version_bump(installed: str | None, latest: str, expected: Bump) -> None:
    assert version_bump(installed, latest) is expected


def test_parse_version_rejects_dates() -> None:
    assert parse_version("2026-01-09") is None
    assert parse_version("v1.16.12") == (1, 16, 12)


def info(entity_id: str, installed: str, latest: str, platform: str) -> UpdateInfo:
    return UpdateInfo(entity_id, entity_id, installed, latest, platform, update_kind(entity_id, platform))


def test_core_rules() -> None:
    core = "update.home_assistant_core_update"
    assert classify(info(core, "2026.10.3", "2026.11.0", "hassio")).tier is Tier.NEVER
    # A minor soaks twice as long as a patch, then it is a candidate like a patch.
    minor = info(core, "2026.10.3", "2026.11.1", "hassio")
    assert classify(minor, release_age_days=9).waits and classify(minor, release_age_days=14).tier is Tier.AUTO
    assert classify(info(core, "2026.10.3", "2027.1.1", "hassio"), release_age_days=30).tier is Tier.APPROVE
    patch = info(core, "2026.10.3", "2026.10.4", "hassio")
    assert classify(patch, release_age_days=2).tier is Tier.APPROVE and classify(patch, release_age_days=2).waits
    assert classify(patch).waits and "unknown" in classify(patch).reason
    assert classify(patch, release_age_days=8).tier is Tier.AUTO and not classify(patch, release_age_days=8).waits


def test_os_firmware_supervisor_and_self() -> None:
    assert (
        classify(info("update.home_assistant_operating_system_update", "17.2", "17.3", "hassio")).tier is Tier.APPROVE
    )
    assert classify(info("update.home_assistant_supervisor_update", "1.0.0", "1.0.1", "hassio")).tier is Tier.MANUAL
    # Device firmware needs approval (house facts); a firmware device class wins over the platform.
    assert classify(info("update.speaker_firmware", "26.6.0", "26.9.0", "esphome")).tier is Tier.APPROVE
    fw = UpdateInfo(
        "update.fridge_fw", "t", "1.0.1", "1.0.2", "mqtt", update_kind("update.fridge_fw", "mqtt", "firmware")
    )
    assert classify(fw).tier is Tier.APPROVE and "firmware" in classify(fw).reason
    agent = info("update.ha_ops_agent_update", "0.1.0", "0.1.1", "hassio")
    assert classify(agent, addon_slug=SELF_SLUG).tier is Tier.MANUAL
    # The agent is recognised by entity id and title too, even without a slug.
    assert classify(agent).tier is Tier.MANUAL
    renamed = UpdateInfo("update.something_else", "HA Ops Agent", "0.1.0", "0.1.1", "hassio", "addon")
    assert classify(renamed, addon_slug="whatever").tier is Tier.MANUAL


HACS_OK = 1 | 2 | 4 | 16


def aged(update: UpdateInfo, **kw: object) -> Decision:
    """`classify` for an update whose release is old enough (the age rule has its own test)."""
    return classify(update, release_age_days=30, **kw)  # type: ignore[arg-type]


def hacs(entity_id: str, installed: str, latest: str, **kw: object) -> UpdateInfo:
    fields: dict[str, object] = {"supported_features": HACS_OK, "hacs_category": "integration"} | kw
    return UpdateInfo(entity_id, entity_id, installed, latest, "hacs", "hacs", **fields)  # type: ignore[arg-type]


def test_addon_and_hacs_tiers() -> None:
    ssh = "update.ssh_web_terminal_update"
    assert aged(info(ssh, "24.2.0", "24.2.1", "hassio"), addon_slug="a0d7b954_ssh").tier is Tier.AUTO
    # Minors are automatic too once soaked; majors always ask.
    assert aged(info(ssh, "24.1.7", "24.2.0", "hassio"), addon_slug="a0d7b954_ssh").tier is Tier.AUTO
    assert aged(info(ssh, "24.2.0", "25.0.0", "hassio"), addon_slug="a0d7b954_ssh").tier is Tier.APPROVE
    # Without a slug there is no precise rollback.
    assert aged(info(ssh, "24.2.0", "24.2.1", "hassio")).tier is Tier.APPROVE
    assert aged(hacs("update.acme_card_update", "v3.0.2", "v4.0.0")).tier is Tier.APPROVE
    assert aged(hacs("update.example_cloud_update", "0.1.10", "0.1.11")).tier is Tier.AUTO
    assert aged(hacs("update.example_cloud_update", "1.1.10", "1.2.0")).tier is Tier.AUTO
    # HACS needs specific-version installs (the rollback) and a known category.
    assert aged(hacs("update.x_update", "1.0.0", "1.0.1", supported_features=1 | 4)).tier is Tier.APPROVE
    assert aged(hacs("update.x_update", "1.0.0", "1.0.1", hacs_category=None)).tier is Tier.APPROVE
    assert aged(hacs("update.x_update", "1.0.0", "1.0.1", hacs_category="python_script")).tier is Tier.APPROVE
    pinned = frozenset({"update.example_cloud_update"})
    assert aged(hacs("update.example_cloud_update", "0.1.10", "0.1.11"), approval_updates=pinned).tier is (Tier.APPROVE)
    assert aged(hacs("update.example_cloud_update", "0.1.10", "0.1.11"), never_updates=pinned).tier is Tier.NEVER
    assert aged(info("update.thing", "1.0.0", "1.0.1", "mystery")).tier is Tier.APPROVE


def test_a_core_addon_minor_soaks_14_days_then_installs() -> None:
    """A core add-on minor soaks twice as long as a patch."""
    matter = info("update.example_broker_update", "4.2.0", "4.3.0", "hassio")
    young = classify(matter, release_age_days=3, addon_slug="core_example_broker")
    assert young.tier is Tier.APPROVE and young.waits and young.bump is Bump.MINOR
    assert young.reason == "addon minor: soaking for 14 days (3 so far)"
    ripe = classify(matter, release_age_days=14, addon_slug="core_example_broker")
    assert ripe.tier is Tier.AUTO and not ripe.waits and ripe.reason == "addon minor, soaked 14 days"


def test_fragile_pieces_from_the_house_profile() -> None:
    house = SAMPLE_PROFILE.updates.fragile
    acme_pool = hacs("update.acme_pool_update", "v3.0.4", "v3.1.0", hacs_category="integration")
    decision = classify(acme_pool, fragile=house)
    assert decision.tier is Tier.PINNED and "pump schedule" in decision.reason
    # Matched by repository or domain too, whatever the entity is called.
    renamed = hacs("update.pool_controller_update", "v3.0.4", "v3.1.2", hacs_repo="example/acme_pool")
    assert classify(renamed, fragile=house).tier is Tier.PINNED
    by_domain = hacs("update.pool_controller_update", "v3.0.4", "v3.1.2", hacs_domain="acme_pool")
    assert classify(by_domain, fragile=house).tier is Tier.PINNED
    assert aged(hacs("update.acme_solar_update", "1.0.0", "1.0.1"), fragile=house).tier is Tier.APPROVE
    acme_weather = hacs("update.acme_weather_update", "v2.4.0", "v2.4.1", hacs_domain="acme_weather")
    assert aged(acme_weather, fragile=house).tier is Tier.APPROVE
    # acme-card: majors wait for the Core minor; patches follow the normal rule.
    assert aged(hacs("update.acme_card_update", "v4.0.0", "v5.0.0"), fragile=house).tier is Tier.APPROVE
    acme_card = hacs("update.acme_card_update", "v4.0.0", "v4.0.1", hacs_category="integration")
    assert aged(acme_card, fragile=house).tier is Tier.AUTO
    # Without a profile none of them is special: the generic rule decides.
    assert aged(acme_weather).tier is Tier.AUTO and classify(acme_pool).tier is Tier.APPROVE


def test_skipped_in_progress_and_age_option() -> None:
    skipped = hacs("update.x_update", "1.0.0", "1.0.1", skipped_version="1.0.1")
    assert classify(skipped).tier is Tier.MANUAL
    # A skip of an older version does not hold a newer one.
    assert aged(hacs("update.x_update", "1.0.0", "1.0.2", skipped_version="1.0.1")).tier is Tier.AUTO
    assert aged(hacs("update.x_update", "1.0.0", "1.0.1", in_progress=True)).tier is Tier.MANUAL
    core = "update.home_assistant_core_update"
    patch = info(core, "2026.10.3", "2026.10.4", "hassio")
    assert classify(patch, release_age_days=8, min_age_days=10).waits
    assert classify(patch, release_age_days=11, min_age_days=10).tier is Tier.AUTO
    # The option can only make the rule stricter: below 7 days is ignored.
    assert classify(patch, release_age_days=3, min_age_days=1).waits
    assert classify(patch, release_age_days=9, approval_updates=frozenset({core})).tier is Tier.APPROVE
    assert not classify(patch, release_age_days=9, approval_updates=frozenset({core})).waits
    assert classify(info(core, "2026.10.4", "2026.11.0", "hassio"), release_age_days=30).tier is Tier.NEVER


def test_every_kind_needs_a_known_age() -> None:
    card = hacs("update.card_update", "1.0.0", "1.0.1", hacs_category="integration")
    assert classify(card).waits and "unknown" in classify(card).reason
    assert classify(card, release_age_days=2).waits
    assert classify(card, release_age_days=7.5).tier is Tier.AUTO
    ssh = info("update.ssh_web_terminal_update", "24.2.0", "24.2.1", "hassio")
    assert classify(ssh, addon_slug="a0d7b954_ssh").waits
    assert classify(ssh, addon_slug="a0d7b954_ssh", release_age_days=8, min_age_days=10).waits


# ------------------------------------------------------------ policy.decide

from opsd.policy import (  # noqa: E402
    Effect,
    Origin,
    PolicyTier,
    Resolution,
    decide,
    resolve_update,
    strictest,
)

READ = frozenset({Effect.READ})
GIT = frozenset({Effect.MUTATE_GIT})
BOTH = frozenset(Origin)


def test_strictest_order() -> None:
    assert strictest(PolicyTier.AUTO, PolicyTier.AUTO_NOTIFY) is PolicyTier.AUTO_NOTIFY
    assert strictest(*PolicyTier) is PolicyTier.NEVER


@pytest.mark.parametrize(
    ("tier", "outcome"),
    [
        (PolicyTier.AUTO, "execute"),
        (PolicyTier.AUTO_NOTIFY, "execute"),
        (PolicyTier.APPROVE, "needs_approval"),
        (PolicyTier.NEVER, "denied"),
    ],
)
def test_default_tiers(tier: PolicyTier, outcome: str) -> None:
    verdict = decide(
        capability="x.y", default_tier=tier, effects=READ, origin=Origin.ADMIN, allowed_origins=BOTH, acting=True
    )
    assert verdict.outcome == outcome and verdict.notify_admin is (tier is PolicyTier.AUTO_NOTIFY)


def test_origin_not_accepted_is_never() -> None:
    verdict = decide(capability="x.y", default_tier=PolicyTier.AUTO, effects=READ, origin=Origin.ADMIN,
                     allowed_origins=frozenset({Origin.AGENT_SELF}), acting=True)  # fmt: skip
    assert verdict.tier is PolicyTier.NEVER and not verdict.execute


def test_observe_mode_reports_external_effects_only() -> None:
    kw = {"capability": "c.d", "default_tier": PolicyTier.AUTO, "origin": Origin.AGENT_SELF, "allowed_origins": BOTH}
    observe = decide(effects=GIT, acting=False, **kw)  # type: ignore[arg-type]
    assert observe.report_only and not observe.execute and observe.outcome == "report_only"
    assert decide(effects=GIT, acting=True, **kw).execute  # type: ignore[arg-type]
    assert decide(effects=frozenset({Effect.NOTIFY}), acting=False, **kw).execute  # type: ignore[arg-type]
    # A refused request is not "report only".
    assert not decide(effects=GIT, acting=False, **{**kw, "default_tier": PolicyTier.NEVER}).report_only  # type: ignore[arg-type]


def test_tightening_and_resolver_cannot_loosen() -> None:
    base = {"capability": "c.d", "effects": READ, "origin": Origin.ADMIN, "allowed_origins": BOTH, "acting": True}
    assert decide(default_tier=PolicyTier.AUTO, tighten={"c.d": PolicyTier.APPROVE}, **base).tier is PolicyTier.APPROVE  # type: ignore[arg-type]
    assert decide(default_tier=PolicyTier.AUTO, tighten={"other": PolicyTier.NEVER}, **base).execute  # type: ignore[arg-type]
    loosen = Resolution(PolicyTier.AUTO, "resolver says auto")
    assert decide(default_tier=PolicyTier.APPROVE, resolution=loosen, **base).tier is PolicyTier.APPROVE  # type: ignore[arg-type]
    loosen_opt = {"c.d": PolicyTier.AUTO}
    assert decide(default_tier=PolicyTier.NEVER, tighten=loosen_opt, **base).tier is PolicyTier.NEVER  # type: ignore[arg-type]


def test_update_classify_is_one_resolver() -> None:
    core = "update.home_assistant_core_update"
    young = classify(info(core, "2026.10.3", "2026.10.4", "hassio"), release_age_days=2)
    assert resolve_update(young).tier is PolicyTier.APPROVE  # soaking: never executed without an approval
    assert resolve_update(classify(info(core, "2026.10.3", "2026.10.4", "hassio"), release_age_days=9)).tier is (
        PolicyTier.AUTO
    )
    assert resolve_update(classify(info(core, "2026.10.3", "2026.11.1", "hassio"))).tier is PolicyTier.APPROVE
    assert resolve_update(Decision(Tier.MANUAL, Bump.PATCH, "self")).tier is PolicyTier.NEVER


def test_chat_origin_only_reads_schedules_and_notifies() -> None:
    def verdict(effects: frozenset[Effect], tier: PolicyTier = PolicyTier.AUTO) -> object:
        return decide(capability="c.d", default_tier=tier, effects=effects, origin=Origin.ADMIN_CHAT,
                      allowed_origins=BOTH, acting=True)  # fmt: skip

    for ok in (READ, frozenset({Effect.SCHEDULE}), frozenset({Effect.NOTIFY}), frozenset({Effect.READ, Effect.NOTIFY})):
        assert verdict(ok).execute  # type: ignore[attr-defined]
    for bad in (
        GIT,
        frozenset({Effect.MUTATE_HA}),
        frozenset({Effect.AGENT_STATE}),
        frozenset({Effect.READ, Effect.MUTATE_HA}),
    ):
        v = verdict(bad)
        assert v.tier is PolicyTier.NEVER and any("chat-origin" in r for r in v.reasons)  # type: ignore[attr-defined]
    assert verdict(frozenset({Effect.MUTATE_HA}), PolicyTier.APPROVE).tier is PolicyTier.NEVER  # type: ignore[attr-defined]


def test_hacs_frontend_plugins_always_need_approval() -> None:
    """Lovelace plugins run as JavaScript in the admin's HA origin: never AUTO."""
    card = hacs("update.mini_media_player_update", "v1.16.13", "v1.16.14", hacs_category="plugin")
    decision = aged(card)
    assert decision.tier is Tier.APPROVE and "frontend plugin" in decision.reason
    theme = aged(hacs("update.mini_media_player_update", "v1.16.13", "v1.16.14", hacs_category="theme"))
    assert theme.tier is Tier.APPROVE and "frontend theme" in theme.reason
    assert aged(hacs("update.x", "1.0.0", "1.0.1", hacs_category="integration")).tier is Tier.AUTO
