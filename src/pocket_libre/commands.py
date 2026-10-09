"""BLE command protocol for the Pocket AI recorder.

Implements the APP&/MCU& text command protocol decoded from PacketLogger captures.
Commands are written to CMD_WRITE_CHAR, responses arrive on CMD_NOTIFY_CHAR.

Usage:
    async with PocketCommander(address) as cmd:
        await cmd.authenticate(session_key)
        dirs = await cmd.list_dirs()
        files = await cmd.list_files("2026-03-28")
        wifi = await cmd.wifi_get_credentials()
"""

import asyncio
import json
import os
import re
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient
from bleak.exc import BleakError
from rich.console import Console

from pocket_libre.protocol import (
    AUDIO_NOTIFY_CHAR,
    BYTES_PER_SECOND,
    CMD_NOTIFY_CHAR,
    CMD_PREFIX,
    CMD_WRITE_CHAR,
    RSP_PREFIX,
    WIFI_STATUS_READY,
)

console = Console()

# Recording identifiers become path components on disk, so they must not
# contain separators or dot-segments. The device is not a trusted input:
# a malfunctioning or spoofed peer could return "../" in a LIST response.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def is_safe_id(value: str) -> bool:
    """True if `value` is safe to use as a single filesystem path component."""
    return bool(value) and value not in (".", "..") and bool(_ID_PATTERN.match(value))


def split_messages(text: str) -> list[str]:
    """One notification can carry several MCU& messages back to back
    (e.g. "MCU&WIFIOMCU&OFF"); split them, dropping padding NULs."""
    text = text.replace("\0", "").strip()
    return [p.strip() for p in re.split(r"(?=MCU&)", text) if p.strip()]


@dataclass
class Recording:
    """A recording stored on the device."""
    date: str          # e.g. "2026-03-28"
    timestamp: str     # e.g. "20260328001919"
    duration_s: int    # recording length in SECONDS, from the LIST response

    @property
    def filename(self) -> str:
        return f"{self.timestamp}.mp3"

    @property
    def sort_key(self) -> tuple[str, str]:
        """(date, start time as YYYYMMDDHHmmss), for oldest-first order.

        PH + YYMMDDHHmmss names (phone calls) are compared by the time they
        carry, not as text, which would put them after every other name.
        """
        ts = self.timestamp
        return self.date, "20" + ts[2:] if ts.startswith("PH") else ts

    @property
    def estimated_bytes(self) -> int:
        """Approximate size on disk, derived from duration at 32 kbps."""
        return max(self.duration_s, 0) * BYTES_PER_SECOND

    def __str__(self) -> str:
        mins, secs = divmod(max(self.duration_s, 0), 60)
        return (
            f"{self.date}/{self.timestamp} "
            f"({mins}m{secs:02d}s, ~{self.estimated_bytes:,} bytes)"
        )


# Next to the recordings of each date, what each download wrote there:
# {"<timestamp>.mp3": {"size": <bytes>, "duration_s": <seconds listed>}} for a
# verified download, {"<timestamp>.mp3": {"verified": false}} for one that
# couldn't be checked (or is being written).
DOWNLOADS_FILE = ".downloads.json"


def _downloads(directory: Path) -> dict:
    try:
        entries = json.loads((directory / DOWNLOADS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return entries if isinstance(entries, dict) else {}


def _set_entry(path: Path, entry: dict) -> None:
    entries = _downloads(path.parent)
    entries[path.name] = entry
    # A temp name of its own: another process may be saving to this date too.
    # If both do, one record can be lost, which only keeps a recording. Opened
    # like any other new file, so it gets the usual mode: a service saving
    # recordings and a user deleting them can share it.
    tmp = path.parent / f"{DOWNLOADS_FILE}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "x") as fh:
            fh.write(json.dumps(entries, indent=1, sort_keys=True) + "\n")
        tmp.replace(path.parent / DOWNLOADS_FILE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def record_download(path, size: int, duration_s: int) -> None:
    """Note that a verified download of a recording listed with `duration_s`
    wrote `size` bytes to `path`.

    Only a download that checked the size the device announces may call
    this; has_complete_copy() relies on it before deleting from the device.
    """
    _set_entry(Path(path), {"size": size, "duration_s": duration_s})


def mark_unverified(path) -> None:
    """Note that `path` holds (or is about to hold) a download that wasn't
    checked against the announced size: never deleted, downloaded again."""
    _set_entry(Path(path), {"verified": False})


def save_recording(path, data: bytes, verified: bool, recording: Recording) -> None:
    """Write a download to `path`, and record it if `verified` (see
    download_checked).

    The file is marked unverified first and written through a .part file, so
    a crash or full disk at any point leaves either that mark (the next run
    downloads it again) or a complete, recorded copy.
    """
    path = Path(path)
    partial = path.with_name(path.name + ".part")
    mark_unverified(path)
    try:
        partial.write_bytes(data)
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if verified:
        record_download(path, len(data), recording.duration_s)


def needs_download(path) -> bool:
    """True if `path` is missing, or holds a download marked unverified.

    A file without any record (one from before records were kept) counts as
    downloaded, as it always has.
    """
    path = Path(path)
    if not path.exists():
        return True
    entry = _downloads(path.parent).get(path.name)
    return isinstance(entry, dict) and entry.get("verified") is False


def has_complete_copy(path, listed: Recording) -> bool:
    """True if `path` is a verified download of the recording the device lists
    now as `listed`.

    The file must have exactly the size its download recorded, and the device
    must list the same duration it did then, so a recording that grew after
    it was downloaded, or a name reused for a new recording, is kept. A file
    without a record (downloaded before records were kept, which older
    versions may have saved cut short, or put there by something else) never
    counts, whatever its size. Deleting from the device can't be undone, so
    this errs towards keeping recordings.
    """
    path = Path(path)
    entry = _downloads(path.parent).get(path.name)
    if not isinstance(entry, dict):
        return False
    size, duration_s = entry.get("size"), entry.get("duration_s")
    if not (isinstance(size, int) and size > 0 and isinstance(duration_s, int)
            and duration_s > 0 and duration_s == listed.duration_s):
        return False
    try:
        return path.stat().st_size == size
    except OSError:
        return False


SESSION_KEY_ALPHABET = string.ascii_uppercase + string.digits


def generate_session_key() -> str:
    """A new random 16-character session key, for pairing a reset device."""
    return "".join(secrets.choice(SESSION_KEY_ALPHABET) for _ in range(16))


def is_valid_session_key(key: str) -> bool:
    """16 letters and digits, the form the vendor app uses."""
    return len(key) == 16 and key.isascii() and key.isalnum()


class PocketCommander:
    """Send commands to a Pocket device and collect responses."""

    def __init__(self, address: str, timeout: float = 20.0):
        self.address = address
        self.timeout = timeout
        self.client: BleakClient | None = None
        self._responses: list[str] = []
        self._response_event = asyncio.Event()
        self._audio_data = bytearray()
        self._audio_event = asyncio.Event()
        self._disconnected = False
        # Every MCU& message received, with its arrival time, in order. Unlike
        # _responses this is never cleared, so replies that arrive on their own
        # schedule (WIFIS changes, MCU&U&WIFI, MCU&OFF) can be waited for with
        # mark() / wait_for_message().
        self.messages: list[tuple[float, str]] = []
        self._message_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self.discarded_audio_bytes = 0
        # Size announced by MCU&U for the most recent download_ble.
        self.last_expected_size = 0

    async def __aenter__(self):
        # Scan first to ensure the device is discovered by CoreBluetooth
        from bleak import BleakScanner
        device = await BleakScanner.find_device_by_address(
            self.address, timeout=self.timeout
        )
        if device is None:
            raise Exception(
                f"Device {self.address} not found. "
                "Make sure it's awake (press the button) and nearby."
            )
        self._disconnected = False
        self.client = BleakClient(
            device,
            timeout=self.timeout,
            disconnected_callback=self._on_disconnect,
        )
        await self.client.connect()

        # Subscribe to command responses
        await self.client.start_notify(
            CMD_NOTIFY_CHAR, self._on_response
        )
        return self

    async def __aexit__(self, *args):
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(CMD_NOTIFY_CHAR)
            except Exception:
                pass
            await self.client.disconnect()

    def _on_disconnect(self, client: BleakClient):
        self._disconnected = True
        self._response_event.set()
        self._audio_event.set()
        self._message_event.set()

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected and not self._disconnected)

    def _on_response(self, sender: int, data: bytearray):
        text = data.decode("ascii", errors="replace")
        now = time.monotonic()
        for part in split_messages(text) or [text]:
            self._responses.append(part)
            self.messages.append((now, part))
        self._response_event.set()
        self._message_event.set()

    def _discard_audio(self, sender: int, data: bytearray):
        self.discarded_audio_bytes += len(data)

    async def _write(self, command: str) -> None:
        payload = f"{CMD_PREFIX}{command}".encode("ascii")
        async with self._write_lock:
            await self.client.write_gatt_char(CMD_WRITE_CHAR, payload, response=False)

    # ── Asynchronous replies ─────────────────────

    def mark(self) -> int:
        """A position in `messages`; wait_for_message(since=mark) only looks after it."""
        return len(self.messages)

    async def send_nowait(self, command: str) -> int:
        """Write APP&<command> without collecting replies. Returns the mark before it."""
        since = self.mark()
        await self._write(command)
        return since

    async def wait_for_message(self, name: str, since: int, timeout: float,
                               accept=None) -> str | None:
        """The value of the first "MCU&<name>&<value>" (or bare "MCU&<name>")
        received after `since` and passing `accept`, or None on timeout or
        disconnect."""
        exact = f"{RSP_PREFIX}{name}"
        prefix = exact + "&"
        deadline = time.monotonic() + timeout
        index = since
        while True:
            while index < len(self.messages):
                text = self.messages[index][1]
                index += 1
                if text == exact:
                    value = ""
                elif text.startswith(prefix):
                    value = text[len(prefix):]
                else:
                    continue
                if accept is None or accept(value):
                    return value
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._disconnected:
                return None
            self._message_event.clear()
            try:
                await asyncio.wait_for(self._message_event.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    async def request(self, command: str, answer: str, timeout: float = 5.0,
                      accept=None) -> str | None:
        """Send APP&<command> and wait for the value of "MCU&<answer>&...". """
        since = await self.send_nowait(command)
        return await self.wait_for_message(answer, since, timeout, accept=accept)

    async def start_audio_sink(self) -> None:
        """Subscribe to the audio channel and throw the data away.

        A Bluetooth transfer only runs while something is subscribed to the
        audio characteristic — without a subscriber the device ends it at once
        with MCU&OFF. The WiFi switch (APP&U&WIFI) needs a running Bluetooth
        transfer to switch, so the WiFi path subscribes and discards.
        """
        await self.client.start_notify(AUDIO_NOTIFY_CHAR, self._discard_audio)

    async def stop_audio_sink(self) -> None:
        try:
            await self.client.stop_notify(AUDIO_NOTIFY_CHAR)
        except Exception:
            pass

    def _on_audio(self, sender: int, data: bytearray):
        self._audio_data.extend(data)
        self._audio_event.set()

    async def _send(self, command: str, verbose: bool = False) -> list[str]:
        """Send an APP& command and collect MCU& responses."""
        self._responses.clear()
        self._response_event.clear()

        if verbose:
            console.print(f"  [cyan]>>> APP&{command}[/cyan]")
        await self._write(command)

        # Wait for response(s) — some commands return multiple lines
        await asyncio.sleep(0.3)
        # Give extra time for multi-line responses
        for _ in range(10):
            self._response_event.clear()
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                break

        if verbose:
            for r in self._responses:
                console.print(f"  [green]<<< {r}[/green]")
            if not self._responses:
                console.print("  [dim]<<< (no response)[/dim]")

        return list(self._responses)

    def _parse_response(self, responses: list[str], prefix: str) -> list[str]:
        """Extract response values matching a prefix like 'MCU&BAT&'."""
        full = f"{RSP_PREFIX}{prefix}&"
        return [r[len(full):] for r in responses if r.startswith(full)]

    # ── Device Info ──────────────────────────────

    async def authenticate(self, session_key: str) -> bool:
        if not session_key:
            raise ValueError("No session key configured.")
        return await self.login(session_key) is True

    async def login(self, session_key: str) -> bool | None:
        """APP&SK&<key>: True for MCU&SK&OK, False for MCU&SK&ERR, None if no
        answer arrived in time (which is not a refusal).

        After a hardware reset the device takes the first key it is sent and
        refuses every other one from then on, dropping the connection after
        MCU&SK&ERR (see PROTOCOL.md, "Session key").
        """
        responses = await self._send(f"SK&{session_key}")
        if any("MCU&SK&OK" in r for r in responses):
            return True
        if any("MCU&SK&ERR" in r for r in responses):
            return False
        return None

    async def get_battery(self) -> int:
        responses = await self._send("BAT")
        vals = self._parse_response(responses, "BAT")
        return int(vals[0]) if vals else -1

    async def get_firmware(self) -> str:
        responses = await self._send("FW")
        vals = self._parse_response(responses, "FW")
        return vals[0].strip() if vals else "unknown"

    async def get_storage(self) -> tuple[int, int]:
        """Returns (used_mb, total_mb).

        The device answers MCU&SPA&<free>&<total> in MB, so used is derived.
        """
        responses = await self._send("SPACE")
        vals = self._parse_response(responses, "SPA")
        if vals:
            parts = vals[0].split("&")
            if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
                free, total = int(parts[0]), int(parts[1])
                return max(total - free, 0), total
        return 0, 0

    async def get_state(self) -> int:
        """0 = idle, other values TBD."""
        responses = await self._send("STE")
        vals = self._parse_response(responses, "STE")
        return int(vals[0]) if vals else -1

    async def set_time(self, when: datetime | None = None) -> bool:
        """Set the device clock to `when` (default: now), sent in UTC.

        The vendor app sends UTC too. Recordings are named after this clock, so
        sending local time here would leave their names in whichever zone was
        set last. A naive `when` is taken as local time, as Python does.
        """
        when = datetime.now(timezone.utc) if when is None else when.astimezone(timezone.utc)
        responses = await self._send(f"T&{when.strftime('%Y%m%d%H%M%S')}")
        return any("MCU&T&OK" in r for r in responses)

    # ── USB Mass Storage ─────────────────────────

    def _parse_usb_state(self, responses: list[str]) -> bool | None:
        """MCU&USB&1 → True, MCU&USB&0 → False, anything else → None."""
        vals = self._parse_response(responses, "USB")
        if vals and vals[-1].strip() in ("0", "1"):
            return vals[-1].strip() == "1"
        return None

    async def get_usb(self) -> bool | None:
        """Whether the device exposes its storage as a USB drive. None if unknown."""
        responses = await self._send("GET&USB")
        return self._parse_usb_state(responses)

    async def set_usb(self, enabled: bool) -> bool | None:
        """Enable or disable USB mass storage. Returns the state the device reports."""
        responses = await self._send(f"USB&{1 if enabled else 0}")
        return self._parse_usb_state(responses)

    # ── File Listing ─────────────────────────────

    async def list_dirs(self) -> list[str]:
        """List recording dates on device. Returns ['2026-03-26', '2026-03-27', ...]."""
        responses = await self._send("LIST_DIRS")
        return [d for d in self._parse_response(responses, "DIRS") if is_safe_id(d)]

    async def list_files(self, date: str) -> list[Recording]:
        """List recordings for a date. Returns list of Recording objects."""
        recordings, _ = self._parse_listing(await self._send(f"LIST&{date}"))
        return recordings

    async def list_files_complete(self, date: str, tries: int = 2) -> list[Recording] | None:
        """List recordings for a date, or None if no complete listing arrived.

        A listing is complete when it ends with MCU&LIST&<count> and the count
        matches the MCU&F entries. Without that, an empty or short answer
        can't be told from a reply that came too late for _send.
        """
        for attempt in range(tries):
            if attempt:
                # Let a late answer to the last LIST arrive, so _send drops it
                # instead of mixing it into the next one.
                await asyncio.sleep(1.0)
            recordings, complete = self._parse_listing(await self._send(f"LIST&{date}"))
            if complete:
                return recordings
        return None

    @staticmethod
    def _parse_listing(responses: list[str]) -> tuple[list[Recording], bool]:
        """The recordings in a LIST answer, and whether the answer was complete.

        Every well-formed entry counts towards completeness, including one
        skipped for an unsafe name, so such a name can't make its date's
        listing incomplete for good. A malformed line makes it incomplete
        rather than silently shorter.
        """
        recordings = []
        well_formed = 0
        count = None
        for r in responses:
            if r.startswith(f"{RSP_PREFIX}LIST&"):
                value = r[len(f"{RSP_PREFIX}LIST&"):].strip()
                count = int(value) if value.isdigit() else None
                continue
            if not r.startswith(f"{RSP_PREFIX}F&"):
                continue
            # MCU&F&2026-03-28&20260328001919&6222
            # The trailing field is a duration in seconds (see protocol.py).
            parts = r.split("&")
            if len(parts) < 5:
                continue
            well_formed += 1
            rec_date = parts[2]
            timestamp = parts[3]
            if not (is_safe_id(rec_date) and is_safe_id(timestamp)):
                console.print(
                    f"[yellow]Skipping recording with unsafe name: "
                    f"{rec_date}/{timestamp}[/yellow]"
                )
                continue
            duration_s = int(parts[4]) if parts[4].isdigit() else 0
            recordings.append(Recording(rec_date, timestamp, duration_s))
        return recordings, count == well_formed

    async def list_all_recordings(self) -> list[Recording]:
        """List all recordings across all dates, oldest first.

        Sorted by date and start time (Recording.sort_key), whatever order the
        device lists them in.
        """
        dirs = await self.list_dirs()
        all_recs = []
        for d in dirs:
            recs = await self.list_files(d)
            all_recs.extend(recs)
            # Brief pause between directory listings to avoid overwhelming device MCU
            if len(dirs) > 1:
                await asyncio.sleep(0.2)
        return sorted(all_recs, key=lambda r: r.sort_key)

    # ── Deleting ─────────────────────────────────

    async def delete(self, recording: Recording) -> bool | None:
        """Delete a recording from the device.

        True once it is gone from a complete listing of its date, False if it
        is still listed, None if no complete listing came back to tell.
        APP&D&<date>&<timestamp> is answered by a bare MCU&D that carries no
        status, so the listing is the only confirmation.
        """
        gone, _ = await self.delete_and_list(recording)
        return gone

    async def delete_and_list(self, recording: Recording
                              ) -> tuple[bool | None, list[Recording] | None]:
        """delete(), plus the complete listing of the date it was checked
        against (None without one), for a caller deleting several."""
        await self._send(f"D&{recording.date}&{recording.timestamp}")
        remaining = await self.list_files_complete(recording.date)
        if remaining is None:
            return None, None
        return all(r.timestamp != recording.timestamp for r in remaining), remaining

    # ── BLE File Transfer ────────────────────────

    async def download_ble(
        self,
        recording: Recording,
        progress_callback=None,
    ) -> bytes:
        """Download a recording over BLE. Returns MP3 bytes."""
        # Subscribe to audio notifications
        self._audio_data.clear()
        self.last_expected_size = 0
        await self.client.start_notify(AUDIO_NOTIFY_CHAR, self._on_audio)

        # Request the file
        cmd = f"U&{recording.date}&{recording.timestamp}"
        responses = await self._send(cmd)

        # Parse expected size from MCU&U&<size_bytes>
        expected_size = 0
        for r in responses:
            if r.startswith(f"{RSP_PREFIX}U&") and not r.startswith(f"{RSP_PREFIX}U&WIFI"):
                try:
                    expected_size = int(r.split("&")[-1])
                except ValueError:
                    pass

        self.last_expected_size = expected_size
        if expected_size > 0:
            console.print(f"[dim]Expected size: {expected_size:,} bytes[/dim]")

        # Collect audio data until transfer completes
        stall_count = 0
        while True:
            if self._disconnected:
                console.print("[yellow]BLE disconnected during transfer.[/yellow]")
                break

            self._audio_event.clear()
            try:
                await asyncio.wait_for(self._audio_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                stall_count += 1
                if stall_count >= 5:
                    break
                continue

            stall_count = 0
            current_size = len(self._audio_data)

            if progress_callback:
                progress_callback(current_size, expected_size)

            if expected_size > 0 and current_size >= expected_size:
                break

        # After a disconnect bleak has dropped the services, so stop_notify
        # would raise and lose the data — let the caller decide what to keep.
        if not self._disconnected:
            try:
                await self.client.stop_notify(AUDIO_NOTIFY_CHAR)
            except BleakError:
                pass
        return bytes(self._audio_data)

    # ── WiFi Transfer ────────────────────────────
    #
    # Single-step wrappers. The working firmware 1.8 transfer, with its
    # ordering and timing rules, is wifi.WifiSession, which uses
    # request() / wait_for_message() instead of these.

    async def wifi_get_credentials(self) -> tuple[str, str] | None:
        """Get WiFi AP credentials. Returns (ssid, password) or None."""
        responses = await self._send("WIFI")
        for r in responses:
            # MCU&WIFI&<ssid>&<password>
            if r.startswith(f"{RSP_PREFIX}WIFI&"):
                parts = r.split("&")
                if len(parts) >= 4:
                    return parts[2], parts[3]
        return None

    async def wifi_trigger(self) -> bool:
        """APP&U&WIFI. Before a transfer it gets no answer on firmware 1.8;
        during a running Bluetooth transfer it switches that transfer to WiFi."""
        await self._send("U&WIFI")
        await asyncio.sleep(0.5)
        return True

    async def wifi_enable(self) -> bool:
        """APP&WIFIO: raise the access point."""
        await self._send("WIFIO")
        return True

    async def wifi_start(self) -> bool:
        """Trigger WiFi mode and bring the AP up, back to back.

        Convenience wrapper. Callers that need to read credentials between
        the two steps — which is the order the vendor app uses, see
        PROTOCOL.md — should call `wifi_trigger`, `wifi_get_credentials`,
        and `wifi_enable` individually instead.
        """
        await self.wifi_trigger()
        await self.wifi_enable()
        return True

    async def wifi_get_status(self) -> int:
        """Get WiFi status. Returns status code (1=ready)."""
        responses = await self._send("WIFIS")
        vals = self._parse_response(responses, "WIFIS")
        return int(vals[0]) if vals else -1

    async def wifi_wait_ready(self, timeout: float = 60.0) -> bool:
        """Poll WiFi status until ready (status=1) or timeout."""
        import time
        start = time.time()
        while time.time() - start < timeout:
            status = await self.wifi_get_status()
            console.print(f"[dim]WiFi status: {status}[/dim]")
            if status == WIFI_STATUS_READY:
                return True
            await asyncio.sleep(2.0)
        return False

    async def wifi_select_file(self, recording: Recording) -> int:
        """Select a file for WiFi transfer. Returns expected size in bytes."""
        cmd = f"U&{recording.date}&{recording.timestamp}"
        responses = await self._send(cmd)
        for r in responses:
            if r.startswith(f"{RSP_PREFIX}U&") and not r.startswith(f"{RSP_PREFIX}U&WIFI"):
                try:
                    return int(r.split("&")[-1])
                except ValueError:
                    pass
        return 0

    async def wifi_begin_transfer(self) -> int:
        """Signal that WiFi transfer should begin. Returns file size."""
        responses = await self._send("U&WIFI")
        for r in responses:
            if r.startswith(f"{RSP_PREFIX}U&") and not r.startswith(f"{RSP_PREFIX}U&WIFI"):
                try:
                    return int(r.split("&")[-1])
                except ValueError:
                    pass
        return 0

    async def wifi_cleanup(self):
        """Send WiFi disconnect/cleanup command."""
        await self._send("WIFIC")


async def download_with_retry(
    address: str,
    session_key: str,
    recording: Recording,
    max_retries: int = 3,
    progress_callback=None,
    retry_delay: float = 3.0,
) -> bytes:
    """Download a recording with automatic retry on failure.

    Returns MP3 bytes (trimmed to sync word) or empty bytes on total failure.
    See download_checked, which also says whether the size was verified.
    """
    data, _ = await download_checked(address, session_key, recording, max_retries,
                                     progress_callback, retry_delay)
    return data


async def download_checked(
    address: str,
    session_key: str,
    recording: Recording,
    max_retries: int = 3,
    progress_callback=None,
    retry_delay: float = 3.0,
) -> tuple[bytes, bool]:
    """Download a recording with automatic retry on failure.

    Creates its own BLE connection for each attempt. On disconnect or
    short data, waits briefly and retries from scratch. A partial transfer
    is never returned: callers write whatever comes back to its final path.

    Returns (MP3 bytes trimmed to the sync word, verified), or (b"", False)
    on total failure. Verified means the device announced a size and exactly
    that many bytes arrived; only then may the size be recorded for deleting
    (save_recording). An attempt without an announced size is retried; if
    every attempt lacks one, the last data is returned unverified.
    """
    from pocket_libre.protocol import MP3_SYNC_WORD

    unverified = b""
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                console.print(f"[yellow]Retry {attempt}/{max_retries}...[/yellow]")
                await asyncio.sleep(retry_delay)

            async with PocketCommander(address) as cmd:
                if not await cmd.authenticate(session_key):
                    console.print("[red]Auth failed.[/red]")
                    continue

                data = await cmd.download_ble(recording, progress_callback=progress_callback)

                if not data:
                    console.print("[yellow]No data received.[/yellow]")
                    continue

                # The device announces the exact size; anything less is a
                # dropped link or a stalled transfer.
                expected_size = cmd.last_expected_size
                if cmd._disconnected or len(data) < expected_size:
                    console.print(
                        f"[yellow]Incomplete transfer: {len(data):,} of "
                        f"{expected_size:,} bytes[/yellow]"
                    )
                    continue

                # Checked against the raw transfer, before the trim below.
                verified = 0 < expected_size == len(data)

                # Trim to MP3 sync word
                mp3_start = data.find(MP3_SYNC_WORD)
                if mp3_start > 0:
                    data = data[mp3_start:]

                # Validate: check we got a reasonable amount of data
                if recording.duration_s > 0:
                    expected = recording.estimated_bytes
                    if len(data) < expected * 0.5:
                        console.print(
                            f"[yellow]Short transfer: {len(data):,} bytes "
                            f"(expected ~{expected:,})[/yellow]"
                        )
                        continue

                if expected_size <= 0:
                    # Nothing to check the transfer against; ask again.
                    console.print("[yellow]The device did not announce the size; "
                                  "nothing to check the transfer against.[/yellow]")
                    unverified = data
                    continue
                return data, verified

        except Exception as e:
            console.print(f"[yellow]Attempt {attempt} failed: {e}[/yellow]")

    if unverified:
        return unverified, False
    console.print("[red]All retry attempts exhausted.[/red]")
    return b"", False
