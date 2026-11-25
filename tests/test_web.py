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


def test_sync_records_verified_download_and_retries_unverified_copy(
    client, out_root, monkeypatch
):
    import json
    import sys
    import types

    from pocket_libre.commands import (
        DOWNLOADS_FILE,
        Recording,
        has_complete_copy,
        save_recording,
    )
    from pocket_libre.protocol import MP3_SYNC_WORD

    rec = Recording("2026-03-29", "20260329001919", 4)
    path = out_root / rec.date / rec.filename
    path.parent.mkdir(parents=True)
    save_recording(path, b"partial", False, rec)

    payload = MP3_SYNC_WORD + b"\x00" * 10

    class FakeCommander:
        def __init__(self, address):
            self._disconnected = False
            self.last_expected_size = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def authenticate(self, key):
            return True

        async def list_all_recordings(self):
            return [rec]

        async def download_ble(self, recording, progress_callback=None):
            self.last_expected_size = len(payload)
            return payload

    monkeypatch.setattr(webapp, "PocketCommander", FakeCommander)
    monkeypatch.setattr(webapp, "_require_device", lambda config: ("address", "key"))
    monkeypatch.setattr(webapp, "resolve_llm", lambda config: None)
    monkeypatch.setattr(webapp, "resolve_hf_token", lambda config: None)

    def fail_load_model(model):
        raise RuntimeError("skip transcription in this test")

    monkeypatch.setitem(
        sys.modules,
        "whisper",
        types.SimpleNamespace(load_model=fail_load_model),
    )

    response = client.get("/api/sync-all")

    assert response.status_code == 200
    assert "Synced 1 recording(s)" in response.text
    assert has_complete_copy(path, rec)
    assert json.loads((path.parent / DOWNLOADS_FILE).read_text()) == {
        path.name: {"size": path.stat().st_size, "duration_s": rec.duration_s}
    }


# ── Language model settings (#39) ───────────────


def test_local_model_key_is_masked(client, monkeypatch):
    monkeypatch.setattr(webapp, "load_config", lambda: {
        "llm": {"provider": "openai", "model": "qwen3:14b", "api_key": "sk-local-secret"}})
    body = client.get("/api/config").json()
    assert body["llm"]["model"] == "qwen3:14b"
    assert "secret" not in body["llm"]["api_key"]
    assert body["llm"]["_api_key_set"] is True


def test_llm_settings_are_saved(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(webapp, "save_config", saved.update)
    r = client.put("/api/config", json={"llm": {
        "provider": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3.2",
        "api_key": "...cret"}})
    assert r.status_code == 200
    # A masked value sent back is never saved over the real key.
    assert saved["llm"] == {"provider": "openai", "base_url": "http://localhost:11434/v1",
                            "model": "llama3.2"}


def test_chat_without_a_model_says_how_to_add_one(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("POCKET_LIBRE_LLM_PROVIDER", raising=False)
    r = client.post("/api/chat/2026-03-28/20260328001919", json={"message": "hi"})
    assert r.status_code == 400
    assert "local model" in r.json()["detail"]
