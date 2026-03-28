"""BLE GATT service explorer. Connects to a device and dumps its service tree."""

from bleak import BleakClient
from rich.console import Console
from rich.tree import Tree
from rich.panel import Panel


console = Console()

# Standard BLE service UUIDs for reference
KNOWN_SERVICES = {
    "00001800-0000-1000-8000-00805f9b34fb": "Generic Access",
    "00001801-0000-1000-8000-00805f9b34fb": "Generic Attribute",
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information",
    "0000180f-0000-1000-8000-00805f9b34fb": "Battery Service",
    "0000181c-0000-1000-8000-00805f9b34fb": "User Data",
}

# Standard characteristic UUIDs
KNOWN_CHARS = {
    "00002a00-0000-1000-8000-00805f9b34fb": "Device Name",
    "00002a01-0000-1000-8000-00805f9b34fb": "Appearance",
    "00002a19-0000-1000-8000-00805f9b34fb": "Battery Level",
    "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
    "00002a25-0000-1000-8000-00805f9b34fb": "Serial Number",
    "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
    "00002a27-0000-1000-8000-00805f9b34fb": "Hardware Revision",
    "00002a28-0000-1000-8000-00805f9b34fb": "Software Revision",
    "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer Name",
}


def format_properties(properties: list[str]) -> str:
    """Format characteristic properties with visual indicators."""
    icons = {
        "read": "R",
        "write": "W",
        "write-without-response": "Wn",
        "notify": "N",
        "indicate": "I",
        "broadcast": "B",
    }
    parts = []
    for prop in properties:
        abbrev = icons.get(prop, prop[0].upper())
        parts.append(abbrev)
    return " ".join(parts)


def classify_characteristic(uuid: str, properties: list[str], service_uuid: str) -> str:
    """Attempt to classify what a characteristic might be used for."""
    hints = []

    if "notify" in properties or "indicate" in properties:
        if "write" not in properties and "write-without-response" not in properties:
            hints.append("[bold yellow]<< possible audio stream[/bold yellow]")
        else:
            hints.append("[yellow]<< bidirectional (control?)[/yellow]")

    if "write" in properties and "notify" not in properties:
        hints.append("[dim]<< write-only (command?)[/dim]")

    return " ".join(hints)


async def explore_device(address: str):
    """Connect to a BLE device and dump its full GATT service tree."""
    console.print(f"\n[bold]Connecting to {address}...[/bold]\n")

    try:
        async with BleakClient(address) as client:
            if not client.is_connected:
                console.print("[red]Failed to connect.[/red]")
                return

            console.print(f"[green]Connected![/green] MTU: {client.mtu_size}\n")

            tree = Tree(f"[bold cyan]{address}[/bold cyan]")

            custom_services = []

            for service in client.services:
                service_name = KNOWN_SERVICES.get(service.uuid, "")
                is_custom = not service.uuid.startswith("0000") or service_name == ""

                if is_custom:
                    label = f"[bold magenta]{service.uuid}[/bold magenta] [dim](custom)[/dim]"
                    custom_services.append(service)
                elif service_name:
                    label = f"[cyan]{service.uuid}[/cyan] ({service_name})"
                else:
                    label = f"[cyan]{service.uuid}[/cyan]"

                service_branch = tree.add(label)

                for char in service.characteristics:
                    char_name = KNOWN_CHARS.get(char.uuid, "")
                    props = format_properties(char.properties)
                    classification = classify_characteristic(
                        char.uuid, char.properties, service.uuid
                    )

                    char_label = f"[green]{char.uuid}[/green]"
                    if char_name:
                        char_label += f" ({char_name})"
                    char_label += f" [{props}]"
                    if classification:
                        char_label += f" {classification}"

                    char_branch = service_branch.add(char_label)

                    # Try to read readable characteristics
                    if "read" in char.properties:
                        try:
                            value = await client.read_gatt_char(char.uuid)
                            # Try to decode as UTF-8, fall back to hex
                            try:
                                decoded = value.decode("utf-8").strip("\x00")
                                if decoded and decoded.isprintable():
                                    char_branch.add(f'[dim]Value: "{decoded}"[/dim]')
                                else:
                                    char_branch.add(f"[dim]Value: {value.hex()}[/dim]")
                            except (UnicodeDecodeError, ValueError):
                                char_branch.add(f"[dim]Value: {value.hex()}[/dim]")
                        except Exception:
                            char_branch.add("[dim]Value: (read failed)[/dim]")

                    for desc in char.descriptors:
                        char_branch.add(f"[dim]{desc.uuid} (descriptor)[/dim]")

            console.print(tree)

            if custom_services:
                console.print(
                    Panel(
                        "[bold]Custom services detected.[/bold]\n"
                        "Characteristics marked with [N] (Notify) are likely audio streams.\n"
                        "Characteristics marked with [W] (Write) are likely control commands.\n\n"
                        "Next step: Run a packet capture with PacketLogger to see the actual\n"
                        "data flowing through these characteristics during a sync.\n"
                        "See PROTOCOL.md for the full guide.",
                        title="Analysis",
                        border_style="green",
                    )
                )

            # Dump raw UUIDs for easy copy-paste into config
            console.print("\n[bold]Raw UUIDs (for config):[/bold]")
            for service in client.services:
                for char in service.characteristics:
                    props_str = ", ".join(char.properties)
                    console.print(f"  {char.uuid}  [{props_str}]")

    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        console.print(
            "[yellow]Make sure the device is on and not connected to the official app.[/yellow]"
        )
