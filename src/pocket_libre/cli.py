"""CLI entry point for Pocket Libre."""

import asyncio
import os
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from pocket_libre.scanner import scan_devices
from pocket_libre.explorer import explore_device
from pocket_libre.capture import capture_audio
from pocket_libre.transcribe import transcribe_audio
from pocket_libre.sniffer import sniff_all
from pocket_libre.probe import probe_characteristic
from pocket_libre.commands import PocketCommander, Recording
from pocket_libre.config import (
    load_config, save_config, CONFIG_FILE,
    resolve_address, resolve_session_key,
    resolve_anthropic_key, resolve_hf_token,
    get_output_dir, get,
)

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
    new_config = {
        "device": dict(config.get("device", {})),
        "api": dict(config.get("api", {})),
        "output": dict(config.get("output", {})),
        "defaults": dict(config.get("defaults", {})),
    }

    # Step 1: Device address
    console.print("\n[bold cyan]Step 1: Device Address[/bold cyan]")
    console.print("[dim]Scanning for Pocket devices...[/dim]")

    try:
        from bleak import BleakScanner
        devices = asyncio.run(BleakScanner.discover(timeout=5.0, return_adv=True))
        pocket_devices = []
        for d, adv in devices.values():
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

    # Step 2: API keys
    console.print("\n[bold cyan]Step 2: API Keys[/bold cyan]")

    console.print("\n  [bold]Anthropic API Key[/bold] (for AI summaries, mind maps, entity extraction)")
    console.print("  [dim]Get one at: https://console.anthropic.com/settings/keys[/dim]")
    console.print("  [dim]Cost: ~$0.003 per recording (summary + entities + mind map)[/dim]")
    console.print("  [dim]Transcription works without this key (runs locally).[/dim]")
    existing_key = new_config["api"].get("anthropic_key", "")
    masked = f"...{existing_key[-4:]}" if len(existing_key) > 4 else ""
    anthropic_key = click.prompt("  Anthropic API key", default=masked or "", show_default=bool(masked))
    if anthropic_key and not anthropic_key.startswith("..."):
        new_config["api"]["anthropic_key"] = anthropic_key

    console.print("\n  [bold]HuggingFace Token[/bold] (optional, for speaker identification)")
    console.print("  [dim]Get one at: https://huggingface.co/settings/tokens[/dim]")
    console.print("  [dim]Free tier works. Enables speaker diarization.[/dim]")
    existing_hf = new_config["api"].get("hf_token", "")
    masked_hf = f"...{existing_hf[-4:]}" if len(existing_hf) > 4 else ""
    hf_token = click.prompt("  HuggingFace token", default=masked_hf or "", show_default=bool(masked_hf))
    if hf_token and not hf_token.startswith("..."):
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
    if new_config["device"].get("address"):
        if click.confirm("\n  Test connection to device?", default=True):
            try:
                async def _test():
                    sk = resolve_session_key(new_config)
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
        console.print(f"\n[bold cyan][{section}][/bold cyan]")
        for key, val in values.items():
            display = val
            if key in ("anthropic_key", "hf_token") and val and len(str(val)) > 8:
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
@click.option("--bit-depth", default=16, type=click.Choice([8, 16], case_sensitive=False))
@click.option("--channels", default=1, help="Number of audio channels.")
def convert(input_path: str, output: str, sample_rate: int, bit_depth: int, channels: int):
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
    session_key = resolve_session_key(config, session_key)

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
                f"[bold]Storage:[/bold] {used:,} / {total:,} KB ({pct}% used)\n"
                f"[bold]State:[/bold] {state_names.get(state, f'Unknown ({state})')}\n"
                f"[bold]Time:[/bold] Synced to host",
                title="Pocket Status",
                border_style="cyan",
            ))

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
    session_key = resolve_session_key(config, session_key)

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
            table.add_column("Size (KB)", justify="right")
            table.add_column("~Duration", justify="right")

            total_files = 0
            for d in dates:
                recs = await cmd.list_files(d)
                for r in recs:
                    total_files += 1
                    secs = r.size_kb * 1024 // 4000 if r.size_kb > 0 else 0
                    mins, secs = divmod(secs, 60)
                    table.add_row(r.date, r.timestamp, str(r.size_kb), f"{mins}m{secs:02d}s")

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
    session_key = resolve_session_key(config, session_key)
    from pocket_libre.protocol import MP3_SYNC_WORD

    async def _run():
        async with PocketCommander(address) as cmd:
            console.print("[dim]Authenticating...[/dim]")
            if not await cmd.authenticate(session_key):
                console.print("[red]Auth failed.[/red]")
                return

            rec = Recording(date=date, timestamp=timestamp, size_kb=0)
            console.print(f"[bold]Downloading {rec.date}/{rec.timestamp}...[/bold]")

            def progress(current, total):
                if total > 0:
                    pct = 100 * current // total
                    console.print(f"\r[dim]{current:,}/{total:,} bytes ({pct}%)[/dim]", end="")

            data = await cmd.download_ble(rec, progress_callback=progress)
            console.print()

            if not data:
                console.print("[red]No data received.[/red]")
                return

            mp3_start = data.find(MP3_SYNC_WORD)
            if mp3_start > 0:
                data = data[mp3_start:]

            out_path = Path(output) if output else Path(f"{timestamp}.mp3")
            out_path.write_bytes(data)
            console.print(f"[bold green]Saved {len(data):,} bytes to {out_path}[/bold green]")

    asyncio.run(_run())


@cli.command("download-all")
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--since", default=None, help="Only download recordings after this date (YYYY-MM-DD).")
@click.option("--process", "do_process", is_flag=True, help="Transcribe and summarize after download.")
@click.option("--output-dir", default=None, help="Output directory.")
@click.pass_context
def download_all(ctx, address: str | None, session_key: str | None,
                 since: str | None, do_process: bool, output_dir: str | None):
    """Download all recordings from the device.

    \b
    Saves to ~/Pocket Libre/<date>/<timestamp>.mp3 (or configured directory).
    Use --process to also transcribe and summarize each recording.
    """
    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = resolve_session_key(config, session_key)
    out_root = Path(get_output_dir(config, output_dir))
    from pocket_libre.protocol import MP3_SYNC_WORD

    async def _run():
        async with PocketCommander(address) as cmd:
            console.print("[dim]Authenticating...[/dim]")
            if not await cmd.authenticate(session_key):
                console.print("[red]Auth failed.[/red]")
                return

            all_recs = await cmd.list_all_recordings()
            if since:
                all_recs = [r for r in all_recs if r.date >= since]

            if not all_recs:
                console.print("[yellow]No recordings found.[/yellow]")
                return

            console.print(f"[bold]{len(all_recs)} recording(s) to download[/bold]\n")

            downloaded_paths = []
            for i, rec in enumerate(all_recs, 1):
                rec_dir = out_root / rec.date
                rec_dir.mkdir(parents=True, exist_ok=True)
                out_path = rec_dir / f"{rec.timestamp}.mp3"

                if out_path.exists():
                    console.print(f"  [{i}/{len(all_recs)}] {rec.date}/{rec.timestamp} [dim](already exists, skipping)[/dim]")
                    downloaded_paths.append(out_path)
                    continue

                console.print(f"  [{i}/{len(all_recs)}] {rec.date}/{rec.timestamp} ({rec.size_kb} KB)...")

                def progress(current, total):
                    if total > 0:
                        pct = 100 * current // total
                        console.print(f"\r    [dim]{pct}%[/dim]", end="")

                data = await cmd.download_ble(rec, progress_callback=progress)
                console.print()

                if data:
                    mp3_start = data.find(MP3_SYNC_WORD)
                    if mp3_start > 0:
                        data = data[mp3_start:]
                    out_path.write_bytes(data)
                    downloaded_paths.append(out_path)
                    console.print(f"    [green]Saved {len(data):,} bytes[/green]")
                else:
                    console.print(f"    [red]No data received[/red]")

            console.print(f"\n[bold green]Downloaded {len(downloaded_paths)} recording(s) to {out_root}[/bold green]")

    asyncio.run(_run())

    if do_process and downloaded_paths:
        console.print("\n[bold cyan]Processing recordings...[/bold cyan]\n")
        for path in downloaded_paths:
            console.print(f"\n[bold]Processing {path.name}...[/bold]")
            ctx.invoke(process, input_path=str(path),
                       whisper_model=get(config, "defaults", "whisper_model", default="base.en"),
                       style=get(config, "defaults", "summary_style", default="meeting"),
                       anthropic_key=None, hf_token=None, skip_summary=False,
                       output=str(path.parent))


# ── Sync & Process ──────────────────────────────


@cli.command()
@click.option("--address", default=None, help="BLE address of your Pocket device.")
@click.option("--output-dir", default=None, help="Where to save recordings.")
@click.option("--since", default=None, help="Only sync recordings after this date (YYYY-MM-DD).")
@click.option("--whisper-model", default=None,
              type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
              help="Whisper model size.")
@click.option("--style", default=None,
              type=click.Choice(["meeting", "notes", "call", "raw"]),
              help="Summary style.")
@click.option("--anthropic-key", default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY).")
@click.option("--hf-token", default=None, help="HuggingFace token for speaker diarization.")
@click.option("--skip-process", is_flag=True, help="Only download, skip transcription and summary.")
@click.option("--prompt", default=None, help="Custom summary prompt (use {transcript} placeholder).")
@click.pass_context
def sync(ctx, address: str | None, output_dir: str | None, since: str | None,
         whisper_model: str | None, style: str | None,
         anthropic_key: str | None, hf_token: str | None,
         skip_process: bool, prompt: str | None):
    """Sync all new recordings: download, transcribe, summarize.

    \b
    Downloads all new recordings from the device over BLE, then
    transcribes with Whisper (locally) and summarizes with Claude Haiku
    (~$0.001 per recording). Skips recordings already on disk.
    """
    from pocket_libre.commands import download_with_retry

    config = ctx.obj["config"]
    address = _require_address(address, config)
    out_root = Path(get_output_dir(config, output_dir))
    whisper_model = whisper_model or get(config, "defaults", "whisper_model", default="base.en")
    style = style or get(config, "defaults", "summary_style", default="meeting")
    anthropic_key = resolve_anthropic_key(config, anthropic_key)
    hf_token = resolve_hf_token(config, hf_token)
    session_key = resolve_session_key(config)

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
            if not mp3_path.exists():
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

            data = await download_with_retry(address, session_key, rec, progress_callback=progress)
            console.print()

            if not data:
                console.print(f"  [red]Failed to download.[/red]")
                continue

            audio_path.write_bytes(data)
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
                console.print(f"  [dim]Summarizing...[/dim]")
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
                        console.print(f"  [green]Summary saved[/green]")
                except Exception as e:
                    console.print(f"  [yellow]Summary failed: {e}[/yellow]")

        console.print(f"\n[bold green]Sync complete! {len(new_recs)} recording(s) processed.[/bold green]")
        console.print(f"[dim]Output: {out_root}[/dim]")

    asyncio.run(_run())


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
            from pocket_libre.summarize import summarize_transcript, estimate_cost
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

    console.print(f"\n[bold green]Done![/bold green]")


# ── WiFi commands removed — BLE-only now ────────
# WiFi discovery/transfer code deleted. Use BLE sync instead.


WIFI_REMOVED_MSG = """WiFi transfer has been removed. Use BLE sync instead:

  pocket-libre sync          Download + process all new recordings
  pocket-libre download-all  Download only
  pocket-libre web           Use the web interface

BLE is slower but reliable. WiFi may return in a future release."""


@cli.command("wifi-discover", hidden=True)
@click.pass_context
def wifi_discover_removed(ctx):
    """(Removed) WiFi endpoint discovery."""
    console.print(WIFI_REMOVED_MSG)


@cli.command("wifi-transfer", hidden=True)
@click.pass_context
def wifi_transfer_removed(ctx):
    """(Removed) WiFi file transfer."""
    console.print(WIFI_REMOVED_MSG)


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
    import uvicorn
    import webbrowser

    console.print(Panel(
        f"[bold]Pocket Libre Web UI[/bold]\n\n"
        f"Starting at http://{host}:{port}\n"
        f"Press Ctrl+C to stop.",
        border_style="cyan",
    ))

    if not no_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    uvicorn.run("pocket_libre.web.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    cli()
