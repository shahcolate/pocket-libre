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


# ── Profile scoping ─────────────────────────────


TWO_PROFILES = {
    "device": {"session_key": "SHAREDACCOUNT123"},
    "profiles": {
        "mine": {"label": "Mine", "address": "AA:01", "accent": "#c4ef17"},
        "hers": {"label": "Hers", "address": "AA:02", "accent": "#C0888D"},
    },
}


@pytest.fixture
def two_libraries(tmp_path, monkeypatch):
    """A config with two profiles, each with one recording only it should see."""
    for name, stamp in (("mine", "20260101010101"), ("hers", "20260202020202")):
        day = tmp_path / name / "2026-01-01"
        day.mkdir(parents=True)
        # The library is indexed by the audio file, so it has to be there too.
        (day / f"{stamp}.mp3").write_bytes(b"\xff\xf3fake")
        (day / f"{stamp}_transcript.txt").write_text(f"{name} only", encoding="utf-8")

    config = dict(TWO_PROFILES)
    config["output"] = {"directory": str(tmp_path)}
    monkeypatch.setattr(webapp, "load_config", lambda: config)

    def _serve(profile):
        webapp.set_active_profile(profile)
        return TestClient(webapp.app)

    yield _serve
    webapp.set_active_profile(None)


def test_server_reports_its_own_profile(two_libraries):
    client = two_libraries("hers")
    body = client.get("/api/profile").json()
    assert body["name"] == "hers"
    assert body["label"] == "Hers"
    assert body["accent"] == "#C0888D"
    assert body["library"].endswith("hers")


def test_each_server_sees_only_its_own_library(two_libraries):
    mine = two_libraries("mine").get("/api/local/recordings").json()
    hers = two_libraries("hers").get("/api/local/recordings").json()

    def stamps(payload):
        rows = payload if isinstance(payload, list) else payload.get("recordings", [])
        return {str(row) for row in rows}

    assert "20260101010101" in str(mine)
    assert "20260202020202" not in str(mine)
    assert "20260202020202" in str(hers)
    assert "20260101010101" not in str(hers)
    assert stamps(mine) != stamps(hers)


def test_the_other_profiles_transcript_is_not_reachable(two_libraries):
    """Asking Hers's server for a recording that only exists in the other library."""
    client = two_libraries("hers")
    r = client.get("/api/local/2026-01-01/20260101010101/transcript")
    assert r.status_code == 404


def test_device_commands_use_the_servers_own_device(two_libraries, monkeypatch):
    client = two_libraries("hers")
    config = webapp.current_config()
    assert webapp.resolve_address(config) == "AA:02"
    # The shared account key is inherited, not duplicated per profile.
    assert webapp.resolve_session_key(config) == "SHAREDACCOUNT123"
    assert client.get("/api/profile").json()["name"] == "hers"


def test_settings_edits_land_in_the_active_profile(two_libraries, monkeypatch):
    saved = {}
    monkeypatch.setattr(webapp, "save_config", lambda c: saved.update({"config": c}))
    client = two_libraries("hers")

    r = client.put("/api/config", json={"device": {"address": "NEW"}})
    assert r.status_code == 200

    written = saved["config"]
    assert written["profiles"]["hers"]["address"] == "NEW"
    # Neither the globals nor the other profile moved.
    assert written["profiles"]["mine"]["address"] == "AA:01"
    assert written.get("device", {}).get("address") in (None, "")


def test_settings_edits_never_save_a_folded_config(two_libraries, monkeypatch):
    from pocket_libre import config as cfg

    saved = {}
    monkeypatch.setattr(webapp, "save_config", lambda c: saved.update({"config": c}))
    client = two_libraries("hers")
    client.put("/api/config", json={"api": {"hf_token": "hf_x"}})
    assert cfg.EFFECTIVE_MARKER not in saved["config"]
    assert saved["config"]["profiles"]["hers"]["hf_token"] == "hf_x"


# ── Search ──────────────────────────────────────


def test_search_finds_a_recording_in_this_library(two_libraries):
    client = two_libraries("hers")
    results = client.get("/api/search", params={"q": "hers"}).json()
    assert results
    assert results[0]["session_id"] == "2026-01-01/20260202020202"
    assert "kind" in results[0] and "snippet" in results[0]


def test_search_cannot_reach_the_other_library(two_libraries):
    """Each server indexes only its own profile's recordings."""
    hers = two_libraries("hers").get("/api/search", params={"q": "mine"}).json()
    mine = two_libraries("mine").get("/api/search", params={"q": "hers"}).json()
    assert hers == []
    assert mine == []


def test_an_empty_search_returns_nothing(two_libraries):
    client = two_libraries("hers")
    assert client.get("/api/search", params={"q": "  "}).json() == []
    assert client.get("/api/search").json() == []


def test_hostile_search_syntax_does_not_500(two_libraries):
    client = two_libraries("hers")
    for hostile in ['NEAR "unclosed', "*", '"', "AND"]:
        assert client.get("/api/search", params={"q": hostile}).status_code == 200


def test_the_result_limit_is_clamped(two_libraries):
    client = two_libraries("hers")
    assert client.get("/api/search", params={"q": "hers", "limit": 9999}).status_code == 200
    assert client.get("/api/search", params={"q": "hers", "limit": 0}).status_code == 200
