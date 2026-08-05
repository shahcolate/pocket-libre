"""Protocol response parsing and recording-identifier safety."""

import pytest

from pocket_libre.commands import PocketCommander, Recording, is_safe_id


@pytest.fixture
def cmd():
    """A commander instance with no BLE connection — parsing helpers only."""
    return PocketCommander("AA:BB:CC:DD:EE:FF")


# ── Response parsing ────────────────────────────


def test_parse_response_extracts_matching_prefix(cmd):
    assert cmd._parse_response(["MCU&BAT&87"], "BAT") == ["87"]


def test_parse_response_ignores_other_prefixes(cmd):
    responses = ["MCU&BAT&87", "MCU&FW&1.3.3", "garbage"]
    assert cmd._parse_response(responses, "FW") == ["1.3.3"]


def test_parse_response_empty_when_no_match(cmd):
    assert cmd._parse_response(["MCU&BAT&87"], "SPA") == []


def test_parse_response_keeps_embedded_separators(cmd):
    """SPACE returns "used&total" — the split must not eat the payload."""
    assert cmd._parse_response(["MCU&SPA&1024&4096"], "SPA") == ["1024&4096"]


# ── Recording ───────────────────────────────────


def test_recording_filename():
    assert Recording("2026-03-28", "20260328001919", 100).filename == "20260328001919.mp3"


def test_recording_str_includes_size():
    assert "6222 KB" in str(Recording("2026-03-28", "20260328001919", 6222))


def test_recording_str_handles_zero_size():
    """A zero-size recording must not divide by zero or render nonsense."""
    assert "0 KB" in str(Recording("2026-03-28", "20260328001919", 0))


# ── Identifier safety ───────────────────────────


@pytest.mark.parametrize(
    "value", ["2026-03-28", "20260328001919", "a_b-c.d", "X1"]
)
def test_accepts_normal_identifiers(value):
    assert is_safe_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../etc",
        "a/b",
        "a\\b",
        "/absolute",
        "with space",
        "semi;colon",
        "new\nline",
        "null\x00byte",
    ],
)
def test_rejects_unsafe_identifiers(value):
    """These become path components on disk; none may traverse or inject."""
    assert not is_safe_id(value)


# ── LIST parsing rejects unsafe names ───────────


class _FakeSend:
    """Stands in for the BLE round trip, returning canned responses."""

    def __init__(self, responses):
        self.responses = responses

    async def __call__(self, command, verbose=False):
        return self.responses


@pytest.mark.asyncio
async def test_list_files_parses_valid_rows(cmd, monkeypatch):
    monkeypatch.setattr(
        cmd, "_send",
        _FakeSend(["MCU&F&2026-03-28&20260328001919&6222"]),
    )
    recs = await cmd.list_files("2026-03-28")
    assert len(recs) == 1
    assert recs[0].timestamp == "20260328001919"
    assert recs[0].size_kb == 6222


@pytest.mark.asyncio
async def test_list_files_drops_traversal_names(cmd, monkeypatch):
    """A malicious or malfunctioning device must not steer writes out of
    the output directory."""
    monkeypatch.setattr(
        cmd, "_send",
        _FakeSend([
            "MCU&F&2026-03-28&20260328001919&6222",
            "MCU&F&..&..&100",
            "MCU&F&2026-03-28&../../../../etc/passwd&100",
        ]),
    )
    recs = await cmd.list_files("2026-03-28")
    assert [r.timestamp for r in recs] == ["20260328001919"]


@pytest.mark.asyncio
async def test_list_files_tolerates_non_numeric_size(cmd, monkeypatch):
    monkeypatch.setattr(
        cmd, "_send", _FakeSend(["MCU&F&2026-03-28&20260328001919&notanumber"]),
    )
    recs = await cmd.list_files("2026-03-28")
    assert recs[0].size_kb == 0


@pytest.mark.asyncio
async def test_list_dirs_drops_traversal_names(cmd, monkeypatch):
    monkeypatch.setattr(
        cmd, "_send", _FakeSend(["MCU&DIRS&2026-03-28", "MCU&DIRS&../.."]),
    )
    assert await cmd.list_dirs() == ["2026-03-28"]


@pytest.mark.asyncio
async def test_authenticate_requires_a_key(cmd):
    with pytest.raises(ValueError):
        await cmd.authenticate("")
