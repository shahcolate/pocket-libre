"""BLE audio capture. Connects to a device and streams data from a characteristic."""

import asyncio
import time
from pathlib import Path

from bleak import BleakClient
from rich.console import Console
from rich.live import Live
from rich.panel import Panel


console = Console()


class AudioCapture:
    """Captures BLE notification data to a file."""

    def __init__(self, output_path: str):
        self.output_path = Path(output_path)
        self.buffer = bytearray()
        self.packet_count = 0
        self.start_time: float | None = None
        self.packet_sizes: list[int] = []

    def handle_notification(self, sender: int, data: bytearray):
        """Callback for BLE notifications. Appends raw data to buffer."""
        if self.start_time is None:
            self.start_time = time.time()

        self.buffer.extend(data)
        self.packet_count += 1
        self.packet_sizes.append(len(data))

    def save(self):
        """Write captured data to file."""
        self.output_path.write_bytes(self.buffer)

    @property
    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    @property
    def bytes_captured(self) -> int:
        return len(self.buffer)

    @property
    def avg_packet_size(self) -> float:
        if not self.packet_sizes:
            return 0.0
        return sum(self.packet_sizes) / len(self.packet_sizes)

    def status_text(self) -> str:
        return (
            f"Packets: {self.packet_count} | "
            f"Bytes: {self.bytes_captured:,} | "
            f"Avg packet: {self.avg_packet_size:.0f}B | "
            f"Elapsed: {self.elapsed:.1f}s"
        )


async def auto_detect_audio_char(client: BleakClient) -> str | None:
    """Find the audio streaming characteristic.

    Uses known Pocket protocol UUIDs first, then falls back to
    heuristic detection for unknown firmware versions.
    """
    from pocket_libre.protocol import AUDIO_DATA_CHAR, AUDIO_CHAR_PRIORITY

    # First: check if the known audio characteristic exists on this device
    all_char_uuids = set()
    for service in client.services:
        for char in service.characteristics:
            all_char_uuids.add(char.uuid.lower())

    # Try known UUIDs in priority order
    for known_uuid in AUDIO_CHAR_PRIORITY:
        if known_uuid.lower() in all_char_uuids:
            console.print(f"[green]Found known audio characteristic: {known_uuid}[/green]")
            return known_uuid

    # Fallback: scan for any custom notify characteristics
    console.print("[yellow]Known audio UUIDs not found. Scanning for candidates...[/yellow]")
    candidates = []

    for service in client.services:
        if service.uuid.startswith("0000"):
            continue
        for char in service.characteristics:
            if "notify" in char.properties:
                candidates.append(char.uuid)

    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        console.print("[yellow]Multiple notify characteristics found:[/yellow]")
        for i, uuid in enumerate(candidates):
            console.print(f"  [{i}] {uuid}")
        console.print(
            "\nUse [bold]--char-uuid[/bold] to specify which one. "
            "Check PROTOCOL.md for guidance."
        )
        return None
    else:
        console.print("[red]No notify characteristics found on custom services.[/red]")
        return None


async def capture_audio(
    address: str,
    output_path: str,
    duration: int,
    char_uuid: str | None = None,
):
    """Connect to a BLE device and capture audio data."""
    console.print(f"\n[bold]Connecting to {address}...[/bold]\n")

    try:
        async with BleakClient(address) as client:
            if not client.is_connected:
                console.print("[red]Failed to connect.[/red]")
                return

            console.print(f"[green]Connected![/green] MTU: {client.mtu_size}")

            # Determine which characteristic to listen on
            target_uuid = char_uuid
            if target_uuid is None:
                console.print("[dim]Auto-detecting audio characteristic...[/dim]")
                target_uuid = await auto_detect_audio_char(client)
                if target_uuid is None:
                    console.print(
                        "[red]Could not auto-detect. "
                        "Use --char-uuid to specify manually.[/red]"
                    )
                    return

            console.print(f"[cyan]Listening on: {target_uuid}[/cyan]")

            capture = AudioCapture(output_path)

            # Subscribe to notifications
            await client.start_notify(target_uuid, capture.handle_notification)

            console.print(f"[bold]Capturing for up to {duration}s... (Ctrl+C to stop early)[/bold]\n")

            # Live status display
            try:
                with Live(
                    Panel(capture.status_text(), title="Capture"), refresh_per_second=4
                ) as live:
                    for _ in range(duration * 4):
                        await asyncio.sleep(0.25)
                        live.update(Panel(capture.status_text(), title="Capture"))
            except KeyboardInterrupt:
                console.print("\n[yellow]Capture stopped by user.[/yellow]")

            await client.stop_notify(target_uuid)

            if capture.bytes_captured > 0:
                raw_data = capture.buffer

                # Detect MP3 and trim to first real frame
                is_mp3 = b"\xff\xf3" in raw_data[:256]
                if is_mp3:
                    first_sync = raw_data.find(b"\xff\xf3")
                    if first_sync > 0:
                        raw_data = raw_data[first_sync:]

                    # Auto-switch extension to .mp3
                    if output_path.endswith(".raw"):
                        output_path = output_path.replace(".raw", ".mp3")

                from pathlib import Path
                Path(output_path).write_bytes(raw_data)

                console.print(
                    f"\n[bold green]Saved {len(raw_data):,} bytes "
                    f"to {output_path}[/bold green]"
                )
                console.print(
                    f"[dim]{capture.packet_count} packets, "
                    f"avg {capture.avg_packet_size:.0f} bytes/packet[/dim]"
                )
                if is_mp3:
                    console.print(
                        f"\n[bold]MP3 audio detected! Play it:[/bold] open {output_path}"
                    )
                    console.print(
                        f"Transcribe locally: "
                        f"[bold]pocket-libre transcribe --input {output_path}[/bold]"
                    )
                else:
                    console.print(
                        f"\nNext: convert to WAV with "
                        f"[bold]pocket-libre convert --input {output_path}[/bold]"
                    )
            else:
                console.print(
                    "\n[yellow]No data received.[/yellow] The device may need a "
                    "trigger command to start streaming. Check PROTOCOL.md."
                )

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print(
            "[yellow]Make sure the device is on and not connected to the official app.[/yellow]"
        )
