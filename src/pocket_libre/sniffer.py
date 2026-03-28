"""Multi-characteristic sniffer. Subscribes to everything and shows what's streaming."""

import asyncio
import time
from collections import defaultdict

from bleak import BleakClient
from rich.console import Console
from rich.live import Live
from rich.table import Table


console = Console()


class MultiSniffer:
    """Subscribes to all notify characteristics and tracks data flow."""

    def __init__(self):
        self.data: dict[str, bytearray] = defaultdict(bytearray)
        self.packet_counts: dict[str, int] = defaultdict(int)
        self.last_packet: dict[str, bytes] = {}
        self.start_time = time.time()

    def make_handler(self, uuid: str):
        """Create a notification handler bound to a specific UUID."""
        def handler(sender: int, data: bytearray):
            self.data[uuid].extend(data)
            self.packet_counts[uuid] += 1
            self.last_packet[uuid] = bytes(data)
        return handler

    def build_table(self) -> Table:
        elapsed = time.time() - self.start_time
        table = Table(title=f"Live BLE Sniff ({elapsed:.0f}s)")
        table.add_column("Characteristic", style="cyan", max_width=40)
        table.add_column("Packets", justify="right")
        table.add_column("Bytes", justify="right")
        table.add_column("Rate", justify="right")
        table.add_column("Last Data (hex)", max_width=36)

        for uuid in sorted(self.packet_counts.keys()):
            count = self.packet_counts[uuid]
            total_bytes = len(self.data[uuid])
            rate = f"{total_bytes / elapsed:.0f} B/s" if elapsed > 0 else "..."
            last = self.last_packet.get(uuid, b"")
            last_hex = last[:16].hex(" ") + ("..." if len(last) > 16 else "")

            # Highlight high-volume characteristics (likely audio)
            style = "bold green" if total_bytes > 1000 else ""
            table.add_row(
                uuid,
                str(count),
                f"{total_bytes:,}",
                rate,
                last_hex,
                style=style,
            )

        return table


async def sniff_all(address: str, duration: int = 15):
    """Connect and subscribe to ALL notify characteristics to map data flow."""
    console.print(f"\n[bold]Connecting to {address}...[/bold]\n")

    try:
        async with BleakClient(address) as client:
            if not client.is_connected:
                console.print("[red]Failed to connect.[/red]")
                return

            console.print(f"[green]Connected![/green] MTU: {client.mtu_size}")

            sniffer = MultiSniffer()
            subscribed = []

            # Subscribe to every notify characteristic
            for service in client.services:
                for char in service.characteristics:
                    if "notify" in char.properties:
                        try:
                            handler = sniffer.make_handler(char.uuid)
                            await client.start_notify(char.uuid, handler)
                            subscribed.append(char.uuid)
                            console.print(f"  [dim]Subscribed: {char.uuid}[/dim]")
                        except Exception as e:
                            console.print(f"  [red]Failed: {char.uuid} ({e})[/red]")

            if not subscribed:
                console.print("[red]No notify characteristics to subscribe to.[/red]")
                return

            console.print(
                f"\n[bold]Listening on {len(subscribed)} characteristics "
                f"for {duration}s...[/bold]\n"
            )

            try:
                with Live(sniffer.build_table(), refresh_per_second=2) as live:
                    for _ in range(duration * 4):
                        await asyncio.sleep(0.25)
                        live.update(sniffer.build_table())
            except KeyboardInterrupt:
                console.print("\n[yellow]Stopped.[/yellow]")

            # Clean up
            for uuid in subscribed:
                try:
                    await client.stop_notify(uuid)
                except Exception:
                    pass

            # Summary
            console.print("\n[bold]Summary:[/bold]\n")
            if not sniffer.packet_counts:
                console.print(
                    "[yellow]No data received from any characteristic.[/yellow]\n"
                    "The device may need a command to start streaming.\n"
                    "Try triggering a recording on the device and run again."
                )
            else:
                for uuid in sorted(
                    sniffer.packet_counts.keys(),
                    key=lambda u: len(sniffer.data[u]),
                    reverse=True,
                ):
                    total = len(sniffer.data[uuid])
                    count = sniffer.packet_counts[uuid]
                    if total > 1000:
                        console.print(
                            f"  [bold green]{uuid}[/bold green]: "
                            f"{total:,} bytes, {count} packets "
                            f"<< HIGH VOLUME (likely audio)"
                        )
                    else:
                        console.print(
                            f"  [dim]{uuid}[/dim]: "
                            f"{total:,} bytes, {count} packets"
                        )

                # Save the highest-volume stream
                if sniffer.data:
                    top_uuid = max(sniffer.data.keys(), key=lambda u: len(sniffer.data[u]))
                    top_bytes = sniffer.data[top_uuid]
                    if len(top_bytes) > 100:
                        # Check if it's MP3 data (expected for Pocket)
                        is_mp3 = b"\xff\xf3" in top_bytes[:256]
                        ext = ".mp3" if is_mp3 else ".raw"
                        dump_path = f"sniff_dump{ext}"

                        out_data = top_bytes
                        if is_mp3:
                            # Trim to first real MP3 frame (skip silence padding)
                            first_sync = top_bytes.find(b"\xff\xf3")
                            if first_sync > 0:
                                out_data = top_bytes[first_sync:]

                        with open(dump_path, "wb") as f:
                            f.write(out_data)
                        console.print(
                            f"\n[bold green]Saved top stream ({len(out_data):,} bytes) "
                            f"to {dump_path}[/bold green]"
                        )
                        if is_mp3:
                            console.print(
                                f"[bold]MP3 audio detected! Play it:[/bold] "
                                f"open {dump_path}"
                            )
                        else:
                            console.print(
                                f"Try: [bold]pocket-libre convert "
                                f"--input {dump_path}[/bold]"
                            )

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
