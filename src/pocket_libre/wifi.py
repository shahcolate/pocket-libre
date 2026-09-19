"""WiFi bulk transfer over the device's SoftAP.

The BLE half of this flow is decoded (see PROTOCOL.md): the device can be
told to raise a WiFi access point and stage a file. What happens *on* that
access point is not decoded, and this module no longer pretends otherwise.

Earlier versions probed for an HTTP server on the AP. Field data from
firmware 1.8 (https://github.com/shahcolate/pocket-libre/issues/4) showed
that was a wrong guess inherited from a BLE-only packet capture: with the
device staged and reporting WIFIS=1 (ready), an exhaustive sweep of all
65535 TCP ports on the AP found only port 53 open, and strings pulled from
the vendor app describe a framed socket protocol using a ``RANGE`` verb
rather than HTTP ``GET``.

So this module is now in three parts:

  * ``scan_ports`` / ``diagnose`` — what ``pocket-libre wifi-discover`` runs.
    It sweeps the AP for listening sockets and prints a report. The open
    question it exists to answer is whether *anything* listens once the
    device is staged in the firmware 1.8 order; see PROTOCOL.md.
  * ``download_file`` — an HTTP client, kept only as an escape hatch for
    ``--url`` in case some firmware does serve over HTTP. Nothing has been
    observed to, so do not rely on it.
  * subnet guards — so we never again report a home router as a device hit.

Implementing the real transfer needs two unknowns filled in: the socket
port, and the RANGE frame format. Neither is guessable, and guessing is
what produced the bug this module is recovering from.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

console = Console()

# The Pocket AP hands this out as the gateway, confirmed by DHCP lease on
# firmware 1.8. (It also appears near WiFi-OTA strings in the vendor app,
# so it may double as the OTA address — either way it is the device.)
DEFAULT_HOST = "192.168.200.1"

# The /24 the device serves. Probing anything outside this is how the old
# candidate list ended up reporting people's home routers as device hits.
AP_SUBNET_PREFIX = "192.168.200."

CANDIDATE_HOSTS = [DEFAULT_HOST]


@dataclass
class PortScan:
    """The result of sweeping a host for listening TCP sockets."""

    host: str
    open_ports: list[int] = field(default_factory=list)
    scanned: int = 0


@dataclass
class DiscoveryReport:
    """Everything a diagnostic run learned, for pasting into an issue."""

    local_address: str | None = None
    on_ap_subnet: bool = False
    scan: PortScan | None = None
    endpoint: str | None = None


def local_address_for(host: str = DEFAULT_HOST) -> str | None:
    """The local interface address the OS would use to reach `host`.

    Opens an unconnected UDP socket and asks the routing table; no packets
    are sent. Returns None when there is no route at all.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect((host, 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def on_ap_subnet(host: str = DEFAULT_HOST) -> tuple[bool, str | None]:
    """True if this machine holds an address inside the device's /24.

    This is the guard that stops us probing a home LAN. Being routable to
    the gateway is not enough — a double-NAT setup will happily route
    192.168.200.1 to something that is not a Pocket.
    """
    address = local_address_for(host)
    if address is None:
        return False, None
    return address.startswith(AP_SUBNET_PREFIX), address


def is_host_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if a TCP connection to host:port completes."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_ports(
    host: str = DEFAULT_HOST,
    ports: list[int] | None = None,
    timeout: float = 0.35,
    workers: int = 256,
) -> PortScan:
    """Sweep `host` for listening TCP sockets.

    Defaults to the full range. This is the measurement that matters right
    now: whether staging the file in the firmware 1.8 order causes a socket
    to appear that the documented order never raised.
    """
    ports = ports if ports is not None else list(range(1, 65536))
    result = PortScan(host=host, scanned=len(ports))

    def check(port: int) -> int | None:
        return port if is_host_reachable(host, port, timeout=timeout) else None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for found in pool.map(check, ports):
            if found is not None:
                result.open_ports.append(found)
                console.print(f"  [green]open[/green] {host}:{found}")

    result.open_ports.sort()
    return result


def build_url(template: str, host: str, port: int, date: str, timestamp: str) -> str:
    """Render a path template into a full URL."""
    path = template.format(
        date=date, timestamp=timestamp, filename=f"{timestamp}.mp3"
    )
    authority = host if port == 80 else f"{host}:{port}"
    return f"http://{authority}{path}"


def diagnose(
    host: str = DEFAULT_HOST,
    ports: list[int] | None = None,
    require_ap_subnet: bool = True,
) -> DiscoveryReport:
    """Sweep the device AP and report what is listening.

    Returns a report even when nothing is found — a confirmed negative is
    the useful result here, because it would mean the transfer listener is
    never raised on this firmware and no client we could write would help.
    """
    report = DiscoveryReport()
    joined, address = on_ap_subnet(host)
    report.local_address = address
    report.on_ap_subnet = joined

    if address is None:
        console.print(
            f"[yellow]No route to {host}. Join the device's WiFi network "
            f"first.[/yellow]"
        )
        if require_ap_subnet:
            return report
    elif not joined:
        console.print(
            f"[yellow]This machine is {address}, which is outside the device "
            f"subnet {AP_SUBNET_PREFIX}0/24.[/yellow]\n"
            f"[yellow]Something else is answering for {host} — probing it "
            f"would report your own network, not the device.[/yellow]"
        )
        if require_ap_subnet:
            console.print("[dim]Pass --force to probe anyway.[/dim]")
            return report
    else:
        console.print(f"[dim]On the device subnet as {address}.[/dim]")

    console.print(f"[dim]Sweeping {host} for listening sockets...[/dim]")
    report.scan = scan_ports(host, ports=ports)
    return report


def download_file(
    url: str,
    output_path: str | Path,
    expected_size: int = 0,
    timeout: float = 30.0,
    progress_callback=None,
) -> int:
    """Stream a URL to disk. Returns bytes written, or 0 on failure.

    Writes to a temporary file and moves it into place only on success, so
    an interrupted transfer never leaves a truncated .mp3 that later runs
    would mistake for a completed download.

    No firmware has been observed serving files over HTTP; this exists for
    the `--url` escape hatch only.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_suffix(output_path.suffix + ".part")

    written = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "pocket-libre"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or expected_size)
            with partial.open("wb") as fh:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if progress_callback:
                        progress_callback(written, total)
    except (urllib.error.URLError, OSError) as e:
        console.print(f"[red]WiFi transfer failed: {e}[/red]")
        partial.unlink(missing_ok=True)
        return 0

    if written == 0:
        partial.unlink(missing_ok=True)
        return 0

    if expected_size and written < expected_size * 0.5:
        console.print(
            f"[yellow]Short transfer: {written:,} of ~{expected_size:,} bytes[/yellow]"
        )
        partial.unlink(missing_ok=True)
        return 0

    partial.replace(output_path)
    return written
