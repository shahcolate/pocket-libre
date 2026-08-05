"""WiFi bulk transfer over the device's SoftAP.

The BLE half of this flow is fully decoded (see PROTOCOL.md): the device
can be told to raise a WiFi access point and stage a file for transfer.
What is *not* confirmed is the HTTP endpoint it serves that file from —
that needs someone with a device to probe it once, on the AP.

So this module is in two halves:

  * `download_file` — the transfer client. Give it a URL and it streams
    the file to disk with progress. Fully implemented.
  * `discover_endpoint` — walks a candidate space of hosts, ports, and
    path templates looking for something that serves MP3 bytes. This is
    what turns the unknown into a known; `pocket-libre wifi-discover`
    runs it and prints a report worth pasting into an issue.

Once the endpoint is confirmed, set it in config to skip discovery:

    pocket-libre config --set wifi.url_template="http://192.168.4.1/{timestamp}.mp3"
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from pocket_libre.protocol import MP3_SYNC_WORD

console = Console()

# ESP32 SoftAP default; the vendor app talks to this address.
DEFAULT_HOST = "192.168.4.1"
CANDIDATE_HOSTS = [DEFAULT_HOST, "192.168.1.1", "10.0.0.1"]
CANDIDATE_PORTS = [80, 8080, 8000, 81, 5000]

# Path templates to try, most-likely first. `{date}` and `{timestamp}`
# are substituted from the recording; `{filename}` is "<timestamp>.mp3".
CANDIDATE_PATHS = [
    "/{filename}",
    "/{date}/{filename}",
    "/sd/{date}/{filename}",
    "/record/{date}/{filename}",
    "/download?file={filename}",
    "/download?path=/{date}/{filename}",
    "/file/{filename}",
    "/api/file/{filename}",
    "/upload/{filename}",
    "/{timestamp}",
]

# Paths that may serve an index we can read even without knowing the
# file naming scheme.
CANDIDATE_INDEX_PATHS = ["/", "/list", "/files", "/api/list", "/dir", "/index.json"]


@dataclass
class Probe:
    """One endpoint attempt and what came back."""

    url: str
    status: int | None = None
    content_type: str = ""
    length: int = 0
    looks_like_mp3: bool = False
    error: str = ""

    @property
    def promising(self) -> bool:
        return self.looks_like_mp3 or (self.status == 200 and self.length > 0)


@dataclass
class DiscoveryReport:
    """Everything a probe run learned, for printing or filing as an issue."""

    reachable_hosts: list[str] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    endpoint: str | None = None

    @property
    def hits(self) -> list[Probe]:
        return [p for p in self.probes if p.promising]


def is_host_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if a TCP connection to host:port completes."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_url(template: str, host: str, port: int, date: str, timestamp: str) -> str:
    """Render a path template into a full URL."""
    path = template.format(
        date=date, timestamp=timestamp, filename=f"{timestamp}.mp3"
    )
    authority = host if port == 80 else f"{host}:{port}"
    return f"http://{authority}{path}"


def probe_url(url: str, timeout: float = 5.0, read_bytes: int = 4096) -> Probe:
    """Fetch the first few KB of a URL and judge whether it looks like audio."""
    probe = Probe(url=url)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "pocket-libre"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            probe.status = response.status
            probe.content_type = response.headers.get("Content-Type", "")
            head = response.read(read_bytes)
            probe.length = int(response.headers.get("Content-Length") or len(head))
            probe.looks_like_mp3 = (
                MP3_SYNC_WORD in head[:512]
                or head[:3] == b"ID3"
                or "audio" in probe.content_type.lower()
            )
    except urllib.error.HTTPError as e:
        probe.status = e.code
        probe.error = f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        probe.error = str(e)
    return probe


def discover_endpoint(
    date: str,
    timestamp: str,
    hosts: list[str] | None = None,
    ports: list[int] | None = None,
    timeout: float = 3.0,
) -> DiscoveryReport:
    """Probe the candidate space for an endpoint serving this recording.

    Returns a report even when nothing is found — the negative results are
    what make a bug report actionable.
    """
    report = DiscoveryReport()
    hosts = hosts or CANDIDATE_HOSTS
    ports = ports or CANDIDATE_PORTS

    live: list[tuple[str, int]] = []
    for host in hosts:
        for port in ports:
            if is_host_reachable(host, port, timeout=1.0):
                live.append((host, port))
                if host not in report.reachable_hosts:
                    report.reachable_hosts.append(host)
                console.print(f"  [green]open[/green] {host}:{port}")

    if not live:
        console.print(
            "[yellow]No HTTP port answered. Are you joined to the device's "
            "WiFi network?[/yellow]"
        )
        return report

    for host, port in live:
        for template in CANDIDATE_PATHS + CANDIDATE_INDEX_PATHS:
            url = build_url(template, host, port, date, timestamp)
            probe = probe_url(url, timeout=timeout)
            report.probes.append(probe)
            if probe.looks_like_mp3:
                console.print(f"  [bold green]MP3![/bold green] {url}")
                report.endpoint = url
                return report
            if probe.promising:
                console.print(f"  [cyan]{probe.status}[/cyan] {url} ({probe.length}B)")

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
