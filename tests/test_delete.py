"""Deleting recordings: only ever what has a verified copy on disk."""

import json

import pytest
from click.testing import CliRunner

from pocket_libre import cli, commands
from pocket_libre.commands import (
    DOWNLOADS_FILE,
    PocketCommander,
    Recording,
    download_checked,
    has_complete_copy,
    mark_unverified,
    needs_download,
    record_download,
    save_recording,
)

DONE = Recording("2026-10-03", "20261003081846", 97)     # verified copy on disk
SHORT = Recording("2026-10-03", "20261003090502", 3684)  # copy cut short since
OLD = Recording("2026-10-03", "20261003101010", 60)      # copy from before sizes were recorded
NEW = Recording("2026-10-03", "20261003192133", 5196)    # never downloaded


@pytest.fixture(autouse=True)
def no_listing_pause(monkeypatch):
    """list_files_complete pauses a second before asking again; not needed here."""
    real_sleep = commands.asyncio.sleep

    async def sleep(seconds, *args, **kwargs):
        return await real_sleep(0)

    monkeypatch.setattr(commands.asyncio, "sleep", sleep)


def _write(root, rec, size, recorded=None, duration_s=None):
    """A copy of `rec` of `size` bytes; `recorded` is the size its download
    noted, with the duration listed then (default: `rec`'s)."""
    path = root / rec.date / rec.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff" * size)
    if recorded is not None:
        record_download(path, recorded, rec.duration_s if duration_s is None else duration_s)
    return path


def _listing(*recs, count=None):
    """A LIST answer as the device sends it, ending with MCU&LIST&<count>."""
    lines = [f"MCU&F&{r.date}&{r.timestamp}&{r.duration_s}" for r in recs]
    return [*lines, f"MCU&LIST&{len(recs) if count is None else count:03d}"]


def _commander(answers):
    """A commander whose _send answers from `answers`, a function of the command."""
    cmd = PocketCommander("AA:BB:CC:DD:EE:FF")
    sent = []

    async def send(command, verbose=False):
        sent.append(command)
        return answers(command)

    cmd._send = send
    return cmd, sent


# ── PocketCommander.delete ──────────────────────


@pytest.mark.asyncio
async def test_delete_sends_command_and_checks_listing():
    cmd, sent = _commander(lambda c: ["MCU&D"] if c.startswith("D&") else _listing(NEW))
    assert await cmd.delete(DONE) is True
    assert sent == ["D&2026-10-03&20261003081846", "LIST&2026-10-03"]


@pytest.mark.asyncio
async def test_delete_of_the_last_recording_of_a_date():
    cmd, _ = _commander(lambda c: ["MCU&D"] if c.startswith("D&") else _listing())
    assert await cmd.delete(DONE) is True


@pytest.mark.asyncio
async def test_delete_reports_a_recording_that_stayed():
    cmd, _ = _commander(lambda c: ["MCU&D"] if c.startswith("D&") else _listing(DONE))
    assert await cmd.delete(DONE) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    [],                                       # no answer in time
    _listing(NEW)[:-1],                       # cut short: no MCU&LIST&<count>
    _listing(NEW, count=2),                   # an entry missing
])
async def test_delete_without_a_complete_listing_is_unconfirmed(answer):
    cmd, sent = _commander(lambda c: ["MCU&D"] if c.startswith("D&") else answer)
    assert await cmd.delete(DONE) is None
    assert sent.count("LIST&2026-10-03") == 2  # listed again before giving up


@pytest.mark.asyncio
async def test_a_late_listing_is_asked_for_again():
    answers = iter([_listing(NEW)[:-1], _listing(NEW)])
    cmd, _ = _commander(lambda c: ["MCU&D"] if c.startswith("D&") else next(answers))
    assert await cmd.delete(DONE) is True


# ── has_complete_copy ───────────────────────────


def test_copy_with_its_recorded_size_and_duration(tmp_path):
    assert has_complete_copy(_write(tmp_path, DONE, 389_408, recorded=389_408), DONE)


def test_copy_one_byte_short_of_its_record(tmp_path):
    assert not has_complete_copy(_write(tmp_path, SHORT, 389_407, recorded=389_408), SHORT)


def test_copy_without_a_record_never_counts(tmp_path):
    """Truncated files from older versions, whatever their size."""
    assert not has_complete_copy(_write(tmp_path, OLD, 10_000_000), OLD)


def test_recording_that_grew_after_its_download_is_kept(tmp_path):
    """Downloaded while the device still listed 97 s; it lists 120 s now."""
    path = _write(tmp_path, DONE, 389_408, recorded=389_408)
    assert not has_complete_copy(path, Recording(DONE.date, DONE.timestamp, 120))


def test_reused_name_is_kept(tmp_path):
    """After a clock reset a new recording can get a name with a verified copy."""
    path = _write(tmp_path, DONE, 389_408, recorded=389_408)
    assert not has_complete_copy(path, Recording(DONE.date, DONE.timestamp, 3_600))


def test_recording_without_a_listed_duration_is_kept(tmp_path):
    path = _write(tmp_path, DONE, 389_408, recorded=389_408, duration_s=0)
    assert not has_complete_copy(path, Recording(DONE.date, DONE.timestamp, 0))


def test_recorded_copy_that_is_gone(tmp_path):
    path = _write(tmp_path, DONE, 389_408, recorded=389_408)
    path.unlink()
    assert not has_complete_copy(path, DONE)


def test_empty_copy(tmp_path):
    assert not has_complete_copy(_write(tmp_path, DONE, 0, recorded=0), DONE)


def test_unreadable_record(tmp_path):
    path = _write(tmp_path, DONE, 100)
    (path.parent / DOWNLOADS_FILE).write_text("{not json")
    assert not has_complete_copy(path, DONE)


def test_record_from_an_earlier_format_never_counts(tmp_path):
    path = _write(tmp_path, DONE, 389_408)
    (path.parent / DOWNLOADS_FILE).write_text(json.dumps({DONE.filename: 389_408}))
    assert not has_complete_copy(path, DONE)


def test_save_recording_records_size_and_duration_and_keeps_other_records(tmp_path):
    (tmp_path / DONE.date).mkdir()
    first = tmp_path / DONE.date / DONE.filename
    second = tmp_path / DONE.date / NEW.filename
    save_recording(first, b"\xff" * 10, True, DONE)
    save_recording(second, b"\xff" * 20, True, NEW)
    book = json.loads((tmp_path / DONE.date / DOWNLOADS_FILE).read_text())
    assert book == {DONE.filename: {"size": 10, "duration_s": DONE.duration_s},
                    NEW.filename: {"size": 20, "duration_s": NEW.duration_s}}
    assert has_complete_copy(first, DONE) and has_complete_copy(second, NEW)
    assert not needs_download(first)
    assert not list((tmp_path / DONE.date).glob("*.part"))


def test_unverified_data_is_marked_and_downloaded_again(tmp_path):
    (tmp_path / DONE.date).mkdir()
    path = tmp_path / DONE.date / DONE.filename
    save_recording(path, b"\xff" * 10, False, DONE)
    assert path.read_bytes() == b"\xff" * 10
    assert not has_complete_copy(path, DONE)
    assert needs_download(path)


def test_unverified_data_replaces_an_older_record(tmp_path):
    path = _write(tmp_path, DONE, 389_408, recorded=389_408)
    save_recording(path, b"\xff" * 10, False, DONE)
    assert not has_complete_copy(path, DONE)
    assert needs_download(path)


def test_a_failed_write_leaves_a_mark_and_no_partial_file(tmp_path, monkeypatch):
    """A full disk (or a crash) part way through: the next run downloads again."""
    (tmp_path / DONE.date).mkdir()
    path = tmp_path / DONE.date / DONE.filename

    def full(self, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(commands.Path, "write_bytes", full)
    with pytest.raises(OSError):
        save_recording(path, b"\xff" * 10, True, DONE)
    monkeypatch.undo()
    assert not path.exists() and not list((tmp_path / DONE.date).glob("*.part"))
    assert needs_download(path)


def test_a_copy_without_any_record_is_not_downloaded_again(tmp_path):
    """Recordings downloaded before records were kept stay as they are."""
    assert not needs_download(_write(tmp_path, OLD, 240_000))
    assert needs_download(tmp_path / NEW.date / NEW.filename)


def test_mark_unverified_before_writing(tmp_path):
    (tmp_path / NEW.date).mkdir()
    path = tmp_path / NEW.date / NEW.filename
    mark_unverified(path)
    assert needs_download(path)  # nothing there yet


def test_concurrent_records_do_not_share_a_temporary_file(tmp_path, monkeypatch):
    """Two processes saving to one date must not move each other's temp file."""
    (tmp_path / DONE.date).mkdir()
    names = []
    real_replace = commands.Path.replace

    def replace(self, target):
        names.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(commands.Path, "replace", replace)
    record_download(tmp_path / DONE.date / DONE.filename, 1, 1)
    record_download(tmp_path / DONE.date / NEW.filename, 2, 1)
    assert len(set(names)) == 2
    assert not list((tmp_path / DONE.date).glob("*.tmp"))


def test_the_record_is_readable_like_the_recordings(tmp_path):
    """mkstemp makes 0600 files; a watch service and a user's delete share this one."""
    import os
    import stat

    path = _write(tmp_path, DONE, 10, recorded=10)
    mask = os.umask(0)
    os.umask(mask)
    mode = stat.S_IMODE((path.parent / DOWNLOADS_FILE).stat().st_mode)
    assert mode == 0o666 & ~mask


class _BleCommander:
    """download_checked's view of a BLE download: one (announced, data) per attempt."""

    attempts: list = []

    def __init__(self, address):
        self._disconnected = False
        self.last_expected_size = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def authenticate(self, key):
        return True

    async def download_ble(self, rec, progress_callback=None):
        self.last_expected_size, data = _BleCommander.attempts.pop(0)
        return data


FRAMES = b"\xff\xf3" * 4_918


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts, data, verified", [
    ([(9_836, FRAMES)], FRAMES, True),                 # the announced size, exactly
    ([(9_000, FRAMES)], FRAMES, False),                # more than announced
    ([(9_840, b"\0" * 4 + FRAMES)], FRAMES, True),     # leading bytes: checked before the trim
    ([(0, FRAMES), (9_836, FRAMES)], FRAMES, True),    # size missed once: asked again
    ([(0, FRAMES)] * 3, FRAMES, False),                # missed every time: kept, unverified
])
async def test_download_is_verified_only_against_the_announced_size(monkeypatch, attempts,
                                                                    data, verified):
    monkeypatch.setattr(commands, "PocketCommander", _BleCommander)
    _BleCommander.attempts = list(attempts)
    assert await download_checked("addr", "k", Recording("d", "t", 0), retry_delay=0) == (
        data, verified)
    assert _BleCommander.attempts == []


@pytest.mark.asyncio
async def test_listing_is_asked_for_again_after_a_pause(monkeypatch):
    """Late replies to the first LIST must not mix into the second."""
    events = []
    real_sleep = commands.asyncio.sleep

    async def sleep(seconds):
        events.append(f"sleep {seconds}")
        await real_sleep(0)

    monkeypatch.setattr(commands.asyncio, "sleep", sleep)
    answers = iter([_listing(NEW)[:-1], _listing(NEW)])

    def answer(command):
        events.append(command)
        return next(answers)

    cmd, _ = _commander(answer)
    assert await cmd.list_files_complete(NEW.date) == [NEW]
    assert events[0] == "LIST&2026-10-03" and events[-1] == "LIST&2026-10-03"
    assert any(e.startswith("sleep") for e in events[1:-1])


@pytest.mark.asyncio
async def test_an_unsafe_name_does_not_make_its_date_undeletable():
    """Well-formed entries count, even ones skipped for their name."""
    unsafe = ["MCU&F&2026-10-03&../../x&97", *_listing(NEW, count=2)]
    cmd, _ = _commander(lambda c: ["MCU&D"] if c.startswith("D&") else unsafe)
    assert await cmd.delete(DONE) is True


@pytest.mark.asyncio
async def test_a_dropped_entry_makes_the_listing_incomplete():
    """A garbled line for the recording must not pass as a complete listing without it."""
    garbled = ["MCU&F&2026-10-03&2026100308", *_listing(NEW, count=2)]
    cmd, _ = _commander(lambda c: ["MCU&D"] if c.startswith("D&") else garbled)
    assert await cmd.delete(DONE) is None


# ── CLI ─────────────────────────────────────────


class _Device:
    """A fake Pocket shared by every connection the CLI opens."""

    recordings: list = []
    deleted: list = []
    confirms = True

    def __init__(self, address):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def authenticate(self, key):
        return True

    async def list_all_recordings(self):
        return list(_Device.recordings)

    async def list_files(self, date):
        return [r for r in _Device.recordings if r.date == date]

    lists = 0

    async def list_files_complete(self, date):
        _Device.lists += 1
        return await self.list_files(date)

    async def delete_and_list(self, rec):
        # The device goes by date and timestamp; `delete --date` has no duration.
        match = next(r for r in _Device.recordings if r.timestamp == rec.timestamp)
        _Device.recordings.remove(match)
        _Device.deleted.append(match)
        if not _Device.confirms:
            return None, None
        return True, await self.list_files_complete(rec.date)


@pytest.fixture
def device(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PocketCommander", _Device)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    _Device.recordings = [DONE, SHORT, OLD, NEW]
    _Device.deleted = []
    _Device.confirms = True
    _Device.lists = 0
    _write(tmp_path, DONE, 389_408, recorded=389_408)
    _write(tmp_path, SHORT, 1_000_000, recorded=14_736_000)
    _write(tmp_path, OLD, 240_000)
    return _Device


def _run(*args, input=None):
    return CliRunner().invoke(cli.cli, [*args, "--address", "addr", "--key", "k" * 16],
                              input=input)


def test_delete_downloaded_keeps_everything_without_a_matching_record(device, tmp_path):
    result = _run("delete", "--downloaded", "--yes", "--output-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert device.deleted == [DONE]
    assert device.recordings == [SHORT, OLD, NEW]


def test_delete_downloaded_asks_first(device, tmp_path):
    result = _run("delete", "--downloaded", "--output-dir", str(tmp_path), input="n\n")
    assert result.exit_code != 0
    assert device.deleted == []


def test_unconfirmed_delete_is_reported_and_fails_the_run(device, tmp_path):
    device.confirms = False
    result = _run("delete", "--downloaded", "--yes", "--output-dir", str(tmp_path))
    assert result.exit_code == 1
    assert "Could not confirm" in result.output
    assert "0 of 1 recording(s) deleted" in result.output


def test_delete_one(device):
    result = _run("delete", "--date", NEW.date, "--timestamp", NEW.timestamp, "--yes")
    assert result.exit_code == 0, result.output
    assert device.deleted == [NEW]


def test_delete_one_that_is_not_there(device):
    result = _run("delete", "--date", NEW.date, "--timestamp", "20990101000000", "--yes")
    assert result.exit_code == 1
    assert device.deleted == []


@pytest.mark.parametrize("args", [
    [],
    ["--downloaded", "--date", "2026-10-03"],
    ["--date", "2026-10-03"],
    ["--date", "..", "--timestamp", "x"],
    ["--date", "2026-10-03", "--timestamp", "x", "--since", "2026-10-01"],
])
def test_delete_rejects_bad_arguments(device, args):
    assert _run("delete", *args).exit_code == 2
    assert device.deleted == []


def test_download_all_delete_after(device, tmp_path, monkeypatch):
    async def download(address, key, rec, progress_callback=None):
        return (b"\xff" * rec.estimated_bytes, True) if rec == NEW else (b"", False)

    monkeypatch.setattr(commands, "download_checked", download)
    result = _run("download-all", "--output-dir", str(tmp_path), "--delete-after")
    assert result.exit_code == 0, result.output
    assert device.deleted == [DONE, NEW]  # NEW was downloaded, so its size was recorded
    assert device.recordings == [SHORT, OLD]


def test_wifi_transfer_delete_after_is_for_a_batch(device):
    result = _run("wifi-transfer", "--date", NEW.date, "--timestamp", NEW.timestamp,
                  "--delete-after")
    assert result.exit_code == 2
    assert device.deleted == []


def test_download_does_not_record_into_the_current_directory(device, tmp_path, monkeypatch):
    async def download(address, key, rec, progress_callback=None):
        return b"\xff" * 100

    monkeypatch.setattr(commands, "download_with_retry", download)
    monkeypatch.chdir(tmp_path)
    result = _run("download", "--date", NEW.date, "--timestamp", NEW.timestamp)
    assert result.exit_code == 0, result.output
    assert (tmp_path / NEW.filename).exists()
    assert not (tmp_path / DOWNLOADS_FILE).exists()


def test_delete_pass_that_cannot_connect_says_what_to_do(device, tmp_path, monkeypatch):
    class _Asleep(_Device):
        async def __aenter__(self):
            raise Exception("Device addr not found. Make sure it's awake.")

    monkeypatch.setattr(cli, "PocketCommander", _Asleep)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    result = _run("delete", "--downloaded", "--yes", "--output-dir", str(tmp_path))
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "delete --downloaded" in " ".join(result.output.split())
    assert device.deleted == []


def test_delete_downloaded_keeps_a_recording_that_grew(device, tmp_path):
    grown = Recording(DONE.date, DONE.timestamp, 120)  # downloaded at 97 s
    device.recordings = [grown, NEW]
    result = _run("delete", "--downloaded", "--yes", "--output-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert device.deleted == []
    assert "Keeping" in result.output


def test_a_recording_that_changes_after_the_check_is_kept(device, tmp_path, monkeypatch):
    """The copy is checked against the listing made before the confirmation;
    the listing made just before deleting must still show the same recording."""
    grown = Recording(DONE.date, DONE.timestamp, 120)  # DONE's copy is of 97 s

    def confirm(*args, **kwargs):
        device.recordings = [grown, NEW]  # changed while the prompt was open
        return True

    monkeypatch.setattr(cli.click, "confirm", confirm)
    result = _run("delete", "--downloaded", "--output-dir", str(tmp_path))
    assert result.exit_code == 1, result.output  # fewer deleted than confirmed
    assert device.deleted == []
    assert "now lists 120s" in " ".join(result.output.split())


def test_deleting_several_on_one_date_lists_it_once_per_deletion(device, tmp_path):
    second = Recording(DONE.date, "20261003081900", 50)
    _write(tmp_path, second, 200_000, recorded=200_000)
    device.recordings = [DONE, second, NEW]
    result = _run("delete", "--downloaded", "--yes", "--output-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert device.deleted == [DONE, second]
    assert device.lists == 3  # one before the first, one after each deletion
