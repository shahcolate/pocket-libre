"""BLE download: disconnects mid-transfer must retry, never save a short file."""

import asyncio

import pytest
from bleak.exc import BleakError
from click.testing import CliRunner

from pocket_libre import commands
from pocket_libre.commands import PocketCommander, Recording, download_with_retry
from pocket_libre.protocol import MP3_SYNC_WORD

REC = Recording("2026-10-03", "20261003090502", 0)


def _flat(result) -> str:
    """The command's output with whitespace collapsed: the console wraps lines
    at 80 columns, and a long temp path moves where the breaks fall."""
    return " ".join(result.output.split())


class _FakeClient:
    """Feeds audio chunks, optionally dropping the link part-way through.

    After a disconnect, bleak raises from stop_notify because the services
    are gone — the real failure seen on firmware 1.7.
    """

    def __init__(self, cmd, chunks, disconnect=False):
        self.cmd = cmd
        self.chunks = chunks
        self.disconnect = disconnect

    async def start_notify(self, char, callback):
        def deliver():
            for chunk in self.chunks:
                callback(None, bytearray(chunk))
            if self.disconnect:
                self.cmd._on_disconnect(self)

        # Arrive once download_ble is waiting, as notifications do.
        asyncio.get_running_loop().call_soon(deliver)

    async def stop_notify(self, char):
        if self.cmd._disconnected:
            raise BleakError("Service Discovery has not been performed yet")


def _commander(chunks, expected, disconnect=False):
    cmd = PocketCommander("AA:BB:CC:DD:EE:FF")
    cmd.client = _FakeClient(cmd, chunks, disconnect)

    async def fake_send(command, verbose=False):
        return [f"MCU&U&{expected}"]

    cmd._send = fake_send
    return cmd


# ── download_ble ────────────────────────────────


@pytest.mark.asyncio
async def test_download_ble_survives_disconnect():
    cmd = _commander([MP3_SYNC_WORD + b"\x00" * 98], expected=1000, disconnect=True)
    data = await cmd.download_ble(REC)
    assert len(data) == 100
    assert cmd.last_expected_size == 1000


@pytest.mark.asyncio
async def test_download_ble_records_expected_size_on_success():
    payload = MP3_SYNC_WORD + b"\x00" * 98
    cmd = _commander([payload], expected=len(payload))
    assert await cmd.download_ble(REC) == payload
    assert cmd.last_expected_size == len(payload)


# ── Duplicated notifications on firmware 1.8.9 (#42) ──


def _blocks(audio: bytes, duplicate: bool) -> list[bytes]:
    """Split audio as the device does: 4096-byte blocks of 244-byte
    notifications closed by a short one, which 1.8.9 sends twice."""
    chunks = []
    for start in range(0, len(audio), 4096):
        block = audio[start:start + 4096]
        packets = [block[i:i + 244] for i in range(0, len(block), 244)]
        chunks.extend(packets)
        if duplicate and len(block) == 4096:
            chunks.append(packets[-1])
    return chunks


# Distinct content per byte, so a wrongly dropped packet shows.
AUDIO = MP3_SYNC_WORD + bytes(i * 7 % 251 for i in range(3 * 4096 + 1000 - 2))


@pytest.mark.asyncio
async def test_duplicated_block_tail_is_dropped():
    chunks = _blocks(AUDIO, duplicate=True)
    raw = sum(map(len, chunks))
    assert raw == len(AUDIO) + 3 * 192
    cmd = _commander(chunks, expected=raw)
    assert await cmd.download_ble(REC) == AUDIO
    assert cmd.last_received_size == raw
    assert cmd.last_duplicate_bytes == 3 * 192


@pytest.mark.asyncio
@pytest.mark.parametrize("audio", [AUDIO, MP3_SYNC_WORD + b"\x00" * (5 * 4096)],
                         ids=["audio", "silence"])
async def test_nothing_dropped_without_duplicates(audio):
    """Silence repeats itself; only a copy of a block's closing packet goes."""
    chunks = _blocks(audio, duplicate=False)
    cmd = _commander(chunks, expected=len(audio))
    assert await cmd.download_ble(REC) == audio
    assert cmd.last_duplicate_bytes == 0


@pytest.mark.asyncio
async def test_silent_duplicates_are_dropped():
    audio = MP3_SYNC_WORD + b"\x00" * (2 * 4096)
    chunks = _blocks(audio, duplicate=True)
    cmd = _commander(chunks, expected=sum(map(len, chunks)))
    assert await cmd.download_ble(REC) == audio


@pytest.mark.asyncio
async def test_size_check_counts_the_dropped_duplicates(monkeypatch):
    """The announced size includes the duplicates: the cleaned file is
    shorter than it and still verified."""
    chunks = _blocks(AUDIO, duplicate=True)
    raw = sum(map(len, chunks))

    class _Commander(PocketCommander):
        def __init__(self, address):
            super().__init__(address)
            self.client = _FakeClient(self, chunks)

            async def fake_send(command, verbose=False):
                return [f"MCU&U&{raw}"]

            self._send = fake_send

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def authenticate(self, key):
            return True

    monkeypatch.setattr(commands, "PocketCommander", _Commander)
    assert await commands.download_checked("addr", "key", REC, retry_delay=0) == (AUDIO, True)


# ── download_with_retry ─────────────────────────


class _FakeCommander:
    """Stands in for a fresh BLE connection per attempt."""

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

    async def download_ble(self, recording, progress_callback=None):
        data, expected, disconnected = _FakeCommander.attempts.pop(0)
        self.last_expected_size = expected
        self.last_received_size = len(data)
        self._disconnected = disconnected
        return data


@pytest.fixture
def fake_commander(monkeypatch):
    monkeypatch.setattr(commands, "PocketCommander", _FakeCommander)
    return _FakeCommander


FULL = MP3_SYNC_WORD + b"\x00" * 998


@pytest.mark.asyncio
async def test_retry_after_disconnect(fake_commander):
    fake_commander.attempts = [(FULL[:400], 1000, True), (FULL, 1000, False)]
    data = await download_with_retry("addr", "key", REC, retry_delay=0)
    assert data == FULL
    assert fake_commander.attempts == []


@pytest.mark.asyncio
async def test_retry_when_short_of_announced_size(fake_commander):
    """A stalled transfer ends without a disconnect; the device's announced
    size is what reveals it — the duration estimate is 0 for `download`."""
    fake_commander.attempts = [(FULL[:900], 1000, False), (FULL, 1000, False)]
    assert await download_with_retry("addr", "key", REC, retry_delay=0) == FULL


@pytest.mark.asyncio
async def test_gives_up_with_nothing_rather_than_partial(fake_commander):
    fake_commander.attempts = [(FULL[:400], 1000, True)] * 3
    assert await download_with_retry("addr", "key", REC, retry_delay=0) == b""


# ── download-all ────────────────────────────────


def test_download_all_never_writes_a_failed_download(tmp_path, monkeypatch):
    from pocket_libre import cli

    class _Lister(_FakeCommander):
        async def list_all_recordings(self):
            return [REC]

    async def failing_download(*args, **kwargs):
        return b"", False

    monkeypatch.setattr(cli, "PocketCommander", _Lister)
    monkeypatch.setattr(commands, "download_checked", failing_download)
    monkeypatch.setattr(cli, "load_config", lambda: {})

    result = CliRunner().invoke(cli.cli, [
        "download-all", "--address", "addr", "--key", "k" * 16,
        "--output-dir", str(tmp_path),
    ])

    assert result.exit_code == 1, result.output
    assert "0 downloaded, 1 failed" in _flat(result)
    assert not (tmp_path / REC.date / REC.filename).exists()


EARLY = Recording("2026-10-02", "20261002110000", 0)
LATE = Recording("2026-10-03", "20261003160000", 0)


def _run_download_all(tmp_path, monkeypatch, recs, args=()):
    """download-all over `recs` (listed in that order); returns the result and
    the recordings it downloaded, in order."""
    from pocket_libre import cli

    fetched = []

    class _Lister(_FakeCommander):
        async def list_all_recordings(self):
            return list(recs)

    async def download(address, key, rec, progress_callback=None):
        fetched.append(rec)
        return FULL, True

    monkeypatch.setattr(cli, "PocketCommander", _Lister)
    monkeypatch.setattr(commands, "download_checked", download)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    result = CliRunner().invoke(cli.cli, [
        "download-all", "--address", "addr", "--key", "k" * 16,
        "--output-dir", str(tmp_path), *args,
    ])
    return result, fetched


def test_download_all_counts_only_what_it_downloaded(tmp_path, monkeypatch):
    (tmp_path / REC.date).mkdir()
    (tmp_path / REC.date / REC.filename).write_bytes(FULL)

    result, fetched = _run_download_all(tmp_path, monkeypatch, [EARLY, REC, LATE])

    assert result.exit_code == 0, result.output
    assert fetched == [EARLY, LATE]  # the existing copy left alone
    assert "2 recording(s) to download (1 already downloaded)" in _flat(result)
    assert "2 downloaded, 0 failed" in _flat(result)
    assert "skipping" not in _flat(result)


def test_download_all_counts_a_failed_write_and_goes_on(tmp_path, monkeypatch):
    """A full disk fails that file, not the run, and leaves no partial .mp3."""
    from pathlib import Path

    real_write = Path.write_bytes

    def write_bytes(self, data):
        if EARLY.timestamp in self.name:
            real_write(self, data[:10])
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    result, fetched = _run_download_all(tmp_path, monkeypatch, [EARLY, LATE])

    assert result.exit_code == 1, result.output
    assert fetched == [EARLY, LATE]
    assert "No space left on device" in _flat(result)
    assert "1 downloaded, 1 failed" in _flat(result)
    assert not list((tmp_path / EARLY.date).glob("*.mp3*"))  # no .mp3, no .part
    assert commands.needs_download(tmp_path / EARLY.date / EARLY.filename)  # tried again
    assert (tmp_path / LATE.date / LATE.filename).read_bytes() == FULL


def test_download_all_says_once_when_everything_is_downloaded(tmp_path, monkeypatch):
    for rec in (EARLY, LATE):
        (tmp_path / rec.date).mkdir()
        (tmp_path / rec.date / rec.filename).write_bytes(FULL)

    result, fetched = _run_download_all(tmp_path, monkeypatch, [LATE, EARLY])

    assert result.exit_code == 0, result.output
    assert fetched == []
    assert "2 recording(s) to consider, all already downloaded." in _flat(result)


def test_download_all_on_an_empty_device(tmp_path, monkeypatch):
    result, fetched = _run_download_all(tmp_path, monkeypatch, [])
    assert result.exit_code == 0, result.output
    assert "No recordings on the device." in _flat(result)
    assert fetched == []


def test_download_all_when_since_matches_nothing(tmp_path, monkeypatch):
    result, fetched = _run_download_all(tmp_path, monkeypatch, [EARLY, LATE],
                                        args=("--since", "2099-01-01"))
    assert result.exit_code == 0, result.output
    assert "No recordings from 2099-01-01 on (2 on the device)." in _flat(result)
    assert fetched == []
