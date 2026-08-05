"""Web API path safety and secret masking."""

import pytest
from fastapi.testclient import TestClient

from pocket_libre.web import app as webapp


@pytest.fixture
def out_root(tmp_path, monkeypatch):
    """Point the app at a temp output directory with one recording in it."""
    root = tmp_path / "Pocket Libre"
    (root / "2026-03-28").mkdir(parents=True)
    (root / "2026-03-28" / "20260328001919_transcript.txt").write_text("[00:00] A: hi")
    (root / "2026-03-28" / "20260328001919_summary.md").write_text("# Summary")
    (root / "2026-03-28" / "20260328001919.mp3").write_bytes(b"\xff\xf3fake")

    # A file the traversal tests try to reach, one level above the root.
    (tmp_path / "secret_transcript.txt").write_text("SHOULD NOT BE SERVED")
    (tmp_path / "secret.mp3").write_bytes(b"SHOULD NOT BE SERVED")

    monkeypatch.setattr(webapp, "load_config", lambda: {})
    monkeypatch.setattr(webapp, "get_output_dir", lambda config, cli_value=None: str(root))
    return root


@pytest.fixture
def client(out_root):
    return TestClient(webapp.app)


# ── Happy path ──────────────────────────────────


def test_serves_a_real_transcript(client):
    r = client.get("/api/local/2026-03-28/20260328001919/transcript")
    assert r.status_code == 200
    assert "hi" in r.text


def test_serves_a_real_summary(client):
    assert client.get("/api/local/2026-03-28/20260328001919/summary").status_code == 200


def test_serves_real_audio(client):
    r = client.get("/api/local/2026-03-28/20260328001919/audio")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"


def test_missing_recording_is_404(client):
    assert client.get("/api/local/2026-03-28/99999999999999/transcript").status_code == 404


def test_lists_local_recordings(client):
    r = client.get("/api/local/recordings")
    assert r.status_code == 200
    assert [x["timestamp"] for x in r.json()] == ["20260328001919"]


# ── Path traversal ──────────────────────────────

# Starlette percent-decodes path parameters *after* routing, so "%2e%2e"
# reaches the handler as ".." and previously escaped the output directory.
TRAVERSALS = [
    "%2e%2e/secret",
    "%2E%2E/secret",
    "..%2f..%2fsecret",
    "....//secret",
]


@pytest.mark.parametrize("date", ["%2e%2e", "%2E%2E", "..", "."])
@pytest.mark.parametrize("kind", ["transcript", "summary", "audio"])
def test_traversal_via_date_is_rejected(client, date, kind):
    r = client.get(f"/api/local/{date}/secret/{kind}")
    assert r.status_code in (400, 404), r.text
    assert "SHOULD NOT BE SERVED" not in r.text


@pytest.mark.parametrize("timestamp", ["%2e%2e", "..", "%2e%2e/secret"])
def test_traversal_via_timestamp_is_rejected(client, timestamp):
    r = client.get(f"/api/local/2026-03-28/{timestamp}/transcript")
    assert r.status_code in (400, 404), r.text
    assert "SHOULD NOT BE SERVED" not in r.text


@pytest.mark.parametrize("bad", ["a/b", "a\\b", "with space", "semi;colon", ""])
def test_invalid_components_are_rejected(client, bad):
    r = client.get(f"/api/local/{bad}/20260328001919/transcript")
    assert r.status_code in (400, 404)


def test_absolute_path_is_rejected(client):
    r = client.get("/api/local/%2Fetc/passwd/transcript")
    assert r.status_code in (400, 404)


def test_traversal_on_analyses_is_rejected(client):
    r = client.get("/api/local/%2e%2e/secret/analyses")
    assert r.status_code in (400, 404)


def test_path_helper_rejects_escape(tmp_path):
    """Unit-level check of the containment guard itself."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        webapp._recording_path(tmp_path, "..", "x", ".mp3")


def test_path_helper_builds_expected_path(tmp_path):
    p = webapp._recording_path(tmp_path, "2026-03-28", "ts", ".mp3")
    assert p == (tmp_path / "2026-03-28" / "ts.mp3").resolve()


# ── Secret masking ──────────────────────────────


def _config_with(secret):
    return {"api": {"anthropic_key": secret}, "device": {"session_key": secret}}


@pytest.mark.parametrize(
    "secret", ["sk-ant-averylongkeyvalue", "shortkey", "zqx", "z"]
)
def test_secrets_are_never_returned_verbatim(client, monkeypatch, secret):
    """Regression: the old `len > 8` guard leaked short keys in cleartext."""
    monkeypatch.setattr(webapp, "load_config", lambda: _config_with(secret))
    body = client.get("/api/config").json()
    for masked in (body["api"]["anthropic_key"], body["device"]["session_key"]):
        assert masked != secret
        # Only a 4-character tail may survive, and only for longer secrets.
        assert secret not in masked
        assert masked.startswith("...")
        assert len(masked) <= 7


def test_masking_flags_that_a_secret_is_set(client, monkeypatch):
    monkeypatch.setattr(webapp, "load_config", lambda: _config_with("sk-ant-longvalue"))
    body = client.get("/api/config").json()
    assert body["api"]["_anthropic_key_set"] is True


def test_non_secret_values_are_returned_plainly(client, monkeypatch):
    monkeypatch.setattr(
        webapp, "load_config", lambda: {"defaults": {"whisper_model": "base.en"}}
    )
    assert client.get("/api/config").json()["defaults"]["whisper_model"] == "base.en"


# ── Device endpoints guard on configuration ─────


@pytest.mark.parametrize("path", ["/api/device/status", "/api/device/recordings"])
def test_device_endpoints_require_configuration(client, path):
    assert client.get(path).status_code == 400
