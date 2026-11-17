"""`pocket-libre doctor`: check the setup and say what to fix.

Each check returns a Check with a status:
  ok    nothing to do
  info  optional, not set up (diarization, say)
  warn  works, but something is off or missing
  fail  a command that needs this won't work
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from pocket_libre import __version__
from pocket_libre.config import (
    CONFIG_FILE,
    get_output_dir,
    resolve_address,
    resolve_hf_token,
    resolve_session_key,
)

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"

# The newest recordings checked for the firmware 1.8.9 duplicates (#42).
REPAIR_SCAN_LIMIT = 200
REPAIR_SCAN_BYTES = 64 * 1024


@dataclass
class Check:
    area: str
    status: str
    message: str
    fix: str = ""


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# ── Local checks ───────────────────────────────


def check_python() -> Check:
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro}, pocket-libre {__version__}"
    if v < (3, 10):
        return Check("Python", FAIL, text, "Pocket Libre needs Python 3.10 or newer.")
    return Check("Python", OK, text)


def check_config(config_error: str | None = None) -> Check:
    if config_error:
        return Check("Config", FAIL, f"{CONFIG_FILE} doesn't parse: {config_error}",
                     "Fix the file by hand, or move it aside and run: pocket-libre setup")
    if not CONFIG_FILE.exists():
        return Check("Config", WARN, f"No config file at {CONFIG_FILE}",
                     "Run: pocket-libre setup  (or pocket-libre pair for a reset device)")
    if os.name == "posix":
        mode = CONFIG_FILE.stat().st_mode & 0o777
        if mode & 0o077:
            return Check("Config", WARN,
                         f"{CONFIG_FILE} is readable by others (mode {mode:o}); it holds "
                         "your session key, which is also the device's WiFi password",
                         f"chmod 600 {CONFIG_FILE}")
    return Check("Config", OK, str(CONFIG_FILE))


def check_device(config: dict) -> Check:
    address = resolve_address(config)
    key = resolve_session_key(config)
    if not address:
        return Check("Device", FAIL, "No device address set",
                     "Run: pocket-libre setup  (or pocket-libre scan to find it)")
    if not key:
        return Check("Device", FAIL, f"Address {address}, but no session key",
                     "Run: pocket-libre setup  (or pocket-libre pair for a reset device)")
    if len(key) != 16:
        return Check("Device", WARN, f"Address {address}; the session key is {len(key)} "
                     "characters, where keys are 16", "Check the key: pocket-libre setup")
    return Check("Device", OK, f"Address {address}, session key set")


def check_output(config: dict) -> list[Check]:
    root = Path(get_output_dir(config))
    if not root.exists():
        parent = next((p for p in root.parents if p.exists()), None)
        if parent is None or not os.access(parent, os.W_OK):
            return [Check("Recordings", FAIL, f"{root} doesn't exist and can't be created",
                          "pocket-libre config --set output.directory=<a folder you own>")]
        return [Check("Recordings", OK, f"{root} (created on the first download)")]
    if not os.access(root, os.W_OK):
        return [Check("Recordings", FAIL, f"{root} isn't writable",
                      "Fix its permissions, or set output.directory to another folder")]

    mp3s = sorted(root.glob("*/*.mp3"), key=lambda p: p.name, reverse=True)
    transcripts = len(list(root.glob("*/*_transcript.txt")))
    checks = [Check("Recordings", OK, f"{root}: {len(mp3s)} recording(s), "
                    f"{transcripts} transcript(s)")]

    unverified = []
    for downloads in root.glob("*/.downloads.json"):
        try:
            entries = json.loads(downloads.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entries, dict):
            unverified += [f"{downloads.parent.name}/{n}" for n, e in entries.items()
                           if isinstance(e, dict) and e.get("verified") is False]
    if unverified:
        checks.append(Check(
            "Recordings", WARN,
            f"{len(unverified)} download(s) couldn't be checked against the device's size "
            f"(e.g. {unverified[0]}); they are never deleted from the device",
            "The next sync, download-all or watch downloads them again."))

    from pocket_libre.mp3repair import remove_duplicates
    affected = []
    for path in mp3s[:REPAIR_SCAN_LIMIT]:
        # The duplicates come every ~4 KB, so the start of a file shows them.
        try:
            with open(path, "rb") as fh:
                head = fh.read(REPAIR_SCAN_BYTES)
        except OSError:
            continue
        if remove_duplicates(head).changed:
            affected.append(path)
    if affected:
        checks.append(Check(
            "Recordings", WARN,
            f"{len(affected)} recording(s) have the firmware 1.8.9 duplicates that tick "
            f"about once a second (e.g. {affected[0].parent.name}/{affected[0].name})",
            f'Run: pocket-libre repair "{root}"'))
    return checks


def check_transcription() -> list[Check]:
    whisper = _installed("whisper")
    ffmpeg = shutil.which("ffmpeg")
    checks = []
    if whisper:
        checks.append(Check("Transcription", OK, "Whisper installed"))
    else:
        checks.append(Check("Transcription", WARN, "Whisper isn't installed; sync, process "
                            "and watch can download but not transcribe",
                            "pip install openai-whisper"))
    if ffmpeg:
        checks.append(Check("ffmpeg", OK, ffmpeg))
    else:
        checks.append(Check("ffmpeg", FAIL if whisper else WARN,
                            "ffmpeg isn't on PATH; Whisper needs it to read MP3s",
                            "Install it: brew install ffmpeg, apt install ffmpeg, "
                            "or winget install ffmpeg"))
    return checks


def check_diarization(config: dict) -> Check:
    token = resolve_hf_token(config)
    pyannote = _installed("pyannote.audio")
    if token and pyannote:
        return Check("Speakers", OK, "pyannote installed, HuggingFace token set")
    if token:
        return Check("Speakers", WARN, "HuggingFace token set, but pyannote isn't installed",
                     'pip install "pocket-libre[diarize]"')
    return Check("Speakers", INFO, "pyannote not set up; the language model labels "
                 "speakers instead, or everyone is \"Speaker\" without one")


def check_llm(config: dict, probe: bool = True) -> Check:
    from pocket_libre.llm import ANTHROPIC, missing_hint, resolve_llm

    llm = resolve_llm(config)
    if llm is None:
        return Check("Language model", WARN,
                     "None set up: transcripts are saved, but no summaries, analyses or chat",
                     missing_hint(config))
    if llm.provider == ANTHROPIC:
        if not _installed("anthropic"):
            return Check("Language model", FAIL, f"{llm.name}, but the anthropic package "
                         "isn't installed", "pip install anthropic")
        if not llm.base_url and not llm.api_key.startswith("sk-ant-"):
            return Check("Language model", WARN, f"{llm.name}; the Anthropic key doesn't "
                         'start with "sk-ant-"', "Check the key: pocket-libre setup")
        return Check("Language model", OK, f"Anthropic, {llm.name}")
    if not probe:
        return Check("Language model", OK, llm.name)
    return _probe_openai(llm)


def _probe_openai(llm) -> Check:
    """Ask an OpenAI-compatible server for its models: free, and it shows
    both that the server is up and whether the model is there."""
    url = llm.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {llm.api_key}"} if llm.api_key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return Check("Language model", WARN, f"{llm.name}: {url} answered HTTP {e.code}",
                     "Check llm.base_url; it should end in /v1 for most servers.")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return Check("Language model", FAIL, f"{llm.name}: can't reach the server ({reason})",
                     "Start it (ollama serve), or fix llm.base_url.")
    except ValueError:
        return Check("Language model", WARN, f"{llm.name}: {url} didn't answer with JSON",
                     "Check llm.base_url; it should end in /v1 for most servers.")
    models = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
    if models and llm.model not in models and f"{llm.model}:latest" not in models:
        shown = ", ".join(models[:8]) + (" ..." if len(models) > 8 else "")
        return Check("Language model", FAIL,
                     f"{llm.name}: the server doesn't have this model. It has: {shown}",
                     f"ollama pull {llm.model}  (or set llm.model to one it has)")
    return Check("Language model", OK, f"{llm.name}, server up")


# ── Bluetooth ──────────────────────────────────


def check_bluetooth_adapter() -> Check | None:
    """Linux only: is there a powered adapter? Elsewhere the scan tells."""
    if not sys.platform.startswith("linux"):
        return None
    if not shutil.which("bluetoothctl"):
        return Check("Bluetooth", WARN, "bluetoothctl not found; is BlueZ installed?",
                     "Install BlueZ (the bluez package) and start bluetoothd.")
    try:
        out = subprocess.run(["bluetoothctl", "--timeout", "3", "show"], capture_output=True,
                             text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return Check("Bluetooth", WARN, "bluetoothctl didn't answer",
                     "Check that bluetoothd is running: systemctl status bluetooth")
    if "Controller" not in out:
        return Check("Bluetooth", FAIL, "No Bluetooth adapter found",
                     "Plug one in, or check rfkill list and systemctl status bluetooth.")
    if "Powered: yes" not in out:
        return Check("Bluetooth", FAIL, "The Bluetooth adapter is off",
                     "bluetoothctl power on  (and rfkill unblock bluetooth if it's blocked)")
    return Check("Bluetooth", OK, "Adapter on")


async def _scan(timeout: float):
    from bleak import BleakScanner
    return await BleakScanner.discover(timeout=timeout, return_adv=True)


def check_scan(config: dict, timeout: float = 8.0) -> Check:
    from pocket_libre.scanner import is_likely_pocket

    address = (resolve_address(config) or "").upper()
    try:
        found = asyncio.run(_scan(timeout))
    except Exception as e:  # bleak raises its own and OS errors here
        return Check("Bluetooth", FAIL, f"Scanning failed: {str(e) or type(e).__name__}",
                     "On macOS, allow Bluetooth for your terminal in System Settings > "
                     "Privacy & Security > Bluetooth. On Linux, check bluetoothd.")
    pockets = {d.address.upper(): (d.name, adv.rssi) for d, adv in found.values()
               if is_likely_pocket(d.name) or d.address.upper() == address}
    if address and address in pockets:
        name, rssi = pockets[address]
        weak = rssi is not None and rssi < -85
        return Check("Bluetooth", WARN if weak else OK,
                     f"Found {name or 'your Pocket'} ({address}), signal {rssi} dBm",
                     "Weak signal: move the device closer for downloads." if weak else "")
    if pockets:
        others = ", ".join(f"{n} ({a})" for a, (n, _) in pockets.items())
        return Check("Bluetooth", WARN, f"The configured device isn't in range, but these are: "
                     f"{others}", "If that's yours: pocket-libre config --set device.address=...")
    return Check("Bluetooth", WARN,
                 f"No Pocket in range after {timeout:.0f}s ({len(found)} other device(s) seen)",
                 "Press the button to wake it, and close the vendor app: the device takes "
                 "one connection at a time.")


def check_connect(config: dict) -> list[Check]:
    """Connect, authenticate and read status: what every device command does."""
    from pocket_libre.commands import PocketCommander

    address = resolve_address(config)
    key = resolve_session_key(config)
    if not (address and key):
        return [Check("Connection", INFO, "Skipped: no device configured")]

    async def run():
        async with PocketCommander(address) as cmd:
            if not await cmd.authenticate(key):
                return None
            return await cmd.get_firmware(), await cmd.get_battery(), await cmd.get_storage()

    try:
        result = asyncio.run(run())
    except Exception as e:
        hint = ("Wake the device and close the vendor app."
                + (" On Linux, after a device reset BlueZ can try Bluetooth Classic and "
                   "time out (#38). Pocket Libre asks BlueZ for the LE bearer, which "
                   "needs Experimental = true under [General] in /etc/bluetooth/main.conf "
                   "on some BlueZ versions; restart bluetoothd after changing it."
                   if sys.platform.startswith("linux") else ""))
        return [Check("Connection", FAIL, f"Couldn't connect: {str(e) or type(e).__name__}", hint)]
    if result is None:
        return [Check("Connection", FAIL, "Connected, but the device refused the session key",
                      "A reset device takes the first key it's sent: pocket-libre pair. "
                      "Otherwise, capture the key again (README, Getting your session key).")]
    firmware, battery, (used, total) = result
    checks = [Check("Connection", OK, f"Authenticated. Firmware {firmware}, battery "
                    f"{battery}%, {used:,} of {total:,} MB used")]
    checks.append(firmware_note(firmware))
    if 0 <= battery < 15:
        checks.append(Check("Connection", WARN, f"Battery at {battery}%",
                            "Charge it before long downloads."))
    return checks


def firmware_note(firmware: str) -> Check:
    version = firmware.strip().lstrip("vV")
    if version.startswith(("1.7", "1.8")):
        return Check("Firmware", OK, f"{firmware}: everything works, WiFi transfer included")
    if version.startswith("1.3"):
        return Check("Firmware", INFO, f"{firmware}: BLE works (slowly); WiFi transfer untested")
    return Check("Firmware", WARN, f"{firmware} hasn't been tested",
                 "Status and explore output in an issue would help: "
                 "https://github.com/shahcolate/pocket-libre/issues")


# ── All of it ──────────────────────────────────


def run_checks(config: dict, config_error: str | None = None, scan: bool = True,
               connect: bool = False, probe_llm: bool = True) -> list[Check]:
    checks = [check_python(), check_config(config_error), check_device(config)]
    checks += check_output(config)
    checks += check_transcription()
    checks.append(check_llm(config, probe=probe_llm))
    checks.append(check_diarization(config))
    adapter = check_bluetooth_adapter()
    if adapter:
        checks.append(adapter)
    if adapter is None or adapter.status == OK:
        if scan:
            checks.append(check_scan(config))
        if connect:
            checks += check_connect(config)
    return checks
