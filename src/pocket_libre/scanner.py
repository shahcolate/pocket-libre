"""BLE device scanner. Finds nearby Bluetooth Low Energy devices."""

from bleak import BleakScanner
from rich.console import Console
from rich.table import Table
from rich.panel import Panel


console = Console()

# Known identifiers for Pocket devices.
# PKT01 is the confirmed device name prefix.
# Note: "ove" was removed because it false-matches "Govee" smart lights.
POCKET_HINTS = ["pkt01", "pkt02", "pocket"]


def is_likely_pocket(name: str | None) -> bool:
    """Heuristic check if a device might be a Pocket recorder."""
    if not name:
        return False
    lower = name.lower()
    return any(hint in lower for hint in POCKET_HINTS)


async def scan_devices(timeout: float = 10.0, name_filter: str | None = None):
    """Scan for nearby BLE devices and display results."""
    console.print(f"\n[bold]Scanning for BLE devices ({timeout}s)...[/bold]\n")

    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    table = Table(title="BLE Devices Found")
    table.add_column("Name", style="cyan")
    table.add_column("Address", style="green", no_wrap=True)
    table.add_column("RSSI", justify="right")
    table.add_column("Pocket?", justify="center")

    pocket_devices = []

    for address, (device, adv_data) in sorted(
        devices.items(), key=lambda x: x[1][1].rssi or -999, reverse=True
    ):
        name = device.name or adv_data.local_name or "Unknown"

        if name_filter and name_filter.lower() not in name.lower():
            continue

        is_pocket = is_likely_pocket(name)
        if is_pocket:
            pocket_devices.append((name, address))

        table.add_row(
            name,
            address,
            str(adv_data.rssi),
            "[bold green]<< likely[/bold green]" if is_pocket else "",
        )

    console.print(table)
    console.print(f"\n[dim]Found {len(devices)} device(s)[/dim]")

    if pocket_devices:
        for name, address in pocket_devices:
            console.print(
                f"\n[bold green]Pocket found: {name}[/bold green]"
            )
            console.print(
                f"  Address: [bold]{address}[/bold]\n"
            )
            console.print(
                f"  Next steps:\n"
                f"    pocket-libre sniff --address {address}\n"
                f"    pocket-libre capture --address {address}\n"
            )
    else:
        console.print(
            Panel(
                "[bold yellow]No Pocket device found.[/bold yellow]\n\n"
                "Troubleshooting:\n"
                "  1. Make sure your Pocket is powered on\n"
                "  2. Disconnect from nRF Connect if you used it for discovery\n"
                "  3. Turn off Bluetooth on your phone (or enable Airplane Mode)\n"
                "     BLE devices can only connect to one host at a time\n"
                "  4. Close the official Pocket app on your phone\n"
                "  5. If nothing works, delete the Pocket app and try again\n\n"
                "Then re-run: [bold]pocket-libre scan --filter pkt[/bold]",
                title="Not Found",
                border_style="yellow",
            )
        )
