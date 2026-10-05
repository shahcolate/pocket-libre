"""Switching this machine's WiFi to the recorder's access point and back.

Each backend remembers the network it was on, joins the recorder with a temporary profile
named PROFILE, and on restore() rejoins the original network and deletes the profile.

The recorder's SSID is hidden, so the profile is marked as a hidden network: the OS then
probes for the SSID directly instead of waiting for it to show up in a scan. The profile is
created before the access point is raised (prepare), and join() keeps waiting until the
deadline without ever re-issuing a connect while one is still in progress — doing that
disconnects and reconnects, and the recorder drops its transfer socket when its client leaves.

Backends: NetworkManager (Linux, nmcli), netsh (Windows), and manual (any OS: the user joins
by hand while we wait for an address on the recorder's subnet). Ported from the
pocket-wifi-probe research tool, where this logic did every join of the firmware 1.8 runs.
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from xml.sax.saxutils import escape

IS_WINDOWS = sys.platform == "win32"

PROFILE = "pocket-libre"

Log = Callable[[str, str], None]


class HostWifiError(Exception):
    pass


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def text(self) -> str:
        return (self.out + ("\n" + self.err if self.err.strip() else "")).strip()


def _decode(data: bytes) -> str:
    # Windows console tools (netsh) write in the OEM code page.
    return data.decode("oem" if IS_WINDOWS else "utf-8", errors="replace")


async def run(*args: str, timeout: float = 30.0) -> Result:
    """Runs a program and returns its exit code and output; never raises for a failed program."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return Result(127, "", f"{args[0]}: not found")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return Result(124, _decode(out), _decode(err) + f"\n{args[0]}: timed out after {timeout:g}s")
    except BaseException:  # cancelled (Ctrl-C): don't leave the program running
        proc.kill()
        raise
    return Result(proc.returncode, _decode(out), _decode(err))


def local_address_for(host: str) -> str | None:
    """The address this machine would use to reach host (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def same_subnet24(a: str | None, b: str) -> bool:
    return bool(a) and a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


class HostWifi:
    name = "base"

    def __init__(self, host: str, iface: str | None, log: Log):
        self.host = host
        self.iface = iface
        self.log = log
        self.original: str | None = None

    async def setup(self) -> None:
        """Finds the interface and remembers the current network."""

    async def prepare(self, ssid: str, password: str) -> None:
        """Creates the temporary profile for the recorder's network."""

    async def join(self, ssid: str, deadline: float) -> bool:
        raise NotImplementedError

    async def leave(self) -> None:
        """Disconnects from the recorder's network but keeps the profile, to join again later."""

    async def restore(self) -> None:
        """Rejoins the original network and removes the temporary profile."""

    def on_ap(self) -> str | None:
        """Our address on the recorder's subnet, or None."""
        addr = local_address_for(self.host)
        return addr if same_subnet24(addr, self.host) else None

    async def wait_for_address(self, seconds: float) -> str | None:
        end = time.monotonic() + seconds
        while True:
            addr = self.on_ap()
            if addr or time.monotonic() >= end:
                return addr
            await asyncio.sleep(0.5)


class NetworkManager(HostWifi):
    """Linux with NetworkManager (nmcli)."""
    name = "networkmanager"

    async def setup(self) -> None:
        r = await run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device")
        if not r.ok:
            raise HostWifiError(f"nmcli is not usable: {r.text()}")
        devices = [split_terse(line) for line in r.out.splitlines() if line]
        wifi = [d for d in devices if len(d) >= 3 and d[1] == "wifi"]
        if self.iface is None:
            if not wifi:
                raise HostWifiError("No WiFi device found by NetworkManager")
            self.iface = wifi[0][0]
        self.log("wifi", f"using {self.iface}")
        r = await run("nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active")
        for fields in (split_terse(line) for line in r.out.splitlines() if line):
            if len(fields) >= 4 and fields[3] == self.iface:
                self.original = fields[1]
                self.log("wifi", f"currently on '{fields[0]}' ({fields[1]})")
        if self.original is None:
            self.log("wifi", f"{self.iface} is not connected to anything now")

    async def prepare(self, ssid: str, password: str) -> None:
        await run("nmcli", "connection", "delete", PROFILE)
        r = await run(
            "nmcli", "connection", "add", "type", "wifi", "ifname", self.iface,
            "con-name", PROFILE, "ssid", ssid, "autoconnect", "no",
            "802-11-wireless.hidden", "yes",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
            "ipv4.method", "auto", "ipv4.never-default", "yes",
            "ipv6.method", "disabled",
        )
        if not r.ok:
            raise HostWifiError(f"Could not create the WiFi profile: {r.text()}")

    async def join(self, ssid: str, deadline: float) -> bool:
        # Never re-issue "connection up" while one is still in progress: that disconnects and
        # reconnects, and the recorder seems to close its transfer socket when its client leaves.
        attempt = 0
        while time.monotonic() < deadline:
            if await self._state() in ("activating", "activated"):
                if await self.wait_for_address(1):
                    self.log("wifi", f"joined {ssid} on attempt {attempt}")
                    return True
                continue
            attempt += 1
            wait = max(3, int(deadline - time.monotonic()))
            r = await run("nmcli", "--wait", str(wait), "connection", "up", PROFILE, "ifname", self.iface,
                          timeout=wait + 5)
            if await self.wait_for_address(5 if r.ok else 0.5):
                self.log("wifi", f"joined {ssid} on attempt {attempt}")
                return True
            self.log("wifi", f"join attempt {attempt}: {r.text().splitlines()[-1] if r.text() else r.code}")
            if await self._state() not in ("activating", "activated"):
                await run("nmcli", "device", "wifi", "rescan", "ifname", self.iface, "ssid", ssid, timeout=5)
                await asyncio.sleep(1)
        return False

    async def leave(self) -> None:
        await run("nmcli", "connection", "down", PROFILE, timeout=15)

    async def _state(self) -> str:
        """"activating", "activated", … for the temporary profile, or "" when it isn't active."""
        r = await run("nmcli", "-t", "-f", "GENERAL.STATE", "connection", "show", PROFILE, timeout=5)
        return r.out.strip().rpartition(":")[2] if r.ok else ""

    async def restore(self) -> None:
        if self.original:
            r = await run("nmcli", "--wait", "30", "connection", "up", "uuid", self.original, timeout=40)
            self.log("wifi", "rejoined the original network" if r.ok else f"could not rejoin: {r.text()}")
        else:
            await run("nmcli", "connection", "down", PROFILE)
        r = await run("nmcli", "connection", "delete", PROFILE)
        self.log("wifi", "removed the temporary profile" if r.ok else f"could not remove the profile: {r.text()}")


def split_terse(line: str) -> list[str]:
    """Splits a line of `nmcli -t` output, which escapes ':' inside fields as '\\:'."""
    fields, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            fields.append(cur)
            cur = ""
        else:
            cur += ch
    fields.append(cur)
    return fields


class Netsh(HostWifi):
    """Windows (netsh wlan)."""
    name = "netsh"

    async def setup(self) -> None:
        r = await run("netsh", "wlan", "show", "interfaces")
        if not r.ok:
            raise HostWifiError(f"netsh wlan is not usable: {r.text()}")
        info = parse_netsh_interfaces(r.out)
        if not info:
            raise HostWifiError("No WiFi interface found by netsh")
        chosen = next((i for i in info if self.iface in (None, i.get("name"))), None)
        if chosen is None:
            raise HostWifiError(f"No WiFi interface named {self.iface}")
        self.iface = chosen.get("name")
        self.original = chosen.get("profile")
        self.log("wifi", f"using '{self.iface}'")
        self.log("wifi", f"currently on profile '{self.original}'" if self.original
                 else f"'{self.iface}' is not connected to anything now")

    async def prepare(self, ssid: str, password: str) -> None:
        await run("netsh", "wlan", "delete", "profile", f"name={PROFILE}", f"interface={self.iface}")
        xml = windows_profile(ssid, password)
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(xml)
            r = await run("netsh", "wlan", "add", "profile", f"filename={path}",
                          f"interface={self.iface}", "user=current")
        finally:
            os.unlink(path)
        if not r.ok:
            raise HostWifiError(f"Could not add the WiFi profile: {r.text()}")

    async def join(self, ssid: str, deadline: float) -> bool:
        # Only ask again once the interface is disconnected: a second connect while Windows is
        # still associating restarts it, and the recorder seems to drop its socket on that.
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            r = await run("netsh", "wlan", "connect", f"name={PROFILE}", f"ssid={ssid}",
                          f"interface={self.iface}")
            if not r.ok:
                self.log("wifi", f"join attempt {attempt}: {r.text().splitlines()[-1] if r.text() else r.code}")
                await asyncio.sleep(1)
                continue
            # netsh returns as soon as the request is queued; wait for DHCP while it's trying.
            while time.monotonic() < deadline:
                if await self.wait_for_address(1):
                    self.log("wifi", f"joined {ssid} on attempt {attempt}")
                    return True
                if await self._disconnected():
                    self.log("wifi", f"join attempt {attempt}: interface is disconnected again")
                    break
        return False

    async def leave(self) -> None:
        await run("netsh", "wlan", "disconnect", f"interface={self.iface}")

    async def _disconnected(self) -> bool:
        r = await run("netsh", "wlan", "show", "interfaces", timeout=5)
        me = next((i for i in parse_netsh_interfaces(r.out) if i.get("name") == self.iface), {})
        return me.get("state", "").lower() in ("disconnected", "getrennt")

    async def restore(self) -> None:
        if self.original:
            r = await run("netsh", "wlan", "connect", f"name={self.original}", f"interface={self.iface}")
            self.log("wifi", "asked Windows to rejoin the original network" if r.ok
                     else f"could not rejoin: {r.text()}")
        else:
            await run("netsh", "wlan", "disconnect", f"interface={self.iface}")
        r = await run("netsh", "wlan", "delete", "profile", f"name={PROFILE}", f"interface={self.iface}")
        self.log("wifi", "removed the temporary profile" if r.ok else f"could not remove the profile: {r.text()}")


# netsh labels are translated; these cover English and German.
_NETSH_KEYS = {
    "name": "name",
    "profile": "profile", "profil": "profile",
    "ssid": "ssid",
    "state": "state", "status": "state",
}


def parse_netsh_interfaces(text: str) -> list[dict]:
    """The interfaces in `netsh wlan show interfaces` output, as dicts of name/profile/ssid/state."""
    out: list[dict] = []
    for line in text.splitlines():
        m = re.match(r"^\s*([^:]+?)\s*:\s(.*)$", line)
        if not m:
            continue
        key = _NETSH_KEYS.get(m.group(1).strip().lower())
        if key is None:
            continue
        if key == "name":
            out.append({})
        if out:
            out[-1].setdefault(key, m.group(2).strip())
    return out


def windows_profile(ssid: str, password: str) -> str:
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{PROFILE}</name>
  <SSIDConfig>
    <SSID><name>{escape(ssid)}</name></SSID>
    <nonBroadcast>true</nonBroadcast>
  </SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM>
    <security>
      <authEncryption>
        <authentication>WPA2PSK</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>{escape(password)}</keyMaterial>
      </sharedKey>
    </security>
  </MSM>
</WLANProfile>
"""


class Manual(HostWifi):
    """Any OS: the user joins by hand while the probe waits for an address on the subnet."""
    name = "manual"

    async def prepare(self, ssid: str, password: str) -> None:
        print(f"\n  >>> Join WiFi '{ssid}' with password '{password}' as soon as it appears <<<\n",
              flush=True)

    async def join(self, ssid: str, deadline: float) -> bool:
        return bool(await self.wait_for_address(max(0, deadline - time.monotonic())))

    async def leave(self) -> None:
        print("\n  >>> The recorder's WiFi is restarting; rejoin it when it's back <<<\n", flush=True)

    async def restore(self) -> None:
        print("\n  >>> Switch your WiFi back to your usual network now <<<\n", flush=True)


def backend(kind: str, host: str, iface: str | None, log: Log) -> HostWifi:
    if kind == "auto":
        kind = "netsh" if IS_WINDOWS else "networkmanager" if sys.platform.startswith("linux") else "manual"
    cls = {"networkmanager": NetworkManager, "netsh": Netsh, "manual": Manual}.get(kind)
    if cls is None:
        raise HostWifiError(f"Unknown WiFi backend {kind}")
    return cls(host, iface, log)
