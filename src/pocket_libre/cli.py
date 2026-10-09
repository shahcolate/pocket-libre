"""CLI entry point for Pocket Libre."""

import asyncio
import os
import time
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from pocket_libre.capture import capture_audio
from pocket_libre.commands import PocketCommander, Recording
from pocket_libre.config import (
    CONFIG_FILE,
    get,
    get_output_dir,
    load_config,
    resolve_address,
    resolve_anthropic_key,
    resolve_hf_token,
    resolve_session_key,
    save_config,
)
from pocket_libre.explorer import explore_device
from pocket_libre.probe import probe_characteristic
from pocket_libre.scanner import scan_devices
from pocket_libre.sniffer import sniff_all
from pocket_libre.transcribe import transcribe_audio

console = Console()


def _require_address(address: str | None, config: dict) -> str:
    """Resolve device address or exit with helpful error."""
    addr = resolve_address(config, address)
    if not addr:
        raise click.UsageError(
            "No device address. Run 'pocket-libre setup' or pass --address.\n"
            "Find your device with: pocket-libre scan --filter pkt"
        )
    return addr


def _require_session_key(session_key: str | None, config: dict) -> str:
    """Resolve session key or exit with helpful error."""
    sk = resolve_session_key(config, session_key)
    if not sk:
        raise click.UsageError(
            "No session key. Run 'pocket-libre setup' or pass --key.\n"
            "Capture yours from the vendor app's APP&SK& write (see PROTOCOL.md)."
        )
    return sk


def _prompt_secret(label: str, existing: str) -> str | None:
    """Prompt for a secret, showing a masked default when one exists.

    Returns the stripped new value, or None to keep the existing one.
    """
    masked = f"...{existing[-4:]}" if len(existing) > 4 else ""
    value = click.prompt(label, default=masked or "", show_default=bool(masked))
    if value and not value.startswith("..."):
        return value.strip()
    return None


@click.group()
@click.version_option()
@click.pass_context
def cli(ctx):
    """Pocket Libre: Liberate your Pocket AI recorder from the cloud."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config()


# ── Setup & Config ──────────────────────────────


@cli.command()
@click.pass_context
def setup(ctx):
    """Interactive setup wizard. Configures device, API keys, and preferences."""
    console.print(Panel(
        "[bold]Pocket Libre Setup[/bold]\n\n"
        "This will configure your device address, API keys, and preferences.\n"
        "Settings are saved to ~/.pocket-libre/config.toml\n"
        "Press Enter to accept defaults. Leave blank to skip.",
        border_style="cyan",
    ))

    config = ctx.obj["config"]
    # Carry over every existing section so re-running setup never wipes
    # settings the wizard doesn't manage (e.g. [analysis]).
    new_config = {section: dict(values) for section, values in config.items()
                  if isinstance(values, dict)}
    for section in ("device", "api", "output", "defaults"):
        new_config.setdefault(section, {})

    # Step 1: Device address
    console.print("\n[bold cyan]Step 1: Device Address[/bold cyan]")
    console.print("[dim]Scanning for Pocket devices...[/dim]")

    try:
        from bleak import BleakScanner
        devices = asyncio.run(BleakScanner.discover(timeout=5.0, return_adv=True))
        pocket_devices = []
        for d, _adv in devices.values():
            if d.name and "pkt" in d.name.lower():
                pocket_devices.append(d)
                console.print(f"  Found: [green]{d.name}[/green] ({d.address})")

        if pocket_devices:
            default_addr = pocket_devices[0].address
            console.print(f"\n  [dim]Auto-detected: {default_addr}[/dim]")
        else:
            default_addr = new_config["device"].get("address", "")
            if not default_addr:
                console.print("  [yellow]No Pocket device found. Make sure it's awake (press button).[/yellow]")
    except Exception:
        default_addr = new_config["device"].get("address", "")
        console.print("  [yellow]BLE scan failed. You can enter the address manually.[/yellow]")

    addr = click.prompt("  Device address", default=default_addr or "", show_default=bool(default_addr))
    if addr:
        new_config["device"]["address"] = addr

    console.print("\n  [bold]Session Key[/bold] (16 characters, authenticates the BLE connection)")
    console.print("  [dim]Capture it from the vendor app's APP&SK& write (see PROTOCOL.md), or leave it\n"
                  "  blank and pair a reset Pocket without the app: pocket-libre pair[/dim]")
    sk = _prompt_secret("  Session key", new_config["device"].get("session_key", ""))
    if sk:
        if len(sk) != 16:
            console.print("  [yellow]Warning: session keys are 16 characters — double-check the value.[/yellow]")
        new_config["device"]["session_key"] = sk

    # Step 2: API keys
    console.print("\n[bold cyan]Step 2: API Keys[/bold cyan]")

    console.print("\n  [bold]Anthropic API Key[/bold] (for AI summaries, mind maps, entity extraction)")
    console.print("  [dim]Get one at: https://console.anthropic.com/settings/keys[/dim]")
    console.print("  [dim]Cost: ~$0.003 per recording (summary + entities + mind map)[/dim]")
    console.print("  [dim]Transcription works without this key (runs locally).[/dim]")
    anthropic_key = _prompt_secret("  Anthropic API key", new_config["api"].get("anthropic_key", ""))
    if anthropic_key:
        new_config["api"]["anthropic_key"] = anthropic_key

    console.print("\n  [bold]HuggingFace Token[/bold] (optional, for speaker identification)")
    console.print("  [dim]Get one at: https://huggingface.co/settings/tokens[/dim]")
    console.print("  [dim]Free tier works. Enables speaker diarization.[/dim]")
    hf_token = _prompt_secret("  HuggingFace token", new_config["api"].get("hf_token", ""))
    if hf_token:
        new_config["api"]["hf_token"] = hf_token

    # Step 3: Output directory
    console.print("\n[bold cyan]Step 3: Output Directory[/bold cyan]")
    default_dir = new_config["output"].get("directory", "~/Pocket Libre")
    out_dir = click.prompt("  Save recordings to", default=default_dir)
    new_config["output"]["directory"] = out_dir

    # Step 4: Defaults
    console.print("\n[bold cyan]Step 4: Preferences[/bold cyan]")
    default_style = new_config["defaults"].get("summary_style", "meeting")
    style = click.prompt(
        "  Summary style",
        type=click.Choice(["meeting", "notes", "call", "raw"]),
        default=default_style,
    )
    new_config["defaults"]["summary_style"] = style

    default_model = new_config["defaults"].get("whisper_model", "base.en")
    model = click.prompt(
        "  Whisper model",
        type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
        default=default_model,
    )
    new_config["defaults"]["whisper_model"] = model

    # Save
    save_config(new_config)
    console.print(f"\n[green]Config saved to {CONFIG_FILE}[/green]")

    # Test connection
    if new_config["device"].get("address") and new_config["device"].get("session_key"):
        if click.confirm("\n  Test connection to device?", default=True):
            try:
                async def _test():
                    sk = new_config["device"]["session_key"]
                    async with PocketCommander(new_config["device"]["address"]) as cmd:
                        ok = await cmd.authenticate(sk)
                        if ok:
                            battery = await cmd.get_battery()
                            console.print(f"  [green]Connected! Battery: {battery}%[/green]")
                        else:
                            console.print("  [yellow]Connected but authentication failed.[/yellow]")
                asyncio.run(_test())
            except Exception as e:
                console.print(f"  [yellow]Connection failed: {e}[/yellow]")
                console.print("  [dim]Make sure the device is awake (press button).[/dim]")

    if not new_config["device"].get("session_key"):
        console.print(Panel(
            "[bold yellow]No session key configured.[/bold yellow]\n\n"
            "Device commands (status, list, download, sync, web) won't work\n"
            "until you set one. Either pair a reset Pocket without the vendor app:\n"
            "  pocket-libre pair\n"
            "or capture the app's key from its APP&SK& write (see PROTOCOL.md) and run:\n"
            "  pocket-libre config --set device.session_key=YOUR-KEY",
            border_style="yellow",
        ))

    console.print(Panel(
        "[bold green]Setup complete![/bold green]\n\n"
        "Get started:\n"
        "  [bold]pocket-libre web[/bold]     Open the web interface (recommended)\n"
        "  [bold]pocket-libre sync[/bold]    Download + transcribe + summarize all recordings\n\n"
        "Other commands:\n"
        "  [bold]pocket-libre status[/bold]  Check device battery & storage\n"
        "  [bold]pocket-libre list[/bold]    List recordings on device",
        border_style="green",
    ))


@cli.command("config")
@click.option("--path", "show_path", is_flag=True, help="Print config file path.")
@click.option("--set", "set_value", default=None, help="Set a value: section.key=value")
@click.pass_context
def show_config(ctx, show_path: bool, set_value: str | None):
    """Show or edit configuration."""
    if show_path:
        console.print(str(CONFIG_FILE))
        return

    if set_value:
        if "=" not in set_value or "." not in set_value.split("=")[0]:
            raise click.UsageError("Format: --set section.key=value (e.g., device.address=ABC123)")
        path, value = set_value.split("=", 1)
        section, key = path.split(".", 1)
        config = ctx.obj["config"]
        if section not in config:
            config[section] = {}
        config[section][key] = value
        save_config(config)
        console.print(f"[green]Set {section}.{key}[/green]")
        return

    config = ctx.obj["config"]
    if not config:
        console.print("[yellow]No config file found.[/yellow] Run: [bold]pocket-libre setup[/bold]")
        return

    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        header = f"[{section}]"
        console.print(f"\n[bold cyan]{escape(header)}[/bold cyan]")
        for key, val in values.items():
            display = val
            if key in ("anthropic_key", "hf_token", "session_key") and val and len(str(val)) > 8:
                display = f"...{str(val)[-4:]}"
            console.print(f"  {key} = {display}")


# ── Device Commands ─────────────────────────────


@cli.command()
@click.option("--timeout", default=10.0, help="Scan duration in seconds.")
@click.option("--filter", "name_filter", default=None, help="Filter devices by name (case-insensitive).")
def scan(timeout: float, name_filter: str | None):
    """Scan for nearby BLE devices. Use this to find your Pocket."""
    asyncio.run(scan_devices(timeout=timeout, name_filter=name_filter))


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.pass_context
def explore(ctx, address: str | None):
    """Connect to a device and dump all GATT services and characteristics."""
    address = _require_address(address, ctx.obj["config"])
    asyncio.run(explore_device(address=address))


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--duration", default=15, help="Sniff duration in seconds.")
@click.pass_context
def sniff(ctx, address: str | None, duration: int):
    """Subscribe to ALL notify characteristics and show what's streaming."""
    address = _require_address(address, ctx.obj["config"])
    asyncio.run(sniff_all(address=address, duration=duration))


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--char", "char_uuid", default=None, help="Specific write characteristic UUID to probe.")
@click.option("--data", default=None, help="Hex bytes to send (e.g., '01' or '01ff02').")
@click.option("--wait", default=2.0, help="Seconds to wait for responses after each write.")
@click.pass_context
def probe(ctx, address: str | None, char_uuid: str | None, data: str | None, wait: float):
    """Probe write characteristics to discover command-response mappings."""
    address = _require_address(address, ctx.obj["config"])
    parsed_data = bytes.fromhex(data) if data else None
    asyncio.run(
        probe_characteristic(
            address=address,
            char_uuid=char_uuid,
            data=parsed_data,
            wait=wait,
        )
    )


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--output", default="recording.raw", help="Output file path.")
@click.option("--duration", default=30, help="Max capture duration in seconds.")
@click.option("--char-uuid", default=None, help="UUID of the audio characteristic.")
@click.pass_context
def capture(ctx, address: str | None, output: str, duration: int, char_uuid: str | None):
    """Connect to Pocket and capture audio data from a BLE characteristic."""
    address = _require_address(address, ctx.obj["config"])
    asyncio.run(
        capture_audio(
            address=address,
            output_path=output,
            duration=duration,
            char_uuid=char_uuid,
        )
    )


@cli.command()
@click.option("--input", "input_path", required=True, help="Path to audio file (.wav, .raw).")
@click.option("--model", default="base.en",
              type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
              help="Whisper model size.")
@click.option("--output", default=None, help="Save transcript to file.")
@click.option("--format", "output_format", default="text",
              type=click.Choice(["text", "json", "srt"]), help="Output format.")
def transcribe(input_path: str, model: str, output: str | None, output_format: str):
    """Transcribe an audio file locally using Whisper. No cloud, no network."""
    transcribe_audio(
        input_path=input_path,
        model_name=model,
        output_path=output,
        output_format=output_format,
    )


@cli.command()
@click.option("--input", "input_path", required=True, help="Path to raw audio capture.")
@click.option("--output", default="recording.wav", help="Output .wav file path.")
@click.option("--sample-rate", default=16000, help="Sample rate in Hz.")
# Choice values must be strings: on click 8.1 (our declared floor) a
# non-string choice raises AttributeError during conversion, which fired
# even when the flag was omitted because defaults are converted too.
@click.option("--bit-depth", default="16", type=click.Choice(["8", "16"]),
              help="Bits per sample.")
@click.option("--channels", default=1, help="Number of audio channels.")
def convert(input_path: str, output: str, sample_rate: int, bit_depth: str, channels: int):
    """Convert raw audio capture to WAV format for playback or transcription."""
    from pocket_libre.audio import raw_to_wav
    raw_to_wav(
        input_path=input_path,
        output_path=output,
        sample_rate=sample_rate,
        bit_depth=int(bit_depth),
        channels=channels,
    )


# ── Device Status & Recordings ──────────────────


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key for authentication.")
@click.pass_context
def status(ctx, address: str | None, session_key: str | None):
    """Connect to Pocket and show device status (battery, firmware, storage)."""
    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)

    async def _run():
        async with PocketCommander(address) as cmd:
            console.print("[dim]Authenticating...[/dim]")
            ok = await cmd.authenticate(session_key)
            if not ok:
                console.print("[red]Authentication failed.[/red]")
                return

            battery = await cmd.get_battery()
            firmware = await cmd.get_firmware()
            used, total = await cmd.get_storage()
            state = await cmd.get_state()
            await cmd.set_time()

            state_names = {0: "Idle", 1: "Recording"}
            pct = 100 * used // total if total > 0 else 0
            console.print(Panel(
                f"[bold]Battery:[/bold] {battery}%\n"
                f"[bold]Firmware:[/bold] {firmware}\n"
                f"[bold]Storage:[/bold] {used:,} / {total:,} MB ({pct}% used)\n"
                f"[bold]State:[/bold] {state_names.get(state, f'Unknown ({state})')}\n"
                f"[bold]Time:[/bold] Synced to host",
                title="Pocket Status",
                border_style="cyan",
            ))

    asyncio.run(_run())


# After pairing, the wait before each of the three tries to reconnect.
PAIR_RECONNECT_DELAY = 2.0


async def _scan_pockets(timeout: float = 5.0) -> list[tuple[str, str]]:
    """(name, address) of every likely Pocket in range."""
    from bleak import BleakScanner

    from pocket_libre.scanner import is_likely_pocket

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [(d.name, d.address) for d, _adv in found.values() if is_likely_pocket(d.name)]


@cli.command()
@click.option("--address", default=None,
              help="BLE address of the Pocket (default: the one Pocket in range).")
@click.option("--key", "session_key", default=None,
              help="Session key to pair with (default: a new random one).")
@click.option("--yes", "assume_yes", is_flag=True,
              help="Replace a configured device without asking.")
@click.pass_context
def pair(ctx, address: str | None, session_key: str | None, assume_yes: bool):
    """Pair a reset Pocket with a session key of its own, without the vendor app.

    \b
    After a hardware reset the device takes the first session key it is sent
    and refuses every other one from then on. To reset: triple-click the side
    button (the LED blinks red), then press and hold it until the red
    blinking stops; the LED then pulses blue.

    \b
    pair sends the reset device a new random key (or --key) and saves it, with
    the device's address, as this machine's device in the config. The vendor
    app pairs the same way, so a device paired here is not reachable from the
    app until it is reset and paired there.
    """
    from pocket_libre import config as cfg
    from pocket_libre.commands import generate_session_key, is_valid_session_key

    if session_key is not None and not is_valid_session_key(session_key):
        raise click.BadParameter("must be 16 letters and digits", param_hint="--key")
    key = session_key or generate_session_key()

    if not address:
        console.print("[dim]Looking for a Pocket in range...[/dim]")
        pockets = asyncio.run(_scan_pockets())
        if not pockets:
            console.print("[red]No Pocket found.[/red] Make sure it is reset (the LED "
                          "pulses blue), nearby, and not connected to a phone.")
            raise SystemExit(1)
        if len(pockets) > 1:
            listing = "\n".join(f"  {name}  {addr}" for name, addr in pockets)
            raise click.UsageError(f"Several Pockets in range; pick one with --address:\n{listing}")
        name, address = pockets[0]
        console.print(f"Found {escape(name)} ({address})")

    config = ctx.obj["config"]
    device = config.get("device", {}) if isinstance(config.get("device"), dict) else {}
    if device.get("session_key") and not assume_yes:
        click.confirm(f"This replaces the configured device ({device.get('address') or 'no address'}) "
                      "and its session key in the config. Continue?", abort=True)

    # Saved before it is sent: once the device takes the key, it refuses any
    # other, so the key must not be lost if this run dies right after.
    previous = cfg.CONFIG_FILE.read_text(encoding="utf-8") if cfg.CONFIG_FILE.exists() else None
    new_config = {section: dict(values) for section, values in config.items()
                  if isinstance(values, dict)}
    new_config.setdefault("device", {}).update(address=address, session_key=key)
    cfg.save_config(new_config)

    def restore() -> None:
        if previous is None:
            cfg.CONFIG_FILE.unlink(missing_ok=True)
        else:
            cfg.CONFIG_FILE.write_text(previous, encoding="utf-8")

    state = {"sent": False, "paired": None}

    async def _pair() -> bool | None:
        async with PocketCommander(address) as cmd:
            state["sent"] = True
            state["paired"] = await cmd.login(key)
            return state["paired"]

    async def _check() -> tuple[int, str] | None:
        # The device drops the connection about a second after it takes its
        # first key (the vendor app's first connection times out the same way),
        # so the clock and a check go over a new connection with the new key.
        for _ in range(3):
            await asyncio.sleep(PAIR_RECONNECT_DELAY)
            try:
                async with PocketCommander(address) as cmd:
                    if await cmd.authenticate(key):
                        await cmd.set_time()
                        return await cmd.get_battery(), await cmd.get_firmware()
            except Exception:
                pass
        return None

    try:
        paired = asyncio.run(_pair())
    except Exception as e:
        if not state["sent"]:
            restore()
            console.print(f"[red]Could not connect to {address}: {e}[/red] The config is "
                          "unchanged.")
        elif state["paired"]:
            paired = True
        else:
            console.print(f"[red]The connection failed during pairing: {e}[/red] The device "
                          "may have taken the key, so it stays in the config: check with "
                          "`pocket-libre status`.")
        if not state["paired"]:
            raise SystemExit(1) from None
    if paired:
        checked = asyncio.run(_check())
        if checked:
            battery, firmware = checked
            console.print(Panel(
                f"[bold]Paired[/bold] {address}\n"
                f"[bold]Battery:[/bold] {battery}%   [bold]Firmware:[/bold] {firmware}\n"
                f"The session key is saved in {cfg.CONFIG_FILE}.",
                title="Pocket paired", border_style="green",
            ))
        else:
            console.print(f"[green]Paired[/green] {address}; the session key is saved in "
                          f"{cfg.CONFIG_FILE}. The device did not take a second connection "
                          "yet (it drops the first one after pairing); check with "
                          "`pocket-libre status`.")
        return
    if paired is False:
        restore()
        console.print("[red]This Pocket already has a session key[/red] and refused the "
                      "new one; the config is unchanged. To pair it anyway, do a hardware "
                      "reset first (see `pocket-libre pair --help`).")
        raise SystemExit(1)
    if paired is None:
        console.print("[yellow]The Pocket did not answer the pairing request.[/yellow] It may "
                      "have taken the key anyway, so the new key stays in the config: check "
                      "with `pocket-libre status`, and if that fails, reset the device and "
                      "run pair again.")
        raise SystemExit(1)


@cli.command()
@click.argument("mode", required=False, type=click.Choice(["on", "off", "status"]), default="status")
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key for authentication.")
@click.pass_context
def usb(ctx, mode: str, address: str | None, session_key: str | None):
    """Show or set USB mass storage mode (on, off, status)."""
    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)

    async def _run():
        async with PocketCommander(address) as cmd:
            console.print("[dim]Authenticating...[/dim]")
            ok = await cmd.authenticate(session_key)
            if not ok:
                console.print("[red]Authentication failed.[/red]")
                raise SystemExit(1)

            state = None
            if mode != "status":
                state = await cmd.set_usb(mode == "on")
            # Fall back to querying when the set reply carried no state.
            if state is None:
                state = await cmd.get_usb()

            if state is None:
                console.print("[yellow]Device did not report a USB state.[/yellow]")
                raise SystemExit(1)
            console.print(f"[bold]USB mass storage:[/bold] {'on' if state else 'off'}")
            if mode != "status" and state != (mode == "on"):
                console.print(f"[red]Device did not switch USB {mode}.[/red]")
                raise SystemExit(1)

    asyncio.run(_run())


@cli.command("list")
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--date", default=None, help="List recordings for a specific date (YYYY-MM-DD).")
@click.pass_context
def list_recordings(ctx, address: str | None, session_key: str | None, date: str | None):
    """List recordings stored on the Pocket device."""
    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)

    from rich.table import Table

    async def _run():
        async with PocketCommander(address) as cmd:
            console.print("[dim]Authenticating...[/dim]")
            if not await cmd.authenticate(session_key):
                console.print("[red]Auth failed.[/red]")
                return

            if date:
                dates = [date]
            else:
                dates = await cmd.list_dirs()
                console.print(f"[bold]{len(dates)} recording date(s) on device[/bold]\n")

            table = Table(title="Recordings")
            table.add_column("Date", style="cyan")
            table.add_column("Timestamp", style="yellow")
            table.add_column("Duration", justify="right")
            table.add_column("~Size", justify="right")

            total_files = 0
            for d in dates:
                recs = await cmd.list_files(d)
                for r in recs:
                    total_files += 1
                    mins, secs = divmod(max(r.duration_s, 0), 60)
                    table.add_row(
                        r.date, r.timestamp,
                        f"{mins}m{secs:02d}s",
                        f"{r.estimated_bytes // 1024:,} KB",
                    )

            console.print(table)
            console.print(f"\n[bold]{total_files} recording(s) total[/bold]")

    asyncio.run(_run())


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--date", required=True, help="Recording date (YYYY-MM-DD).")
@click.option("--timestamp", required=True, help="Recording timestamp (YYYYMMDDHHmmss).")
@click.option("--output", default=None, help="Output file path (default: <timestamp>.mp3).")
@click.pass_context
def download(ctx, address: str | None, session_key: str | None,
             date: str, timestamp: str, output: str | None):
    """Download a specific recording over BLE."""
    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)
    from pocket_libre.commands import download_with_retry

    async def _run():
        rec = Recording(date=date, timestamp=timestamp, duration_s=0)
        console.print(f"[bold]Downloading {rec.date}/{rec.timestamp}...[/bold]")

        def progress(current, total):
            if total > 0:
                pct = 100 * current // total
                console.print(f"\r[dim]{current:,}/{total:,} bytes ({pct}%)[/dim]", end="")

        data = await download_with_retry(address, session_key, rec, progress_callback=progress)
        console.print()

        if not data:
            console.print("[red]Download failed; nothing saved.[/red]")
            return

        # Not recorded: delete --downloaded only looks in <output dir>/<date>/.
        out_path = Path(output) if output else Path(f"{timestamp}.mp3")
        out_path.write_bytes(data)
        console.print(f"[bold green]Saved {len(data):,} bytes to {out_path}[/bold green]")

    asyncio.run(_run())


def _plan_downloads(jobs: list[tuple[Recording, Path]], listed: int,
                    since: str | None = None, overwrite: bool = False,
                    hint: str = "") -> tuple[list[tuple[Recording, Path]], list[Path]]:
    """Split (recording, path) jobs into those to download and the paths that
    already have a copy, checking each path once, and say so in one line.

    `listed` is how many recordings the device listed before `since` filtered
    them. `hint` follows the "all already downloaded" message, e.g. how to
    download again. Returns (todo, existing).
    """
    from pocket_libre.commands import needs_download

    todo, existing = [], []
    for rec, path in jobs:
        # A copy marked unverified is downloaded again (see save_recording).
        if not overwrite and not needs_download(path):
            existing.append(path)
        else:
            todo.append((rec, path))
    if not listed:
        console.print("[yellow]No recordings on the device.[/yellow]")
    elif not jobs:
        console.print(f"[yellow]No recordings from {since} on ({listed} on the device).[/yellow]")
    elif not todo:
        console.print(f"{len(jobs)} recording(s) to consider, all already downloaded{hint}.")
    else:
        console.print(f"[bold]{len(todo)} recording(s) to download[/bold]"
                      + (f" ({len(existing)} already downloaded)" if existing else ""))
    return todo, existing


@cli.command("download-all")
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--since", default=None,
              help="Only recordings from this date (YYYY-MM-DD) on: the date of the "
                   "recording's folder on the device, in UTC.")
@click.option("--process", "do_process", is_flag=True, help="Transcribe and summarize after download.")
@click.option("--output-dir", default=None, help="Output directory.")
@click.option("--delete-after", is_flag=True,
              help="Afterwards, delete every recording with a verified local copy from the device.")
@click.pass_context
def download_all(ctx, address: str | None, session_key: str | None,
                 since: str | None, do_process: bool, output_dir: str | None,
                 delete_after: bool):
    """Download all recordings from the device.

    \b
    Saves to ~/Pocket Libre/<date>/<timestamp>.mp3 (or configured directory).
    Use --process to also transcribe and summarize each recording.
    """
    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)
    out_root = Path(get_output_dir(config, output_dir))
    from pocket_libre.commands import download_checked, save_recording

    async def _run():
        async with PocketCommander(address) as cmd:
            console.print("[dim]Authenticating...[/dim]")
            if not await cmd.authenticate(session_key):
                console.print("[red]Auth failed.[/red]")
                return [], 1
            all_recs = await cmd.list_all_recordings()

        listed = len(all_recs)
        if since:
            all_recs = [r for r in all_recs if r.date >= since]

        jobs = [(r, out_root / r.date / f"{r.timestamp}.mp3") for r in all_recs]
        todo, existing = _plan_downloads(jobs, listed, since)
        if not todo:
            return existing, 0
        console.print()

        # Each download gets its own connection, so a dropped link is
        # retried instead of ending the run (see download_checked).
        downloaded_paths, failed = list(existing), 0
        for i, (rec, out_path) in enumerate(todo, 1):
            console.print(
                f"  [{i}/{len(todo)}] {rec.date}/{rec.timestamp} "
                f"(~{rec.estimated_bytes // 1024:,} KB)..."
            )

            def progress(current, total):
                if total > 0:
                    pct = 100 * current // total
                    console.print(f"\r    [dim]{pct}%[/dim]", end="")

            data, verified = await download_checked(address, session_key, rec,
                                                    progress_callback=progress)
            console.print()

            if not data:
                failed += 1
                console.print("    [red]Download failed; nothing saved. Re-run to try again.[/red]")
                continue
            # save_recording writes through a .part file, so a full disk never
            # leaves a truncated .mp3 that the next run would take as downloaded.
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                save_recording(out_path, data, verified, rec)
            except OSError as e:
                failed += 1
                console.print(f"    [red]Could not write {out_path}: {e}[/red]")
                continue
            downloaded_paths.append(out_path)
            console.print(f"    [green]Saved {len(data):,} bytes[/green]")

        console.print(f"\n[bold]{len(todo) - failed} downloaded, {failed} failed[/bold] "
                      f"(into {out_root})")
        return downloaded_paths, failed

    downloaded_paths, failed = asyncio.run(_run())

    if do_process and downloaded_paths:
        console.print("\n[bold cyan]Processing recordings...[/bold cyan]\n")
        for path in downloaded_paths:
            console.print(f"\n[bold]Processing {path.name}...[/bold]")
            ctx.invoke(process, input_path=str(path),
                       whisper_model=get(config, "defaults", "whisper_model", default="base.en"),
                       style=get(config, "defaults", "summary_style", default="meeting"),
                       anthropic_key=None, hf_token=None, skip_summary=False,
                       output=str(path.parent))

    if delete_after:
        _delete_downloaded(address, session_key, out_root, since, assume_yes=True)
    if failed:
        raise SystemExit(1)


# ── Deleting ────────────────────────────────────


async def _delete_recordings(address: str, session_key: str,
                             recs: list[Recording]) -> int:
    """Delete `recs` from the device over one connection. Returns how many went."""
    deleted = 0
    async with PocketCommander(address) as cmd:
        if not await cmd.authenticate(session_key):
            raise click.ClickException("Authentication failed.")
        listings: dict[str, list[Recording]] = {}  # per date, as last listed
        for rec in recs:
            name = f"{rec.date}/{rec.timestamp}"
            # delete() checks the listing afterwards, which can't tell a
            # deletion from a recording that was never there. The listing a
            # deletion was confirmed with serves as the next one's "before".
            listed = listings.pop(rec.date, None)
            if listed is None:
                listed = await cmd.list_files_complete(rec.date)
            if listed is None:
                console.print(f"  [red]Could not list {rec.date}; kept {name}.[/red]")
                continue
            if all(r.timestamp != rec.timestamp for r in listed):
                console.print(f"  [red]{name} is not on the device.[/red]")
                continue
            gone, remaining = await cmd.delete_and_list(rec)
            if remaining is not None:
                listings[rec.date] = remaining
            if gone:
                deleted += 1
                console.print(f"  [green]Deleted {name}[/green]")
            elif gone is None:
                console.print(f"  [yellow]Could not confirm that {name} was deleted; "
                              "check with `pocket-libre list`.[/yellow]")
            else:
                console.print(f"  [red]{name} is still on the device.[/red]")
    return deleted


def _delete_downloaded(address: str, session_key: str, out_root: Path,
                       since: str | None, assume_yes: bool) -> None:
    """Delete from the device every recording with a verified copy under out_root."""
    from pocket_libre.commands import has_complete_copy

    async def _list() -> list[Recording]:
        async with PocketCommander(address) as cmd:
            if not await cmd.authenticate(session_key):
                raise click.ClickException("Authentication failed.")
            return await cmd.list_all_recordings()

    def unreachable(error: Exception) -> None:
        console.print(f"[red]Could not reach the device to delete recordings: {error}[/red]")
        console.print("[yellow]Nothing more was deleted. Run `pocket-libre delete "
                      "--downloaded` once it connects again.[/yellow]")
        raise SystemExit(1)

    console.print("\n[bold]Removing downloaded recordings from the device...[/bold]")
    # Right after a transfer the device can take a moment to answer again.
    for attempt in (1, 2):
        try:
            recs = asyncio.run(_list())
            break
        except click.ClickException:
            raise
        except Exception as e:  # not found, BleakError, a dropped connection
            if attempt == 2:
                unreachable(e)
            time.sleep(5)
    if since:
        recs = [r for r in recs if r.date >= since]
    done = [r for r in recs if has_complete_copy(out_root / r.date / r.filename, r)]
    for r in recs:
        if r not in done:
            console.print(f"  [yellow]Keeping {r} (no copy in {out_root} that its download "
                          "verified for this size and duration)[/yellow]")
    if not done:
        console.print("[dim]Nothing to delete.[/dim]")
        return
    if not assume_yes:
        for r in done:
            console.print(f"  {r}")
        click.confirm(f"Delete these {len(done)} recording(s) from the device? "
                      "This can't be undone", abort=True)
    try:
        deleted = asyncio.run(_delete_recordings(address, session_key, done))
    except click.ClickException:
        raise
    except Exception as e:
        unreachable(e)
    console.print(f"[bold]{deleted} of {len(done)} recording(s) deleted from the device.[/bold]")
    if deleted < len(done):
        raise SystemExit(1)


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--date", default=None, help="Recording date (YYYY-MM-DD), with --timestamp.")
@click.option("--timestamp", default=None, help="Recording timestamp, with --date.")
@click.option("--downloaded", is_flag=True,
              help="Delete every recording that has a verified copy in the output directory "
                   "(one whose size matches what its download recorded).")
@click.option("--output-dir", default=None,
              help="With --downloaded: where the copies are (default: the configured one).")
@click.option("--since", default=None,
              help="With --downloaded: only recordings from this date (YYYY-MM-DD) on.")
@click.option("--yes", "assume_yes", is_flag=True, help="Don't ask for confirmation.")
@click.pass_context
def delete(ctx, address: str | None, session_key: str | None, date: str | None,
           timestamp: str | None, downloaded: bool, output_dir: str | None,
           since: str | None, assume_yes: bool):
    """Delete recordings from the device. This can't be undone.

    \b
    One recording with --date and --timestamp, or with --downloaded every
    recording whose <output dir>/<date>/<timestamp>.mp3 is complete;
    recordings without a local copy are kept.
    """
    from pocket_libre.commands import is_safe_id

    if downloaded == (date is not None or timestamp is not None):
        raise click.UsageError("Pass either --date and --timestamp, or --downloaded.")
    if not downloaded and (date is None or timestamp is None):
        raise click.UsageError("Pass --date and --timestamp together.")
    if not downloaded and not (is_safe_id(date) and is_safe_id(timestamp)):
        raise click.UsageError("--date and --timestamp must be plain identifiers.")
    if not downloaded and (output_dir or since):
        raise click.UsageError("--output-dir and --since go with --downloaded.")

    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)

    if downloaded:
        out_root = Path(get_output_dir(config, output_dir))
        _delete_downloaded(address, session_key, out_root, since, assume_yes)
        return

    rec = Recording(date=date, timestamp=timestamp, duration_s=0)
    if not assume_yes:
        click.confirm(f"Delete {date}/{timestamp} from the device? This can't be undone",
                      abort=True)
    if not asyncio.run(_delete_recordings(address, session_key, [rec])):
        raise SystemExit(1)


# ── Sync & Process ──────────────────────────────


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--output-dir", default=None, help="Where to save recordings.")
@click.option("--since", default=None,
              help="Only recordings from this date (YYYY-MM-DD) on: the date of the "
                   "recording's folder on the device, in UTC.")
@click.option("--whisper-model", default=None,
              type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
              help="Whisper model size.")
@click.option("--style", default=None,
              type=click.Choice(["meeting", "notes", "call", "raw"]),
              help="Summary style.")
@click.option("--anthropic-key", default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY).")
@click.option("--hf-token", default=None, help="HuggingFace token for speaker diarization.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--skip-process", is_flag=True, help="Only download, skip transcription and summary.")
@click.option("--prompt", default=None, help="Custom summary prompt (use {transcript} placeholder).")
@click.option("--delete-after", is_flag=True,
              help="Afterwards, delete every recording with a verified local copy from the device.")
@click.pass_context
def sync(ctx, address: str | None, output_dir: str | None, since: str | None,
         whisper_model: str | None, style: str | None,
         anthropic_key: str | None, hf_token: str | None,
         session_key: str | None, skip_process: bool, prompt: str | None,
         delete_after: bool):
    """Sync all new recordings: download, transcribe, summarize.

    \b
    Downloads all new recordings from the device over BLE, then
    transcribes with Whisper (locally) and summarizes with Claude Haiku
    (~$0.001 per recording). Skips recordings already on disk.
    """
    from pocket_libre.commands import download_checked, needs_download, save_recording

    config = ctx.obj["config"]
    address = _require_address(address, config)
    out_root = Path(get_output_dir(config, output_dir))
    whisper_model = whisper_model or get(config, "defaults", "whisper_model", default="base.en")
    style = style or get(config, "defaults", "summary_style", default="meeting")
    anthropic_key = resolve_anthropic_key(config, anthropic_key)
    hf_token = resolve_hf_token(config, hf_token)
    session_key = _require_session_key(session_key, config)

    if not anthropic_key and not skip_process:
        console.print(Panel(
            "[bold yellow]No Anthropic API key found.[/bold yellow]\n\n"
            "Transcription works without it (runs locally).\n"
            "To enable AI summaries (~$0.001/recording):\n"
            "  pocket-libre setup\n"
            "  OR set: export ANTHROPIC_API_KEY=sk-ant-...\n\n"
            "Use --skip-process to just download.",
            title="API Key Missing",
            border_style="yellow",
        ))

    async def _run():
        # List recordings on device
        console.print("[bold]Connecting to device...[/bold]")
        async with PocketCommander(address) as cmd:
            if not await cmd.authenticate(session_key):
                console.print("[red]Auth failed.[/red]")
                return
            all_recs = await cmd.list_all_recordings()

        if since:
            all_recs = [r for r in all_recs if r.date >= since]

        if not all_recs:
            console.print("[yellow]No recordings found.[/yellow]")
            return

        # Filter to new recordings
        new_recs = []
        for rec in all_recs:
            mp3_path = out_root / rec.date / f"{rec.timestamp}.mp3"
            if needs_download(mp3_path):
                new_recs.append(rec)

        if not new_recs:
            console.print(f"[green]All {len(all_recs)} recordings already synced.[/green]")
            return

        console.print(f"[bold]{len(new_recs)} new recording(s) to sync[/bold] ({len(all_recs)} total on device)\n")

        # Download each
        for i, rec in enumerate(new_recs, 1):
            rec_dir = out_root / rec.date
            rec_dir.mkdir(parents=True, exist_ok=True)
            audio_path = rec_dir / f"{rec.timestamp}.mp3"

            console.print(f"[bold][{i}/{len(new_recs)}] Downloading {rec}...[/bold]")

            def progress(current, total):
                if total > 0:
                    pct = 100 * current // total
                    console.print(f"\r  [dim]{pct}%[/dim]", end="")

            data, verified = await download_checked(address, session_key, rec,
                                                    progress_callback=progress)
            console.print()

            if not data:
                console.print("  [red]Failed to download.[/red]")
                continue

            save_recording(audio_path, data, verified, rec)
            console.print(f"  [green]Saved {len(data):,} bytes[/green]")

            if skip_process:
                continue

            # Transcribe
            console.print(f"  [dim]Transcribing ({whisper_model})...[/dim]")
            try:
                import whisper
                model = whisper.load_model(whisper_model)
                result = model.transcribe(str(audio_path), verbose=False)
                segments = result.get("segments", [])
            except Exception as e:
                console.print(f"  [red]Transcription failed: {e}[/red]")
                continue

            # Diarize
            try:
                from pocket_libre.diarize import diarize_auto, merge_transcript_with_speakers
                speaker_segments = diarize_auto(
                    segments, audio_path=str(audio_path),
                    hf_token=hf_token, anthropic_key=anthropic_key,
                )
                labeled = merge_transcript_with_speakers(segments, speaker_segments)
            except Exception:
                labeled = [{"start": s["start"], "end": s["end"], "speaker": "Speaker", "text": s["text"]} for s in segments]

            from pocket_libre.summarize import format_transcript_for_summary
            transcript_text = format_transcript_for_summary(labeled)
            transcript_path = rec_dir / f"{rec.timestamp}_transcript.txt"
            transcript_path.write_text(transcript_text, encoding="utf-8")
            console.print(f"  [green]Transcript saved ({len(segments)} segments)[/green]")

            # Summarize
            if anthropic_key:
                console.print("  [dim]Summarizing...[/dim]")
                try:
                    from pocket_libre.summarize import summarize_transcript
                    summary = summarize_transcript(
                        transcript_text=transcript_text,
                        api_key=anthropic_key,
                        style=style,
                        custom_prompt=prompt,
                    )
                    if summary:
                        from datetime import datetime
                        summary_path = rec_dir / f"{rec.timestamp}_summary.md"
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                        full_doc = f"# {rec.timestamp} ({ts})\n\n{summary}\n\n---\n\n## Full Transcript\n\n{transcript_text}"
                        summary_path.write_text(full_doc, encoding="utf-8")
                        console.print("  [green]Summary saved[/green]")
                except Exception as e:
                    console.print(f"  [yellow]Summary failed: {e}[/yellow]")

        console.print(f"\n[bold green]Sync complete! {len(new_recs)} recording(s) processed.[/bold green]")
        console.print(f"[dim]Output: {out_root}[/dim]")

    asyncio.run(_run())
    if delete_after:
        _delete_downloaded(address, session_key, out_root, since, assume_yes=True)


@cli.command()
@click.option("--input", "input_path", required=True, help="Path to MP3 or WAV file.")
@click.option("--whisper-model", default=None,
              type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
              help="Whisper model size.")
@click.option("--style", default=None,
              type=click.Choice(["meeting", "notes", "call", "raw"]),
              help="Summary style.")
@click.option("--anthropic-key", default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY).")
@click.option("--hf-token", default=None, help="HuggingFace token for speaker diarization.")
@click.option("--skip-summary", is_flag=True, help="Skip AI summary.")
@click.option("--output", default=None, help="Output directory (default: same as input file).")
@click.pass_context
def process(ctx, input_path: str, whisper_model: str | None, style: str | None,
            anthropic_key: str | None, hf_token: str | None,
            skip_summary: bool, output: str | None):
    """Process an existing audio file: transcribe, diarize, summarize."""
    from datetime import datetime

    config = ctx.obj["config"]
    whisper_model = whisper_model or get(config, "defaults", "whisper_model", default="base.en")
    style = style or get(config, "defaults", "summary_style", default="meeting")
    anthropic_key = resolve_anthropic_key(config, anthropic_key)
    hf_token = resolve_hf_token(config, hf_token)

    input_file = Path(input_path)
    if not input_file.exists():
        console.print(f"[red]File not found: {input_path}[/red]")
        return

    if output:
        out_dir = Path(os.path.expanduser(output))
    else:
        out_dir = input_file.parent

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_file.stem

    console.print(Panel(
        f"[bold]Processing: {input_path}[/bold]\n"
        f"Whisper: {whisper_model} | Style: {style}",
        border_style="cyan",
    ))

    # Transcribe
    console.print("\n[bold cyan]Step 1/3: Transcribing...[/bold cyan]\n")
    try:
        import whisper
    except ImportError:
        console.print("[red]Whisper not installed.[/red]")
        return

    model = whisper.load_model(whisper_model)
    result = model.transcribe(str(input_file), verbose=False)
    segments = result.get("segments", [])
    console.print(f"[green]Transcribed: {len(segments)} segments[/green]")

    # Diarize
    console.print("\n[bold cyan]Step 2/3: Identifying speakers...[/bold cyan]\n")
    from pocket_libre.diarize import diarize_auto, merge_transcript_with_speakers

    speaker_segments = diarize_auto(
        segments, audio_path=str(input_file),
        hf_token=hf_token, anthropic_key=anthropic_key,
    )
    labeled = merge_transcript_with_speakers(segments, speaker_segments)

    from pocket_libre.summarize import format_transcript_for_summary
    transcript_text = format_transcript_for_summary(labeled)

    transcript_path = out_dir / f"{stem}_transcript.txt"
    transcript_path.write_text(transcript_text, encoding="utf-8")
    console.print(f"[green]Transcript saved: {transcript_path}[/green]")

    # Summarize
    if skip_summary:
        console.print("\n[dim]Skipping summary.[/dim]")
    else:
        console.print("\n[bold cyan]Step 3/3: Summarizing...[/bold cyan]\n")
        if not anthropic_key:
            console.print(Panel(
                "[bold yellow]No Anthropic API key found.[/bold yellow]\n\n"
                "To enable AI summaries:\n"
                "  1. Get a key at https://console.anthropic.com/settings/keys\n"
                "  2. Run: [bold]pocket-libre setup[/bold]\n\n"
                "Transcript was still saved above.",
                title="API Key Missing",
                border_style="yellow",
            ))
        else:
            from pocket_libre.summarize import estimate_cost, summarize_transcript
            est = estimate_cost(transcript_text)
            console.print(f"[dim]Estimated cost: {est}[/dim]")

            summary = summarize_transcript(
                transcript_text=transcript_text,
                api_key=anthropic_key,
                style=style,
            )

            if summary:
                summary_path = out_dir / f"{stem}_summary.md"
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                full_doc = (
                    f"# {stem} ({ts})\n\n"
                    + summary
                    + "\n\n---\n\n## Full Transcript\n\n"
                    + transcript_text
                )
                summary_path.write_text(full_doc, encoding="utf-8")
                console.print(f"[green]Summary saved: {summary_path}[/green]")

    console.print("\n[bold green]Done![/bold green]")


# ── Background Sync ─────────────────────────────


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--interval", default=60.0, help="Seconds between presence checks.")
@click.option("--output-dir", default=None, help="Where to save recordings.")
@click.option("--process", "do_process", is_flag=True,
              help="Also transcribe and summarize each new recording.")
@click.pass_context
def watch(ctx, address: str | None, session_key: str | None, interval: float,
          output_dir: str | None, do_process: bool):
    """Watch for the device and sync new recordings automatically.

    \b
    Runs until interrupted. Scans for your Pocket every --interval seconds;
    when it appears, downloads anything not already on disk. Backs off to
    5-minute checks while the device is away.
    """
    from pocket_libre.watch import sync_new_recordings, watch_loop

    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)
    out_root = Path(get_output_dir(config, output_dir))
    whisper_model = get(config, "defaults", "whisper_model", default="base.en")
    style = get(config, "defaults", "summary_style", default="meeting")
    anthropic_key = resolve_anthropic_key(config)
    hf_token = resolve_hf_token(config)

    console.print(Panel(
        f"[bold]Watching for {address}[/bold]\n\n"
        f"Interval:   {interval:.0f}s\n"
        f"Output:     {out_root}\n"
        f"Processing: {'on' if do_process else 'off'}\n\n"
        "Press Ctrl+C to stop.",
        border_style="cyan",
    ))

    async def _sync_once() -> int:
        return await sync_new_recordings(
            address=address, session_key=session_key, out_root=out_root,
            process=do_process, whisper_model=whisper_model, summary_style=style,
            anthropic_key=anthropic_key, hf_token=hf_token,
        )

    try:
        stats = asyncio.run(watch_loop(address, _sync_once, poll_interval=interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
        return

    console.print(
        f"[dim]{stats.scans} scans, {stats.recordings_synced} recordings synced.[/dim]"
    )


# ── WiFi Transfer ───────────────────────────────


@cli.command("wifi-discover")
@click.option("--host", default=None, help="Host to sweep (default: the device AP).")
@click.option("--start-port", default=1, type=int, help="First port to sweep.")
@click.option("--end-port", default=65535, type=int, help="Last port to sweep.")
@click.option("--force", is_flag=True,
              help="Sweep even when this machine is not on the device subnet.")
def wifi_discover(host: str | None, start_port: int, end_port: int, force: bool):
    """Sweep the device's WiFi AP for listening sockets.

    \b
    A diagnostic for firmware other than 1.7 and 1.8. On those the transfer
    socket is TCP 8475 (see PROTOCOL.md), and 'wifi-transfer' uses it. Join the
    device's WiFi network first; this does not raise the AP itself.
    """
    from pocket_libre.protocol import TRANSFER_PORT
    from pocket_libre.wifi import DEFAULT_HOST, diagnose

    target = host or DEFAULT_HOST
    if start_port > end_port:
        raise click.UsageError(
            f"--start-port ({start_port}) is above --end-port ({end_port})."
        )
    if end_port < 1 or start_port > 65535:
        raise click.UsageError("Port range must fall within 1-65535.")
    ports = list(range(max(start_port, 1), min(end_port, 65535) + 1))

    console.print(f"[bold]Sweeping {target} ({len(ports):,} ports)...[/bold]\n")
    report = diagnose(host=target, ports=ports, require_ap_subnet=not force)

    if report.scan is None:
        console.print(
            "\n[yellow]Did not sweep.[/yellow] Join the device's WiFi network first."
        )
        return

    if report.scan.open_ports:
        listed = ", ".join(str(port) for port in report.scan.open_ports)
        known = (f"\n\n{TRANSFER_PORT} is the transfer socket known from firmware 1.7 and 1.8."
                 if TRANSFER_PORT in report.scan.open_ports else "")
        console.print(Panel(
            f"[bold]{len(report.scan.open_ports)} open port(s)[/bold] on {target}\n\n"
            f"{listed}{known}\n\n"
            "On firmware other than 1.7 and 1.8, please report what you found:\n"
            "  https://github.com/shahcolate/pocket-libre/issues",
            border_style="green",
        ))
    elif not report.scan.reliable:
        # An incomplete sweep must never be reported as a clean negative:
        # the whole value of this command is that an empty result can be
        # trusted.
        console.print(Panel(
            f"[bold red]Sweep incomplete — do not report this as a "
            f"result.[/bold red]\n\n"
            f"{report.scan.error}\n\n"
            "The scan ran out of file descriptors, so ports may be reported\n"
            "closed when they are not. Raise the limit and try again:\n"
            "  ulimit -n 4096",
            border_style="red",
        ))
    else:
        console.print(Panel(
            f"[bold]Nothing listening[/bold] on {target}\n\n"
            f"Swept {report.scan.scanned:,} ports from {report.local_address}.\n\n"
            f"On firmware 1.7 and 1.8, {TRANSFER_PORT} listens only while the AP is\n"
            "up and has transfer connections left (one per AP session on 1.7, two\n"
            "on 1.8). On other firmware, please report this result:\n"
            "  https://github.com/shahcolate/pocket-libre/issues",
            border_style="yellow",
        ))


@cli.command("wifi-transfer")
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--date", default=None, help="Recording date (YYYY-MM-DD), with --timestamp.")
@click.option("--timestamp", default=None, help="Recording timestamp, with --date.")
@click.option("--since", default=None,
              help="Without --date/--timestamp: only recordings from this date (YYYY-MM-DD) "
                   "on: the date of the recording's folder on the device, in UTC.")
@click.option("--output", default=None,
              help="Output file for a single recording (default: <timestamp>.mp3).")
@click.option("--output-dir", default=None,
              help="Output directory for all recordings (default: the configured one).")
@click.option("--overwrite", is_flag=True, help="Download files that already exist again.")
@click.option("--wifi", "wifi_backend", default="auto",
              type=click.Choice(["auto", "networkmanager", "netsh", "manual"]),
              help="How to join the device's network: NetworkManager (Linux), netsh "
                   "(Windows), or manual (you join it yourself). Default: by OS.")
@click.option("--iface", default=None, help="WiFi interface to use (default: the first one).")
@click.option("--force", is_flag=True, help="Run on firmware other than 1.7 or 1.8.")
@click.option("--delete-after", is_flag=True,
              help="Afterwards, delete every recording with a verified local copy from the device.")
@click.pass_context
def wifi_transfer(ctx, address: str | None, session_key: str | None, date: str | None,
                  timestamp: str | None, since: str | None, output: str | None,
                  output_dir: str | None, overwrite: bool, wifi_backend: str,
                  iface: str | None, force: bool, delete_after: bool):
    """Download recordings over WiFi instead of BLE (firmware 1.7 and 1.8).

    \b
    Roughly 1 MB/s instead of BLE's few KB/s. Raises the device's WiFi
    access point over BLE, moves this machine's WiFi onto it, downloads, and
    puts this machine's WiFi back — also after errors and Ctrl-C. While it
    runs, this machine has no internet over WiFi.

    \b
    One recording with --date and --timestamp, otherwise every recording
    (or those from --since on) into <output dir>/<date>/<timestamp>.mp3,
    skipping files that already exist. The device serves two files per
    access-point session on firmware 1.8 and one on 1.7, so the AP is
    restarted in between (about 15 s each time).

    \b
    Joining needs NetworkManager on Linux or netsh on Windows; on macOS (or
    with --wifi manual) you join the network yourself when asked.
    """
    from bleak.exc import BleakError

    from pocket_libre.commands import is_safe_id, mark_unverified, record_download
    from pocket_libre.hostwifi import HostWifiError, backend
    from pocket_libre.protocol import FILES_PER_AP_SESSION
    from pocket_libre.wifi import (
        DEFAULT_HOST,
        AccessPointError,
        WifiSession,
        WifiTransferError,
        files_per_ap_session,
        firmware_line,
    )

    if (date is None) != (timestamp is None):
        raise click.UsageError("Pass --date and --timestamp together, or neither.")
    if output and date is None:
        raise click.UsageError("--output is for a single recording; use --output-dir.")
    if delete_after and date is not None:
        raise click.UsageError("--delete-after is for a batch; use `delete` for one recording.")
    if date is not None and not (is_safe_id(date) and is_safe_id(timestamp)):
        raise click.UsageError("--date and --timestamp must be plain identifiers.")

    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)
    out_root = Path(get_output_dir(config, output_dir))

    def log(text: str) -> None:
        console.print(f"[dim]{text}[/dim]")

    link_lost = False

    def lost_link(error: Exception | None, not_attempted: int) -> None:
        nonlocal link_lost
        link_lost = True
        detail = f": {error}" if error else ""
        console.print(f"\n[red]Lost the BLE link{detail}[/red]")
        if not_attempted:
            console.print(f"[yellow]{not_attempted} recording(s) not attempted.[/yellow]")
        console.print("[yellow]The device may need a power-cycle before it "
                      "connects again.[/yellow]")

    async def _run() -> tuple[int, int]:
        async with PocketCommander(address) as cmd:
            if not await cmd.authenticate(session_key):
                raise click.ClickException("Authentication failed.")
            firmware = await cmd.get_firmware()
            if firmware_line(firmware) not in FILES_PER_AP_SESSION:
                if not force:
                    raise click.ClickException(
                        f"This device runs firmware {firmware}. WiFi transfer works on "
                        "firmware 1.7 and 1.8;\nother firmware may behave differently. "
                        "Pass --force to try anyway, or use 'download'."
                    )
                log(f"Firmware {firmware} is untested; restarting the access point "
                    "for every file.")
            per_session = files_per_ap_session(firmware)
            battery = await cmd.get_battery()
            if 0 <= battery < 10:
                raise click.ClickException(
                    f"Battery is at {battery}%. The device needs more than 10% for WiFi transfer."
                )

            if date is not None:
                rec = Recording(date=date, timestamp=timestamp, duration_s=0)
                path = Path(output) if output else Path(f"{timestamp}.mp3")
                if path.exists() and not overwrite:
                    console.print(f"{path} already exists (use --overwrite to download "
                                  "it again).")
                    return 0, 0
                todo = [(rec, path)]
            else:
                recs = await cmd.list_all_recordings()
                listed = len(recs)
                if since:
                    recs = [r for r in recs if r.date >= since]
                jobs = [(r, out_root / r.date / f"{r.timestamp}.mp3") for r in recs]
                todo, _ = _plan_downloads(jobs, listed, since, overwrite,
                                          hint=" (use --overwrite to download again)")
                if not todo:
                    return 0, 0
            console.print("Over WiFi: this machine's WiFi switches to the device's "
                          "network until done.")
            try:
                host_wifi = backend(wifi_backend, DEFAULT_HOST, iface,
                                    lambda kind, text: log(text))
            except HostWifiError as e:
                raise click.ClickException(str(e)) from e

            done = failed = 0
            try:
                async with WifiSession(cmd, host_wifi, log=log,
                                       files_per_session=per_session) as session:
                    for i, (rec, path) in enumerate(todo, 1):
                        console.print(f"  [{i}/{len(todo)}] {rec.date}/{rec.timestamp}...")

                        def progress(current: int, total: int) -> None:
                            pct = 100 * current // total if total else 100
                            console.print(f"\r    [dim]{current:,}/{total:,} bytes ({pct}%)[/dim]",
                                          end="")

                        # Marked until it is recorded, so a crash in between
                        # leaves a copy the next run downloads again.
                        path.parent.mkdir(parents=True, exist_ok=True)
                        mark_unverified(path)
                        try:
                            result = await session.download(rec, path, progress_callback=progress)
                        except (WifiTransferError, BleakError) as e:
                            console.print(f"\n    [red]Failed: {e}[/red]")
                            failed += 1
                            # A dropped link can also surface as a missing
                            # reply; either way, nothing more will work.
                            if isinstance(e, BleakError) or not cmd.connected:
                                lost_link(None, len(todo) - i)
                                break
                            # The restart was already tried three times; the
                            # rest would only repeat it.
                            if isinstance(e, AccessPointError):
                                console.print("[yellow]The device's access point did not come "
                                              "back; stopping.[/yellow]")
                                if len(todo) - i:
                                    console.print(f"[yellow]{len(todo) - i} recording(s) "
                                                  "not attempted.[/yellow]")
                                break
                            continue
                        record_download(result.path, result.size, rec.duration_s)
                        rate = result.size / result.seconds / 1024 if result.seconds else 0
                        console.print(
                            f"\n    [green]Saved {result.size:,} bytes to {result.path} "
                            f"({rate:,.0f} KB/s)[/green]"
                        )
                        if not result.marker_ok:
                            console.print("    [yellow]The end marker was missing; the file "
                                          "has the full size the device reported.[/yellow]")
                        done += 1
            except (WifiTransferError, HostWifiError) as e:
                raise click.ClickException(str(e)) from e
            except BleakError as e:
                # Raised outside a download: raising the AP or cleaning up.
                if not (done or failed):
                    raise click.ClickException(f"Lost the BLE link: {e}") from e
                lost_link(e, len(todo) - done - failed)
            return done, failed

    try:
        done, failed = asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow] If this machine is still on the "
                      "device's network, switch back to your usual one.")
        raise SystemExit(130) from None

    if done or failed:
        console.print(f"\n[bold]{done} downloaded, {failed} failed.[/bold]")
    if delete_after and link_lost:
        console.print("[yellow]Not deleting anything from the device: the BLE link was "
                      "lost. Run `pocket-libre delete --downloaded` once it connects "
                      "again.[/yellow]")
    elif delete_after:
        if done or failed:
            time.sleep(3)  # the AP was up: let the device leave AP mode first
        _delete_downloaded(address, session_key, out_root, since, assume_yes=True)
    if failed:
        raise SystemExit(1)


# ── Web Interface ───────────────────────────────


@cli.command()
@click.option("--port", default=8265, help="Port to serve on.")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically.")
def web(host: str, port: int, no_browser: bool):
    """Launch the Pocket Libre web interface.

    Opens a browser-based UI for managing recordings, transcripts,
    and summaries. No terminal required after launch.
    """
    import webbrowser

    import uvicorn

    # 0.0.0.0 is not a connectable address — point the browser at loopback.
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    console.print(Panel(
        f"[bold]Pocket Libre Web UI[/bold]\n\n"
        f"Starting at http://{browse_host}:{port}\n"
        f"Press Ctrl+C to stop.",
        border_style="cyan",
    ))

    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(Panel(
            "[bold yellow]This binds a non-loopback address.[/bold yellow]\n\n"
            "The web interface has no authentication. Anyone who can reach\n"
            f"{host}:{port} can read your transcripts and summaries, control\n"
            "your device, and spend your API credits.\n\n"
            "Only do this on a network you trust.",
            title="Warning",
            border_style="yellow",
        ))

    if not no_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{browse_host}:{port}")).start()

    uvicorn.run("pocket_libre.web.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    cli()
