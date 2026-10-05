"""WiFi file transfer over the device's access point (firmware 1.7 and 1.8).

How it works, decoded on firmware 1.8 / WiFi firmware V9 from the vendor app's
HCI log and confirmed byte for byte against BLE downloads (PROTOCOL.md has the
full write-up):

  1. Over BLE: APP&WIFIO raises the access point, APP&WIFI returns its SSID and
     password, and MCU&WIFIS goes 3 (starting) -> 2 (waiting for a client) ->
     1 (a client has joined). The SSID is hidden; it is WPA2-PSK, the device
     is 192.168.200.1 and hands out 192.168.200.2.
  2. Per file: connect to 192.168.200.1:8475 and send nothing. Request the file
     as a normal Bluetooth transfer (APP&U&<date>&<ts> -> MCU&U&<size>), then
     about 0.3 s later switch it to WiFi with APP&U&WIFI (-> MCU&U&WIFI). The
     socket then carries the raw MP3 file, exactly <size> bytes, followed by a
     fixed 10-byte END_MARKER; MCU&OFF arrives over BLE at the same time. Close
     the connection afterwards.
  3. The device serves a limited number of transfer connections per
     access-point session: two on 1.8, after which 8475 stops listening, and
     one on 1.7, which resets the second. Restarting the AP (APP&WIFIC,
     APP&WIFIO) starts over. Never send APP&U&WIFI without a connection open:
     the device hangs in the switch and later reports MCU&SHUT.

Firmware 1.7 (WiFi firmware V9) was confirmed by a field report to follow the
same protocol apart from that limit.

``WifiSession`` implements this, using a ``hostwifi`` backend to move this
machine onto the device's network and back. ``scan_ports`` / ``diagnose`` stay
as a diagnostic for other firmware: on 1.7 and 1.8 the only listener is 8475.
"""

from __future__ import annotations

import asyncio
import errno
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from pocket_libre.protocol import (
    DEFAULT_FILES_PER_AP_SESSION,
    END_MARKER,
    FILES_PER_AP_SESSION,
    MP3_SYNC_WORD,
    TRANSFER_HOST,
    TRANSFER_PORT,
)

console = Console()

# The Pocket AP hands this out as the gateway, confirmed by DHCP lease on
# firmware 1.8. It is also where the transfer socket listens.
DEFAULT_HOST = TRANSFER_HOST

# The /24 the device serves. Probing anything outside this is how the old
# candidate list ended up reporting people's home routers as device hits.
# Derived from DEFAULT_HOST so the two cannot drift apart.
AP_SUBNET_PREFIX = DEFAULT_HOST.rsplit(".", 1)[0] + "."

CANDIDATE_HOSTS = [DEFAULT_HOST]


@dataclass
class PortScan:
    """The result of sweeping a host for listening TCP sockets."""

    host: str
    open_ports: list[int] = field(default_factory=list)
    scanned: int = 0
    reliable: bool = True
    error: str = ""


@dataclass
class DiscoveryReport:
    """Everything a diagnostic run learned, for pasting into an issue."""

    local_address: str | None = None
    on_ap_subnet: bool = False
    scan: PortScan | None = None


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


def subnet_prefix(host: str) -> str:
    """The /24 prefix of `host`, e.g. "192.168.200." — "" if unparseable."""
    octets = host.split(".")
    if len(octets) != 4:
        return ""
    return ".".join(octets[:3]) + "."


def on_ap_subnet(host: str = DEFAULT_HOST) -> tuple[bool, str | None]:
    """True if this machine holds an address inside `host`'s /24.

    This is the guard that stops us probing a home LAN. Being routable to
    the gateway is not enough — a double-NAT setup will happily route
    192.168.200.1 to something that is not a Pocket.

    The prefix is derived from `host` rather than hardcoded, so pointing
    --host at a device on a different subnet still works.
    """
    address = local_address_for(host)
    if address is None:
        return False, None
    prefix = subnet_prefix(host)
    if not prefix:
        return False, address
    return address.startswith(prefix), address


# Running out of file descriptors looks exactly like a closed port at the
# socket layer. That would be a silent, confident lie from a command whose
# whole purpose is to report a trustworthy negative, so it is tracked apart.
_FD_EXHAUSTION_ERRNOS = frozenset(
    e for e in (
        getattr(errno, "EMFILE", None),
        getattr(errno, "ENFILE", None),
        getattr(errno, "ENOBUFS", None),
    ) if e is not None
)


class ScanResourceError(RuntimeError):
    """The scanner ran out of file descriptors, so results are unreliable."""


def is_host_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if a TCP connection to host:port completes.

    Raises ScanResourceError if the local process is out of descriptors,
    rather than reporting the port closed.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as e:
        if e.errno in _FD_EXHAUSTION_ERRNOS:
            raise ScanResourceError(
                f"Out of file descriptors while probing port {port}"
            ) from e
        return False


def _safe_worker_count(requested: int) -> int:
    """Cap concurrency well inside the process descriptor limit.

    macOS commonly ships a soft limit of 256, which a naive 256-way sweep
    walks straight into.
    """
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        return min(requested, 64)
    if soft in (-1, getattr(resource, "RLIM_INFINITY", -1)):
        return requested
    # Leave half the budget for everything else the process is doing, but
    # never scale *up* past what the caller asked for.
    return min(requested, max(8, soft // 2))


def scan_ports(
    host: str = DEFAULT_HOST,
    ports: list[int] | None = None,
    timeout: float = 0.35,
    workers: int = 128,
) -> PortScan:
    """Sweep `host` for listening TCP sockets.

    Defaults to the full range. On firmware 1.7 and 1.8 the only listener is
    the transfer socket, 8475, and only while the AP is up and has transfer
    connections left; a sweep is for checking other firmware.

    An empty result is only meaningful if the sweep actually completed, so
    descriptor exhaustion sets `reliable = False` instead of quietly
    reporting every port closed.
    """
    ports = ports if ports is not None else list(range(1, 65536))
    result = PortScan(host=host, scanned=len(ports))

    def check(port: int) -> int | None:
        return port if is_host_reachable(host, port, timeout=timeout) else None

    with ThreadPoolExecutor(max_workers=_safe_worker_count(workers)) as pool:
        try:
            for found in pool.map(check, ports):
                if found is not None:
                    result.open_ports.append(found)
                    console.print(f"  [green]open[/green] {host}:{found}")
        except ScanResourceError as e:
            result.reliable = False
            result.error = str(e)

    result.open_ports.sort()
    return result


def diagnose(
    host: str = DEFAULT_HOST,
    ports: list[int] | None = None,
    require_ap_subnet: bool = True,
) -> DiscoveryReport:
    """Sweep the device AP and report what is listening.

    Returns a report even when nothing is found — on firmware other than
    1.8, a confirmed negative is a useful result in an issue.
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
            f"subnet {subnet_prefix(host) or host}0/24.[/yellow]\n"
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


# ── File transfer ────────────────────────────────


class WifiTransferError(RuntimeError):
    """A WiFi transfer step failed; the message says which."""


def firmware_line(firmware: str) -> str:
    """The major.minor part of a firmware version: "1.8.0" -> "1.8"."""
    return ".".join(firmware.strip().split(".")[:2])


def files_per_ap_session(firmware: str) -> int:
    """Transfer connections the device serves per AP session on `firmware`."""
    return FILES_PER_AP_SESSION.get(firmware_line(firmware), DEFAULT_FILES_PER_AP_SESSION)


@dataclass
class TransferResult:
    path: Path
    size: int
    seconds: float
    marker_ok: bool


async def open_transfer_socket(host: str = DEFAULT_HOST, port: int = TRANSFER_PORT,
                               wait: float = 15.0):
    """Connect to the transfer socket, retrying refusals for `wait` seconds.

    After a transfer connection closes, the device refuses new ones for
    about 1.5–3.5 s before it listens again, so a refusal is retried.
    Returns an asyncio (reader, writer) pair.
    """
    deadline = time.monotonic() + wait
    last: Exception | None = None
    while True:
        try:
            return await asyncio.wait_for(asyncio.open_connection(host, port), 3.0)
        except (OSError, asyncio.TimeoutError) as e:
            last = e
        if time.monotonic() >= deadline:
            raise WifiTransferError(
                f"{host}:{port} did not accept a connection within {wait:g}s ({last})"
            )
        await asyncio.sleep(0.5)


def _discard(partial: Path) -> None:
    """Remove a partial download, without masking the error that ended it."""
    try:
        partial.unlink(missing_ok=True)
    except OSError:
        pass


async def receive_file(
    reader: asyncio.StreamReader,
    size: int,
    out_path: str | Path,
    first_byte_timeout: float = 15.0,
    idle_timeout: float = 15.0,
    marker_timeout: float = 3.0,
    progress_callback: Callable[[int, int], None] | None = None,
) -> bool:
    """Read exactly `size` bytes of file into `out_path`, then the end marker.

    Writes to `<out_path>.part` and moves it into place only once all `size`
    bytes arrived, so an interrupted transfer never leaves a truncated .mp3.
    Returns whether the 10-byte end marker followed; a missing marker is
    reported but does not fail an otherwise complete file.
    """
    out_path = Path(out_path)
    partial = out_path.with_suffix(out_path.suffix + ".part")
    received = 0
    tail = b""
    head = b""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("wb") as fh:
            timeout = first_byte_timeout
            while received < size:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout)
                except asyncio.TimeoutError:
                    what = "no data" if received == 0 else f"stalled at {received:,} bytes"
                    raise WifiTransferError(
                        f"{what} after {timeout:g}s (expected {size:,} bytes)"
                    ) from None
                except OSError as e:  # e.g. reset by the device
                    raise WifiTransferError(
                        f"connection lost at {received:,} of {size:,} bytes: {e}"
                    ) from e
                if not chunk:
                    raise WifiTransferError(
                        f"device closed the connection at {received:,} of {size:,} bytes"
                    )
                timeout = idle_timeout
                body = chunk[: size - received]
                tail += chunk[len(body):]
                fh.write(body)
                if len(head) < 4:
                    head += body[: 4 - len(head)]
                received += len(body)
                if progress_callback:
                    progress_callback(received, size)
        while len(tail) < len(END_MARKER):
            try:
                chunk = await asyncio.wait_for(reader.read(len(END_MARKER) - len(tail)), marker_timeout)
            except (asyncio.TimeoutError, OSError):
                # The file is complete; a reset here only costs the marker.
                break
            if not chunk:
                break
            tail += chunk
        partial.replace(out_path)
    except OSError as e:  # the local file: disk full, permissions
        _discard(partial)
        raise WifiTransferError(f"could not write {out_path}: {e}") from e
    except BaseException:
        _discard(partial)
        raise
    if size and not head.startswith(MP3_SYNC_WORD[:1]):
        console.print(f"[yellow]{out_path.name} does not start with an MP3 frame "
                      f"({head.hex()}).[/yellow]")
    return tail[: len(END_MARKER)] == END_MARKER


class WifiSession:
    """The device's access point, raised, joined, and used for transfers.

    Use as ``async with WifiSession(cmd, host_wifi) as session`` after
    authenticating ``cmd``, then ``await session.download(recording, path)``
    for each file. On exit the AP is lowered and this machine's WiFi is put
    back, also after errors and Ctrl-C.
    """

    def __init__(
        self,
        cmd,
        host_wifi,
        host: str = DEFAULT_HOST,
        port: int = TRANSFER_PORT,
        log: Callable[[str], None] | None = None,
        join_timeout: float = 90.0,
        ready_timeout: float = 90.0,
        heartbeat: float = 5.0,
        status_interval: float = 1.0,
        switch_delay: float = 0.3,
        files_per_session: int = DEFAULT_FILES_PER_AP_SESSION,
        connect_wait: float = 15.0,
        first_byte_timeout: float = 15.0,
        idle_timeout: float = 15.0,
    ):
        self.cmd = cmd
        self.host_wifi = host_wifi
        self.host = host
        self.port = port
        self.log = log or (lambda text: None)
        self.join_timeout = join_timeout
        self.ready_timeout = ready_timeout
        self.heartbeat = heartbeat
        self.status_interval = status_interval
        self.switch_delay = switch_delay
        self.files_per_session = files_per_session
        self.connect_wait = connect_wait
        self.first_byte_timeout = first_byte_timeout
        self.idle_timeout = idle_timeout
        self.ssid: str | None = None
        self.password: str | None = None
        self.connections = 0
        self.ap_starts = 0
        self._raised = False
        self._prepared = False
        self._audio = False
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> WifiSession:
        try:
            await self.start()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        await self.host_wifi.setup()
        await self.cmd.start_audio_sink()
        self._audio = True
        if self.heartbeat > 0:
            self._tasks.append(asyncio.create_task(self._heartbeat()))
        await self._raise_ap()

    async def _raise_ap(self) -> None:
        """WIFIO, then WIFI for the credentials, then join while WIFIS is
        polled until it reports 1 — the vendor app's order on firmware 1.8."""
        since = self.cmd.mark()
        self._raised = True
        raised_at = time.monotonic()
        if await self.cmd.request("WIFIO", "WIFIO", timeout=5.0) is None:
            raise WifiTransferError("The device did not acknowledge APP&WIFIO.")
        if self.ssid is None:
            creds = await self.cmd.request("WIFI", "WIFI", timeout=5.0, accept=lambda v: "&" in v)
            if not creds:
                raise WifiTransferError("The device did not report its WiFi credentials.")
            self.ssid, self.password = creds.split("&", 1)
            await self.host_wifi.prepare(self.ssid, self.password)
            self._prepared = True
        self.log(f"Access point {self.ssid} is coming up; joining it...")
        poll = asyncio.create_task(self._poll_status())
        try:
            if not await self.host_wifi.join(self.ssid, raised_at + self.join_timeout):
                raise WifiTransferError(
                    f"Could not join {self.ssid} within {self.join_timeout:g}s."
                )
            remaining = raised_at + self.ready_timeout - time.monotonic()
            ready = await self.cmd.wait_for_message(
                "WIFIS", since, timeout=max(0.1, remaining), accept=lambda v: v.strip() == "1"
            )
            if ready is None:
                raise WifiTransferError("The device never reported a client (WIFIS=1).")
        finally:
            poll.cancel()
        self.connections = 0
        self.ap_starts += 1
        self.log(f"On {self.ssid}; the device reports the client.")

    async def cycle(self) -> None:
        """Restart the access point: the device serves only
        `files_per_session` transfer connections per AP session."""
        self.log("Restarting the access point for the next files...")
        await self.cmd.request("WIFIC", "WIFIC", timeout=5.0)
        self._raised = False
        await self.host_wifi.leave()
        await asyncio.sleep(2.0)
        await self._raise_ap()

    async def download(self, recording, out_path: str | Path,
                       progress_callback: Callable[[int, int], None] | None = None) -> TransferResult:
        """Transfer one recording to `out_path` over WiFi."""
        if self.connections >= self.files_per_session:
            await self.cycle()
        # The connection must be open BEFORE the switch: APP&U&WIFI with no
        # client connected hangs the device until it reports MCU&SHUT.
        try:
            reader, writer = await open_transfer_socket(self.host, self.port,
                                                        wait=self.connect_wait)
        except BaseException:
            # Not listening on this AP any more; the next file gets a fresh one.
            self.connections = self.files_per_session
            raise
        self.connections += 1
        started = time.monotonic()
        try:
            value = await self.cmd.request(
                f"U&{recording.date}&{recording.timestamp}", "U", timeout=10.0,
                accept=lambda v: v.strip().isdigit(),
            )
            if value is None:
                raise WifiTransferError("The device did not answer the file request (MCU&U).")
            size = int(value)
            await asyncio.sleep(self.switch_delay)
            switched = await self.cmd.send_nowait("U&WIFI")
            marker_ok = await receive_file(
                reader, size, out_path,
                first_byte_timeout=self.first_byte_timeout,
                idle_timeout=self.idle_timeout,
                progress_callback=progress_callback,
            )
            if await self.cmd.wait_for_message("OFF", switched, timeout=10.0) is None:
                self.log("The device did not report the end of the transfer (MCU&OFF).")
        except BaseException:
            # The device's state is unknown now; start the next file on a
            # fresh access point rather than gamble on this one.
            self.connections = self.files_per_session
            raise
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
        return TransferResult(Path(out_path), size, time.monotonic() - started, marker_ok)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._raised and self.cmd.connected:
            try:
                await self.cmd.request("WIFIC", "WIFIC", timeout=5.0)
            except Exception:
                pass
        self._raised = False
        if self._audio and self.cmd.connected:
            await self.cmd.stop_audio_sink()
        self._audio = False
        if self._prepared:
            self._prepared = False
            try:
                await self.host_wifi.restore()
            except Exception as e:
                console.print(f"[yellow]Could not restore this machine's WiFi: {e}[/yellow]")

    async def _heartbeat(self) -> None:
        """APP&WPING every few seconds while the AP is up, as the vendor app does."""
        while True:
            await asyncio.sleep(self.heartbeat)
            if self.cmd.connected:
                try:
                    await self.cmd.send_nowait("WPING")
                except Exception:
                    pass

    async def _poll_status(self) -> None:
        while True:
            if self.cmd.connected:
                try:
                    await self.cmd.send_nowait("WIFIS")
                except Exception:
                    pass
            await asyncio.sleep(self.status_interval)
