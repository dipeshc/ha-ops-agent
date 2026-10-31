from pathlib import Path

from opsd.fingerprint import fingerprint, normalize
from opsd.redact import Redactor, build_redactor


def test_fingerprint_ignores_volatile_tokens() -> None:
    a = "Failed to connect to 192.168.1.20:1883 after 3 attempts (id 4f1c2a9b-1111-2222-3333-444455556666)"
    b = "Failed to connect to 10.0.0.7:1883 after 12 attempts (id 0a1c2a9b-1111-2222-3333-444455556666)"
    assert normalize(a) == normalize(b)
    assert fingerprint("acme_weather", a) == fingerprint("acme_weather", b)
    assert fingerprint("acme_weather", a) != fingerprint("acme_solar", a)
    assert normalize("Entity 'light.kitchen' timed out") == "Entity <str> timed out"


def test_redactor_masks_credentials() -> None:
    r = Redactor(["supersecretvalue"])
    text = (
        "rtsp://admin:pa55word@192.168.1.5/stream token=abc123def456 Authorization: Bearer abcdefghijklmnopqrstuv "
        "key sk-ant-oat01-ABCDEFGHIJKL supersecretvalue mail me@example.com"
    )
    out = r.redact(text)
    for secret in (
        "pa55word",
        "abc123def456",
        "abcdefghijklmnopqrstuv",
        "sk-ant-oat01",
        "supersecretvalue",
        "me@example.com",
    ):
        assert secret not in out
    assert "rtsp://***:***@192.168.1.5" in out


def test_redactor_from_secrets_yaml(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("# comment\nwifi: hunter22secret\nshort: abc\nquoted: 'q-value-123'\n")
    r = build_redactor(secrets)
    assert r.redact("x hunter22secret q-value-123 abc") == "x *** *** abc"
    assert r.redact_obj({"a": ["hunter22secret"], "n": 1}) == {"a": ["***"], "n": 1}
