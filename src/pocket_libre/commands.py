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
from dataclasses import dataclass

from bleak import BleakClient
from rich.console import Console

from pocket_libre.protocol import (
    CMD_WRITE_CHAR,
    CMD_NOTIFY_CHAR,
    AUDIO_NOTIFY_CHAR,
    CMD_PREFIX,
    RSP_PREFIX,
    WIFI_STATUS_READY,
)


console = Console()


@dataclass
class Recording:
    """A recording stored on the device."""
    date: str          # e.g. "2026-03-28"
    timestamp: str     # e.g. "20260328001919"
    size_kb: int       # file size in KB from LIST response

    @property
    def filename(self) -> str:
        return f"{self.timestamp}.mp3"

    def __str__(self) -> str:
        mins = self.size_kb // 60 if self.size_kb > 0 else 0
        return f"{self.date}/{self.timestamp} ({self.size_kb} KB, ~{mins}m)"


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
        self.client = BleakClient(device, timeout=self.timeout)
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

    def _on_response(self, sender: int, data: bytearray):
        text = data.decode("ascii", errors="replace")
        self._responses.append(text)
        self._response_event.set()

    def _on_audio(self, sender: int, data: bytearray):
        self._audio_data.extend(data)
        self._audio_event.set()

    async def _send(self, command: str) -> list[str]:
        """Send an APP& command and collect MCU& responses."""
        self._responses.clear()
        self._response_event.clear()

        payload = f"{CMD_PREFIX}{command}".encode("ascii")
        await self.client.write_gatt_char(CMD_WRITE_CHAR, payload, response=False)

        # Wait for response(s) — some commands return multiple lines
        await asyncio.sleep(0.3)
        # Give extra time for multi-line responses
        for _ in range(10):
            self._response_event.clear()
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                break

        return list(self._responses)

    def _parse_response(self, responses: list[str], prefix: str) -> list[str]:
        """Extract response values matching a prefix like 'MCU&BAT&'."""
        full = f"{RSP_PREFIX}{prefix}&"
        return [r[len(full):] for r in responses if r.startswith(full)]

    # ── Device Info ──────────────────────────────

    async def authenticate(self, session_key: str) -> bool:
        responses = await self._send(f"SK&{session_key}")
        return any("MCU&SK&OK" in r for r in responses)

    async def get_battery(self) -> int:
        responses = await self._send("BAT")
        vals = self._parse_response(responses, "BAT")
        return int(vals[0]) if vals else -1

    async def get_firmware(self) -> str:
        responses = await self._send("FW")
        vals = self._parse_response(responses, "FW")
        return vals[0].strip() if vals else "unknown"

    async def get_storage(self) -> tuple[int, int]:
        """Returns (used_kb, total_kb)."""
        responses = await self._send("SPACE")
        vals = self._parse_response(responses, "SPA")
        if vals:
            parts = vals[0].split("&")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        return 0, 0

    async def get_state(self) -> int:
        """0 = idle, other values TBD."""
        responses = await self._send("STE")
        vals = self._parse_response(responses, "STE")
        return int(vals[0]) if vals else -1

    async def set_time(self, time_str: str | None = None) -> bool:
        """Set device time. Format: YYYYMMDDHHmmss."""
        if time_str is None:
            from datetime import datetime
            time_str = datetime.now().strftime("%Y%m%d%H%M%S")
        responses = await self._send(f"T&{time_str}")
        return any("MCU&T&OK" in r for r in responses)

    # ── File Listing ─────────────────────────────

    async def list_dirs(self) -> list[str]:
        """List recording dates on device. Returns ['2026-03-26', '2026-03-27', ...]."""
        responses = await self._send("LIST_DIRS")
        return self._parse_response(responses, "DIRS")

    async def list_files(self, date: str) -> list[Recording]:
        """List recordings for a date. Returns list of Recording objects."""
        responses = await self._send(f"LIST&{date}")
        recordings = []
        for r in responses:
            if not r.startswith(f"{RSP_PREFIX}F&"):
                continue
            # MCU&F&2026-03-28&20260328001919&6222
            parts = r.split("&")
            if len(parts) >= 5:
                rec_date = parts[2]
                timestamp = parts[3]
                size_kb = int(parts[4]) if parts[4].isdigit() else 0
                recordings.append(Recording(rec_date, timestamp, size_kb))
        return recordings

    async def list_all_recordings(self) -> list[Recording]:
        """List all recordings across all dates."""
        dirs = await self.list_dirs()
        all_recs = []
        for d in dirs:
            recs = await self.list_files(d)
            all_recs.extend(recs)
        return all_recs

    # ── BLE File Transfer ────────────────────────

    async def download_ble(
        self,
        recording: Recording,
        progress_callback=None,
    ) -> bytes:
        """Download a recording over BLE. Returns MP3 bytes."""
        # Subscribe to audio notifications
        self._audio_data.clear()
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

        if expected_size > 0:
            console.print(f"[dim]Expected size: {expected_size:,} bytes[/dim]")

        # Collect audio data until transfer completes
        last_size = 0
        stall_count = 0
        while True:
            self._audio_event.clear()
            try:
                await asyncio.wait_for(self._audio_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                stall_count += 1
                if stall_count >= 3:
                    break
                continue

            stall_count = 0
            current_size = len(self._audio_data)

            if progress_callback:
                progress_callback(current_size, expected_size)

            if expected_size > 0 and current_size >= expected_size:
                break

            last_size = current_size

        await self.client.stop_notify(AUDIO_NOTIFY_CHAR)
        return bytes(self._audio_data)

    # ── WiFi Transfer ────────────────────────────

    async def wifi_get_credentials(self) -> tuple[str, str] | None:
        """Get WiFi AP credentials. Returns (ssid, password) or None."""
        responses = await self._send("WIFI")
        for r in responses:
            # MCU&WIFI&PKT01_GREY_XXXXXXXX&xJiEbRKn
            if r.startswith(f"{RSP_PREFIX}WIFI&"):
                parts = r.split("&")
                if len(parts) >= 4:
                    return parts[2], parts[3]
        return None

    async def wifi_start(self) -> bool:
        """Trigger WiFi AP mode on the device."""
        await self._send("U&WIFI")
        await asyncio.sleep(0.5)

        # Turn on WiFi
        await self._send("WIFIO")
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
