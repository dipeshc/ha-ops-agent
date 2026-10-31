"""Release-note parsers on synthetic pages: what they extract, and that unrecognised structure fails closed."""

from pathlib import Path

import pytest

from opsd.evidence import parse_changelog, parse_github_body, parse_release_post, release_post_url

FIXTURES = Path(__file__).parent / "fixtures"
INDEX = (FIXTURES / "release-index.html").read_text()
POST = (FIXTURES / "release-post.html").read_text()
SOURCE = "https://www.home-assistant.io/blog/2030/03/05/release-20303/"


def test_the_release_post_url_comes_from_the_index() -> None:
    assert release_post_url(INDEX, (2030, 3)) == SOURCE
    assert release_post_url(INDEX, (2029, 12, 1)) == "https://www.home-assistant.io/blog/2029/12/03/release-202912/"
    assert release_post_url(INDEX, (2030, 4)) is None
    assert release_post_url("<html>nothing here</html>", (2030, 3)) is None


def test_the_release_post_yields_breaking_devblog_and_removed_items() -> None:
    changes = parse_release_post(POST, SOURCE)
    breaking = [c for c in changes if c.kind == "breaking"]
    assert [(c.title, c.domain) for c in breaking] == [
        ("Acme Sprinkler", "acme_sprinkler"),
        ("Acme Weather", "acme_weather"),
        ("General", None),
    ]
    assert "acme_sprinkler.run_zone" in breaking[0].terms and "<" not in breaking[0].text
    assert {"sensor.acme_weather_rain", "sensor.acme_weather_precipitation"} <= set(breaking[1].terms)
    [devblog] = [c for c in changes if c.kind == "devblog"]
    assert devblog.terms == ("acme_helper.legacy_api",)
    [removed] = [c for c in changes if c.kind == "removed"]
    assert (removed.title, removed.domain) == ("Acme Legacy Hub", "acme_legacy_hub")
    assert all(c.source == SOURCE for c in changes)


@pytest.mark.parametrize(
    "page",
    [
        "<html><body><h2>Something else</h2></body></html>",
        POST.replace('name="backward-incompatible-changes"', 'name="breaking-changes"'),
        POST.replace("details-block-item", "accordion-item").replace("details-block-content", "accordion-body"),
    ],
    ids=["no section", "renamed anchor", "unknown item markup"],
)
def test_an_unrecognised_release_post_fails_closed(page: str) -> None:
    with pytest.raises(ValueError, match="release post"):
        parse_release_post(page, SOURCE)


def test_a_patch_body_names_what_it_touches_and_its_breaking_lines() -> None:
    body = (
        "- Fix the pump timer ([acme_pool docs])\n"
        "- Rename `acme_weather.refresh` (breaking-change) ([acme_weather docs])\n"
        "- Bump a library\n"
    )
    changes, touched = parse_github_body(body, "src")
    assert touched == {"acme_pool", "acme_weather"}
    [change] = changes
    assert change.domain == "acme_weather" and "acme_weather.refresh" in change.terms


def test_changelog_lines_that_ask_for_a_manual_step_are_kept() -> None:
    notes = "- Faster start-up\n- Breaking: the `acme_card.layout` option is removed\n- Translations\n"
    [change] = parse_changelog(notes, title="Acme Card 4.0.0", domain=None, source="src")
    assert change.kind == "notes" and "acme_card.layout" in change.terms
    assert parse_changelog("- Faster start-up\n- Translations\n", title="t", domain=None, source="s") == []
