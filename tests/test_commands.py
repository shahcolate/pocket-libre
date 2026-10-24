"""Protocol response parsing and recording-identifier safety."""

import asyncio

import pytest

from pocket_libre.commands import PocketCommander, Recording, _prefer_le_bearer, is_safe_id


@pytest.fixture
def cmd():
    """A commander instance with no BLE connection — parsing helpers only."""
    return PocketCommander("AA:BB:CC:DD:EE:FF")


@pytest.mark.asyncio
async def test_prefer_le_bearer_ignores_asyncio_timeout(monkeypatch):
    """A bluetoothctl timeout must not prevent the BLE fallback connection."""
    calls = 0

    class _TimedOutProcess:
        def __init__(self):
            self.returncode = None

        async def wait(self):
            if self.returncode is None:
                raise asyncio.TimeoutError
            return self.returncode

        def kill(self):
            self.returncode = -9

    async def create_process(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _TimedOutProcess()

    monkeypatch.setattr("pocket_libre.commands.sys.platform", "linux")
    monkeypatch.setattr("pocket_libre.commands._PREFERRED_BEARER_ADDRESSES", set())
    monkeypatch.setattr(
        "pocket_libre.commands.asyncio.create_subprocess_exec", create_process
    )

    await _prefer_le_bearer("AA:BB:CC:DD:EE:FF")
    await _prefer_le_bearer("AA:BB:CC:DD:EE:FF")
    assert calls == 1


# ── Response parsing ────────────────────────────


def test_parse_response_extracts_matching_prefix(cmd):
    assert cmd._parse_response(["MCU&BAT&87"], "BAT") == ["87"]


def test_parse_response_ignores_other_prefixes(cmd):
    responses = ["MCU&BAT&87", "MCU&FW&1.3.3", "garbage"]
    assert cmd._parse_response(responses, "FW") == ["1.3.3"]


def test_parse_response_empty_when_no_match(cmd):
    assert cmd._parse_response(["MCU&BAT&87"], "SPA") == []


def test_parse_response_keeps_embedded_separators(cmd):
    """SPACE returns "free&total" — the split must not eat the payload."""
    assert cmd._parse_response(["MCU&SPA&1024&4096"], "SPA") == ["1024&4096"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses, expected",
    [
        # A brand-new 64 GB device on firmware 1.7 (issue #11): 15 MB used.
        (["MCU&SPA&059619&059634"], (15, 59634)),
        (["MCU&SPA&0&4096"], (4096, 4096)),
        ([], (0, 0)),
        (["MCU&SPA&1024"], (0, 0)),
        (["MCU&SPA&lots&4096"], (0, 0)),
    ],
)
async def test_get_storage_reads_free_and_total_mb(cmd, monkeypatch, responses, expected):
    monkeypatch.setattr(cmd, "_send", _FakeSend(responses))
    assert await cmd.get_storage() == expected


# ── Recording ───────────────────────────────────


def test_recording_filename():
    assert Recording("2026-03-28", "20260328001919", 100).filename == "20260328001919.mp3"


def test_recording_str_includes_duration():
    assert "103m42s" in str(Recording("2026-03-28", "20260328001919", 6222))


def test_recording_str_handles_zero_duration():
    """A zero-length recording must not divide by zero or render nonsense."""
    assert "0m00s" in str(Recording("2026-03-28", "20260328001919", 0))


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
    assert recs[0].duration_s == 6222


@pytest.mark.asyncio
async def test_list_all_recordings_is_in_date_order(cmd, monkeypatch):
    """The device's order is not guaranteed; every caller gets oldest first."""
    listings = {
        "2026-10-03": ["MCU&F&2026-10-03&20261003160000&10",
                       "MCU&F&2026-10-03&20261003090000&10"],
        "2026-10-02": ["MCU&F&2026-10-02&20261002110000&10"],
    }

    async def send(command, verbose=False):
        if command == "LIST_DIRS":
            return ["MCU&DIRS&2026-10-03", "MCU&DIRS&2026-10-02"]
        return listings[command.split("&", 1)[1]]

    monkeypatch.setattr(cmd, "_send", send)
    monkeypatch.setattr("pocket_libre.commands.asyncio.sleep", _no_sleep)
    recs = await cmd.list_all_recordings()
    assert [r.timestamp for r in recs] == ["20261002110000", "20261003090000",
                                           "20261003160000"]


@pytest.mark.parametrize("names, expected", [
    # PH + yyMMddHHmmss (phone calls) sorts by its time among the usual names.
    (["20261002213000", "PH261002211958", "20261002090000"],
     ["20261002090000", "PH261002211958", "20261002213000"]),
    (["20261002120000", "PH261002080000"], ["PH261002080000", "20261002120000"]),
])
def test_recordings_sort_by_their_time_whatever_the_name_form(names, expected):
    from pocket_libre.commands import Recording

    recs = [Recording("2026-10-02", ts, 0) for ts in names]
    assert [r.timestamp for r in sorted(recs, key=lambda r: r.sort_key)] == expected


@pytest.mark.asyncio
async def test_list_all_recordings_puts_ph_names_in_time_order(cmd, monkeypatch):
    async def send(command, verbose=False):
        if command == "LIST_DIRS":
            return ["MCU&DIRS&2026-10-02"]
        return ["MCU&F&2026-10-02&20261002213000&10", "MCU&F&2026-10-02&PH261002211958&10",
                "MCU&F&2026-10-02&20261002090000&10"]

    monkeypatch.setattr(cmd, "_send", send)
    monkeypatch.setattr("pocket_libre.commands.asyncio.sleep", _no_sleep)
    recs = await cmd.list_all_recordings()
    assert [r.timestamp for r in recs] == ["20261002090000", "PH261002211958", "20261002213000"]


async def _no_sleep(seconds):
    return None


@pytest.mark.asyncio
async def test_list_files_keeps_ph_prefixed_names(cmd, monkeypatch):
    """Some recordings are named PH + YYMMDDHHmmss (issue #11, #18); they
    must not be dropped by a parser that expects a 14-digit timestamp."""
    monkeypatch.setattr(
        cmd, "_send",
        _FakeSend([
            "MCU&F&2026-10-02&20261002201252&3014",
            "MCU&F&2026-10-02&PH261002211958&346",
        ]),
    )
    recs = await cmd.list_files("2026-10-02")
    assert [r.timestamp for r in recs] == ["20261002201252", "PH261002211958"]
    assert recs[1].filename == "PH261002211958.mp3"
    assert recs[1].duration_s == 346


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
async def test_list_files_tolerates_non_numeric_duration(cmd, monkeypatch):
    monkeypatch.setattr(
        cmd, "_send", _FakeSend(["MCU&F&2026-03-28&20260328001919&notanumber"]),
    )
    recs = await cmd.list_files("2026-03-28")
    assert recs[0].duration_s == 0


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


# ── WiFi command sequencing ─────────────────────


class _RecordingSend:
    """Records the command order the caller drives."""

    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, command, verbose=False):
        self.sent.append(command)
        return []


@pytest.fixture
def kolkata():
    """Local time five and a half hours off UTC, so sending it would show.

    CI runners are on UTC, where local time and UTC agree. A POSIX zone
    string needs no tzdata; the check below makes sure it took effect, so the
    tests can't pass vacuously on a machine that ignores it.
    """
    import os
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() is not available on Windows")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "IST-05:30"
    time.tzset()
    try:
        assert time.timezone == -19800, "the local zone did not change"
        yield
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.mark.asyncio
async def test_set_time_sends_utc(cmd, monkeypatch, kolkata):
    """The vendor app sets the clock to UTC; recordings are named after it."""
    from datetime import datetime, timezone

    recorder = _RecordingSend()
    monkeypatch.setattr(cmd, "_send", recorder)
    before = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    await cmd.set_time()
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    sent = datetime.strptime(recorder.sent[0], "T&%Y%m%d%H%M%S")
    assert before <= sent <= after


@pytest.mark.asyncio
@pytest.mark.parametrize("when", [
    "2026-10-06T17:05:31-06:00",  # aware, in another zone
    "2026-10-06T23:05:31+00:00",  # aware, already UTC
    "2026-10-07T04:35:31",        # naive: local time (Asia/Kolkata here)
])
async def test_set_time_converts_a_given_time_to_utc(cmd, monkeypatch, kolkata, when):
    from datetime import datetime

    recorder = _RecordingSend()
    monkeypatch.setattr(cmd, "_send", recorder)
    await cmd.set_time(datetime.fromisoformat(when))
    assert recorder.sent == ["T&20261006230531"]


@pytest.mark.asyncio
async def test_wifi_trigger_and_enable_are_separable(cmd, monkeypatch):
    """Credentials must be readable between triggering WiFi mode and
    bringing the AP up — the order the vendor app uses (PROTOCOL.md)."""
    recorder = _RecordingSend()
    monkeypatch.setattr(cmd, "_send", recorder)

    await cmd.wifi_trigger()
    await cmd.wifi_get_credentials()
    await cmd.wifi_enable()

    assert recorder.sent == ["U&WIFI", "WIFI", "WIFIO"]


@pytest.mark.asyncio
async def test_wifi_start_still_bundles_both_steps(cmd, monkeypatch):
    recorder = _RecordingSend()
    monkeypatch.setattr(cmd, "_send", recorder)
    await cmd.wifi_start()
    assert recorder.sent == ["U&WIFI", "WIFIO"]


# ── USB mass storage ────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, command", [(True, "USB&1"), (False, "USB&0")])
async def test_set_usb_sends_flag(cmd, monkeypatch, enabled, command):
    recorder = _RecordingSend()
    monkeypatch.setattr(cmd, "_send", recorder)
    await cmd.set_usb(enabled)
    assert recorder.sent == [command]


@pytest.mark.asyncio
async def test_get_usb_sends_get_command(cmd, monkeypatch):
    recorder = _RecordingSend()
    monkeypatch.setattr(cmd, "_send", recorder)
    await cmd.get_usb()
    assert recorder.sent == ["GET&USB"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses, expected",
    [
        (["MCU&USB&1"], True),
        (["MCU&USB&0"], False),
        ([], None),
        (["MCU&USB&maybe"], None),
        (["MCU&BAT&87"], None),
    ],
)
async def test_get_usb_parses_state(cmd, monkeypatch, responses, expected):
    monkeypatch.setattr(cmd, "_send", _FakeSend(responses))
    assert await cmd.get_usb() is expected


# ── Recording field semantics ───────────────────
#
# The MCU&F trailing field is a duration in seconds, not a size in KB.
# Confirmed twice against firmware 1.8 hardware in
# https://github.com/shahcolate/pocket-libre/issues/4


def test_recording_duration_estimates_size_at_32kbps():
    from pocket_libre.commands import Recording

    # 1663 s of audio was reported by the device as 6,653,128 bytes.
    rec = Recording("2026-09-03", "20260903145856", 1663)
    assert abs(rec.estimated_bytes - 6_653_128) < 6_653_128 * 0.01


def test_recording_str_reports_duration_not_kilobytes():
    from pocket_libre.commands import Recording

    assert "27m43s" in str(Recording("2026-09-03", "20260903145856", 1663))


def test_recording_handles_zero_duration():
    from pocket_libre.commands import Recording

    rec = Recording("2026-09-03", "20260903145856", 0)
    assert rec.estimated_bytes == 0
    assert "0m00s" in str(rec)
