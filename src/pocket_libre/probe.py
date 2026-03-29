"""BLE write characteristic prober. Sends commands and logs responses across all notify chars."""

import asyncio
import time
from collections import defaultdict

from bleak import BleakClient, BleakScanner
from rich.console import Console
from rich.table import Table

from pocket_libre.protocol import (
    ALL_NOTIFY_CHARS,
    ALL_WRITE_CHARS,
)


console = Console()

# Standard probe payloads to try when no specific data is given
DEFAULT_PROBES = [
    b"\x00",
    b"\x01",
    b"\x02",
    b"\x03",
    b"\x04",
    b"\x05",
    b"\xff",
    b"\x01\x00",
    b"\x01\x01",
    b"\x02\x00",
    b"\x00\x01",
    # ASCII text commands (device metadata uses "MCU&STA&..." format)
    b"AT\r\n",
    b"MCU\r\n",
    b"STA\r\n",
    b"LIST\r\n",
    b"WIFI\r\n",
]


class ProbeResult:
    """Collects notify responses during a probe window."""

    def __init__(self):
        self.responses: dict[str, list[tuple[float, bytes]]] = defaultdict(list)
        self.start_time = 0.0

    def make_handler(self, uuid: str):
        def handler(sender: int, data: bytearray):
            elapsed = time.time() - self.start_time
            self.responses[uuid].append((elapsed, bytes(data)))
        return handler

    def reset(self):
        self.responses.clear()
        self.start_time = time.time()


def _short_uuid(uuid: str) -> str:
    """Return a readable short form of a UUID."""
    upper = uuid.upper()
    if upper.endswith("-0000-1000-8000-00805F9B34FB"):
        return upper[:8]
    return upper.split("-")[0]


def _format_bytes(data: bytes, max_len: int = 32) -> str:
    hex_str = data[:max_len].hex(" ")
    ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:max_len])
    suffix = "..." if len(data) > max_len else ""
    return f"{hex_str}{suffix}  [{ascii_str}{suffix}]"


async def _probe_one_char(
    address: str,
    target_uuid: str | None,
    probes: list[bytes],
    wait: float,
) -> list[tuple[str, str, str, str, str]]:
    """Connect, probe one write char with all payloads, disconnect. Returns table rows."""
    rows = []

    async with BleakClient(address, timeout=10.0) as client:
        if not client.is_connected:
            return rows

        collector = ProbeResult()

        # Subscribe to all notify chars
        for service in client.services:
            for char in service.characteristics:
                if "notify" in char.properties:
                    try:
                        await client.start_notify(char.uuid, collector.make_handler(char.uuid))
                    except Exception:
                        pass

        # Find the target write char object
        char_obj = None
        for service in client.services:
            for char in service.characteristics:
                if "write" in char.properties or "write-without-response" in char.properties:
                    if target_uuid is None or char.uuid == target_uuid:
                        char_obj = char
                        if target_uuid:
                            break
            if char_obj and target_uuid:
                break

        if not char_obj:
            return rows

        write_uuid = char_obj.uuid
        use_response = "write-without-response" not in char_obj.properties
        props = ", ".join(char_obj.properties)
        console.print(f"\n[bold cyan]Probing {_short_uuid(write_uuid)}[/bold cyan] [{props}]")

        for probe_data in probes:
            hex_sent = probe_data.hex(" ")
            console.print(f"  Writing: [yellow]{hex_sent}[/yellow]", end="")

            collector.reset()
            try:
                await client.write_gatt_char(char_obj, probe_data, response=use_response)
            except Exception as e:
                console.print(f" [red]FAIL: {e}[/red]")
                rows.append((_short_uuid(write_uuid), hex_sent, "-", f"write error: {e}", "-"))
                continue

            await asyncio.sleep(wait)

            if not collector.responses:
                console.print(f" [dim]... silence[/dim]")
                rows.append((_short_uuid(write_uuid), hex_sent, "-", "(no response)", "-"))
            else:
                console.print()
                for resp_uuid, packets in collector.responses.items():
                    for elapsed, resp_data in packets:
                        console.print(
                            f"    [green]<< {_short_uuid(resp_uuid)}[/green]: "
                            f"{_format_bytes(resp_data)} ({elapsed:.3f}s)"
                        )
                        rows.append((
                            _short_uuid(write_uuid),
                            hex_sent,
                            _short_uuid(resp_uuid),
                            _format_bytes(resp_data),
                            f"{elapsed:.3f}s",
                        ))

    return rows


async def probe_characteristic(
    address: str,
    char_uuid: str | None = None,
    data: bytes | None = None,
    wait: float = 0.5,
):
    """Probe write characteristics with fresh BLE connections per characteristic."""
    console.print(f"\n[bold]Scanning for device {address[:12]}...[/bold]")

    # First connection: enumerate available write characteristics
    console.print(f"[dim]Connecting to discover services...[/dim]")
    write_char_uuids = []

    async with BleakClient(address, timeout=10.0) as client:
        if not client.is_connected:
            console.print("[red]Failed to connect.[/red]")
            return

        console.print(f"[green]Connected![/green] MTU: {client.mtu_size}")

        for service in client.services:
            for char in service.characteristics:
                if "write" in char.properties or "write-without-response" in char.properties:
                    write_char_uuids.append(char.uuid)
                    console.print(f"  [dim]Write char: {_short_uuid(char.uuid)} [{', '.join(char.properties)}][/dim]")

    if char_uuid:
        target = char_uuid.lower()
        matches = [u for u in write_char_uuids if u.lower() == target]
        if not matches:
            console.print(f"\n[red]Characteristic {char_uuid} not found.[/red]")
            console.print(f"[dim]Available: {', '.join(_short_uuid(u) for u in write_char_uuids)}[/dim]")
            return
        write_char_uuids = matches

    probes = [data] if data else DEFAULT_PROBES
    console.print(f"\n[bold]Probing {len(write_char_uuids)} write chars × {len(probes)} payloads[/bold]")
    console.print(f"[dim]Reconnecting per characteristic to avoid BLE timeout[/dim]")

    # Probe each characteristic with a fresh connection
    all_rows = []
    total_responses = 0

    for uuid in write_char_uuids:
        try:
            rows = await _probe_one_char(address, uuid, probes, wait)
            all_rows.extend(rows)
            total_responses += sum(1 for r in rows if r[2] != "-")
        except Exception as e:
            console.print(f"\n[red]Error probing {_short_uuid(uuid)}: {e}[/red]")
        # Brief pause between reconnects
        await asyncio.sleep(1.0)

    # Summary table
    results_table = Table(title="Probe Results")
    results_table.add_column("Write Char", style="cyan")
    results_table.add_column("Sent", style="yellow")
    results_table.add_column("Response Char", style="green")
    results_table.add_column("Response Data")
    results_table.add_column("Delay", justify="right")

    for row in all_rows:
        style = "bold green" if row[2] != "-" else ""
        results_table.add_row(*row, style=style)

    console.print()
    console.print(results_table)
    console.print(
        f"\n[bold]Total: {total_responses} responses from "
        f"{len(write_char_uuids)} chars × {len(probes)} probes[/bold]"
    )

    if total_responses == 0:
        console.print(
            "\n[yellow]No responses detected.[/yellow] Next steps:\n"
            "  1. Press the button on your Pocket to start/stop a recording, then probe again\n"
            "  2. Try probing while the device is actively recording\n"
            "  3. Use PacketLogger to capture what the official app writes\n"
            "  4. The device may need a multi-byte handshake before it responds"
        )
    else:
        console.print(
            "\n[green]Responses found![/green] Cross-reference with PacketLogger captures."
        )
