"""CLI entry point for Pocket Libre."""

import asyncio
import functools
import os
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from pocket_libre.backends import (
    BACKENDS,
    DEFAULT_BACKEND,
    DEFAULT_LOCAL_MODEL,
    LOCAL_BACKEND,
    TranscriptionError,
    find_faster_whisper,
    options_from_config,
    transcribe_and_label,
)
from pocket_libre.capture import capture_audio
from pocket_libre.commands import PocketCommander, Recording
from pocket_libre.config import (
    CONFIG_FILE,
    PROFILES_SECTION,
    VALID_PROFILE_NAME,
    ProfileError,
    active_profile,
    effective_config,
    get,
    get_output_dir,
    list_profiles,
    load_config,
    profile_key_for,
    profile_label,
    profile_warnings,
    resolve_address,
    resolve_anthropic_key,
    resolve_hf_token,
    resolve_profile_name,
    resolve_session_key,
    resolve_web_port,
    save_config,
)
from pocket_libre.explorer import explore_device
from pocket_libre.probe import probe_characteristic
from pocket_libre.scanner import scan_devices
from pocket_libre.sniffer import sniff_all
from pocket_libre.transcribe import transcribe_audio

console = Console()


def _due_marker() -> str:
    """The calendar emoji, unless this console cannot encode it.

    A Windows console on a legacy code page raises while printing it, and
    crashing halfway through a task list is a silly way to lose one. Files are
    always written UTF-8, so only the console needs the plainer marker.
    """
    marker = "📅"
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    try:
        marker.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "due"
    return marker


def _require_resolved_profile(ctx) -> None:
    """Raise the deferred profile error for a command that does need a device."""
    if ctx.obj.get("profile_error"):
        raise click.UsageError(ctx.obj["profile_error"])


def _for_profile(config: dict) -> str:
    """' for profile <name>', or nothing when no profile is active."""
    name = active_profile(config)
    return f" for profile '{name}'" if name else ""


def _setup_hint(config: dict) -> str:
    name = active_profile(config)
    return f"pocket-libre setup --profile {name}" if name else "pocket-libre setup"


def _require_address(address: str | None, config: dict) -> str:
    """Resolve device address or exit with helpful error."""
    addr = resolve_address(config, address)
    if not addr:
        raise click.UsageError(
            f"No device address{_for_profile(config)}. "
            f"Run '{_setup_hint(config)}' or pass --address.\n"
            "Find your device with: pocket-libre scan --filter pkt"
        )
    return addr


def _require_session_key(session_key: str | None, config: dict) -> str:
    """Resolve session key or exit with helpful error."""
    sk = resolve_session_key(config, session_key)
    if not sk:
        raise click.UsageError(
            f"No session key{_for_profile(config)}. "
            f"Run '{_setup_hint(config)}' or pass --key.\n"
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


# Commands that touch no device and no library, so an unresolved profile must
# not stop them: these are what you run to inspect or repair the config.
PROFILE_OPTIONAL_COMMANDS = frozenset({
    "search", "reindex", "export", "tasks",
    # "speakers" works on the library, not the device, but it still needs to
    # know which library, so its subcommands resolve the profile themselves.
    "speakers",
    # "watch" and "web" belong here only because of their --all form, which
    # needs no single profile; both re-raise the error when --all is absent.
    "watch", "web",
    "profiles", "config", "setup", "scan", "transcribe", "convert", "wifi-discover",
})


@click.group()
@click.version_option()
@click.option("--profile", "-p", "profile_name", default=None, metavar="NAME",
              help="Which recorder to act on. Defaults to default_profile, "
                   "or the only profile when there is just one.")
@click.pass_context
def cli(ctx, profile_name: str | None):
    """Pocket Libre: Liberate your Pocket AI recorder from the cloud."""
    ctx.ensure_object(dict)
    raw = load_config()
    active = None
    ctx.obj["profile_error"] = None
    try:
        active = resolve_profile_name(raw, profile_name)
    except ProfileError as e:
        # Deferred, not swallowed: a command that needs a device still fails,
        # but 'profiles' and 'config' have to stay reachable to fix this.
        if ctx.invoked_subcommand not in PROFILE_OPTIONAL_COMMANDS:
            raise click.UsageError(str(e)) from e
        ctx.obj["profile_error"] = str(e)

    # `config` is the profile folded down into the global sections, so no
    # command below can reach another profile's device or library. `raw_config`
    # is the file as written, and is the only thing ever saved back.
    ctx.obj["raw_config"] = raw
    ctx.obj["profile"] = active
    ctx.obj["config"] = effective_config(raw, active)


# ── Setup & Config ──────────────────────────────


class _SetupTarget:
    """Where the wizard reads and writes: the global sections, or one profile.

    A profile table is flat, so `(section, key)` is translated to the name that
    setting takes inside a profile. Keeps the wizard's prompts identical either
    way instead of forking it.
    """

    def __init__(self, config: dict, profile: str | None = None):
        self.config = config
        self.profile = profile
        self.table: dict = {}
        if profile:
            profiles = config.setdefault(PROFILES_SECTION, {})
            self.table = profiles.setdefault(profile, {})

    def get(self, section: str, key: str, default=""):
        if self.profile:
            return self.table.get(profile_key_for(section, key), default)
        return self.config.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value):
        if self.profile:
            self.table[profile_key_for(section, key)] = value
        else:
            self.config.setdefault(section, {})[key] = value


@cli.command()
@click.option("--profile", "-p", "profile_name", default=None, metavar="NAME",
              help="Configure this profile, creating it if it does not exist. "
                   "Omit to configure the single-device defaults.")
@click.pass_context
def setup(ctx, profile_name: str | None):
    """Interactive setup wizard. Configures device, API keys, and preferences."""
    target_profile = (profile_name or ctx.obj["profile"] or "").strip().lower()
    if not target_profile and ctx.obj.get("profile_error"):
        # Falling through here would edit the global section while the user
        # believes they are configuring one of their recorders.
        raise click.UsageError(
            f"{ctx.obj['profile_error']}\n"
            "Say which one to configure: pocket-libre setup --profile <name>"
        )
    if target_profile and not VALID_PROFILE_NAME.match(target_profile):
        raise click.UsageError(
            f"{target_profile!r} is not a usable profile name. Use lowercase "
            "letters, digits, '-' or '_', starting with a letter or digit."
        )

    scope = (f"profile [bold]{target_profile}[/bold]" if target_profile
             else "the default (single-device) settings")
    console.print(Panel(
        "[bold]Pocket Libre Setup[/bold]\n\n"
        f"Configuring {scope}: device address, API keys, and preferences.\n"
        "Settings are saved to ~/.pocket-libre/config.toml\n"
        "Press Enter to accept defaults. Leave blank to skip.",
        border_style="cyan",
    ))

    # The file as written, never the profile-folded view: saving that back would
    # flatten one profile over the global settings and drop the others.
    config = ctx.obj["raw_config"]
    # Carry over every existing section so re-running setup never wipes
    # settings the wizard doesn't manage (e.g. [analysis]).
    new_config = {section: (dict(values) if isinstance(values, dict) else values)
                  for section, values in config.items()}
    if PROFILES_SECTION in new_config:
        new_config[PROFILES_SECTION] = {
            name: dict(table) for name, table in new_config[PROFILES_SECTION].items()
            if isinstance(table, dict)
        }
    for section in ("device", "api", "output", "defaults"):
        new_config.setdefault(section, {})

    target = _SetupTarget(new_config, target_profile or None)

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

        taken = {
            str(table.get("address", "")).strip().upper()
            for name, table in list_profiles(new_config).items()
            if name != target_profile
        }
        unclaimed = [d for d in pocket_devices if d.address.strip().upper() not in taken]
        if taken and len(pocket_devices) != len(unclaimed):
            console.print("  [dim]Skipping devices already assigned to another profile.[/dim]")

        if unclaimed:
            default_addr = unclaimed[0].address
            console.print(f"\n  [dim]Auto-detected: {default_addr}[/dim]")
        else:
            default_addr = target.get("device", "address", "")
            if not pocket_devices:
                console.print("  [yellow]No Pocket device found. Make sure it's awake (press button).[/yellow]")
    except Exception:
        default_addr = target.get("device", "address", "")
        console.print("  [yellow]BLE scan failed. You can enter the address manually.[/yellow]")

    addr = click.prompt("  Device address", default=default_addr or "", show_default=bool(default_addr))
    if addr:
        target.set("device", "address", addr)

    console.print("\n  [bold]Session Key[/bold] (16 characters, authenticates the BLE connection)")
    console.print("  [dim]Capture it from the vendor app's APP&SK& write — see PROTOCOL.md.[/dim]")
    if target_profile:
        console.print("  [dim]The key is issued per vendor account, not per device: leave this "
                      "blank to reuse the one already in [device].[/dim]")
    sk = _prompt_secret("  Session key", target.get("device", "session_key", ""))
    if sk:
        if len(sk) != 16:
            console.print("  [yellow]Warning: session keys are 16 characters — double-check the value.[/yellow]")
        target.set("device", "session_key", sk)

    # Step 2: API keys
    console.print("\n[bold cyan]Step 2: API Keys[/bold cyan]")

    console.print("\n  [bold]Anthropic API Key[/bold] (for AI summaries, mind maps, entity extraction)")
    console.print("  [dim]Get one at: https://console.anthropic.com/settings/keys[/dim]")
    console.print("  [dim]Cost: ~$0.003 per recording (summary + entities + mind map)[/dim]")
    console.print("  [dim]Transcription works without this key (runs locally).[/dim]")
    anthropic_key = _prompt_secret("  Anthropic API key", target.get("api", "anthropic_key", ""))
    if anthropic_key:
        target.set("api", "anthropic_key", anthropic_key)

    console.print("\n  [bold]HuggingFace Token[/bold] (optional, for speaker identification)")
    console.print("  [dim]Get one at: https://huggingface.co/settings/tokens[/dim]")
    console.print("  [dim]Free tier works. Enables speaker diarization.[/dim]")
    hf_token = _prompt_secret("  HuggingFace token", target.get("api", "hf_token", ""))
    if hf_token:
        target.set("api", "hf_token", hf_token)

    # Step 3: Output directory
    console.print("\n[bold cyan]Step 3: Output Directory[/bold cyan]")
    default_dir = get_output_dir(new_config, profile=target_profile or None)
    out_dir = click.prompt("  Save recordings to", default=default_dir)
    target.set("output", "directory", out_dir)

    # Step 4: Defaults
    console.print("\n[bold cyan]Step 4: Preferences[/bold cyan]")
    default_style = target.get("defaults", "summary_style", "") or "meeting"
    style = click.prompt(
        "  Summary style",
        type=click.Choice(["meeting", "notes", "call", "raw"]),
        default=default_style,
    )
    target.set("defaults", "summary_style", style)

    found_local = find_faster_whisper(target.get("defaults", "faster_whisper_path", ""))
    default_backend = (target.get("defaults", "transcribe_backend", "")
                       or (LOCAL_BACKEND if found_local else DEFAULT_BACKEND))
    console.print(
        "\n  [bold]Transcription backend[/bold]\n"
        "  [dim]openai-whisper runs in this process: English models by default, "
        "no speaker labels.\n"
        "  faster-whisper-xxl drives a local standalone build: every language, "
        "speakers included,\n"
        "  GPU, and no HuggingFace token"
        + (f" [green](found: {found_local})[/green]" if found_local
           else " [yellow](not found on this machine)[/yellow]")
        + ".[/dim]"
    )
    backend = click.prompt(
        "  Backend", type=click.Choice(list(BACKENDS)), default=default_backend,
    )
    target.set("defaults", "transcribe_backend", backend)

    if backend == LOCAL_BACKEND and not found_local:
        where = click.prompt(
            "  Path to faster-whisper-xxl (leave blank to set it later)", default="",
        )
        if where:
            target.set("defaults", "faster_whisper_path", where)
            if not find_faster_whisper(where):
                console.print("  [yellow]Nothing executable there yet. "
                              "Device commands still work; transcription will not."
                              "[/yellow]")

    if backend == LOCAL_BACKEND:
        default_model = (target.get("defaults", "whisper_model", "")
                         or DEFAULT_LOCAL_MODEL)
        if str(default_model).endswith(".en"):
            # Carrying an English-only model over would transcribe Italian and
            # German into phonetic nonsense.
            default_model = DEFAULT_LOCAL_MODEL
        model = click.prompt("  Model", default=default_model)
    elif backend == DEFAULT_BACKEND:
        model = click.prompt(
            "  Whisper model",
            type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
            default=target.get("defaults", "whisper_model", "") or "base.en",
        )
    else:
        model = target.get("defaults", "whisper_model", "") or "base.en"
    target.set("defaults", "whisper_model", model)

    console.print(
        "\n  [bold]Language[/bold]\n"
        "  [dim]'auto' detects it per recording. Set one explicitly if you always "
        "speak the same\n  language: detection on a short recording is a coin flip, "
        "and losing it produces\n  phonetic nonsense rather than an obvious error."
        "[/dim]"
    )
    language = click.prompt(
        "  Language (auto, or a code like it, de, en)",
        default=target.get("defaults", "language", "") or "auto",
    )
    target.set("defaults", "language", language.strip().lower())

    if target_profile:
        label = click.prompt("  Label shown in the web interface",
                             default=target.get("web", "label", "")
                             or target_profile.replace("-", " ").replace("_", " ").title())
        target.set("web", "label", label)
        # One default is enough to answer "which profile?"; without it every
        # command needs --profile, which is the right default for two people
        # sharing a machine but surprising for one person with two recorders.
        if len(list_profiles(new_config)) > 1 and not new_config.get("default_profile"):
            if click.confirm(f"\n  Make '{target_profile}' the default profile?", default=False):
                new_config["default_profile"] = target_profile
        elif len(list_profiles(new_config)) == 1:
            new_config["default_profile"] = target_profile

    # Save
    save_config(new_config)
    console.print(f"\n[green]Config saved to {CONFIG_FILE}[/green]")
    for warning in profile_warnings(new_config):
        console.print(f"[yellow]  {warning}[/yellow]")

    saved = effective_config(new_config, target_profile or None)
    saved_address = resolve_address(saved)
    saved_key = resolve_session_key(saved)

    # Test connection
    if saved_address and saved_key:
        if click.confirm("\n  Test connection to device?", default=True):
            try:
                async def _test():
                    async with PocketCommander(saved_address) as cmd:
                        ok = await cmd.authenticate(saved_key)
                        if ok:
                            battery = await cmd.get_battery()
                            console.print(f"  [green]Connected! Battery: {battery}%[/green]")
                        else:
                            console.print("  [yellow]Connected but authentication failed.[/yellow]")
                asyncio.run(_test())
            except Exception as e:
                console.print(f"  [yellow]Connection failed: {e}[/yellow]")
                console.print("  [dim]Make sure the device is awake (press button).[/dim]")

    if not saved_key:
        key_path = (f"profiles.{target_profile}.session_key" if target_profile
                    else "device.session_key")
        console.print(Panel(
            "[bold yellow]No session key configured.[/bold yellow]\n\n"
            "Device commands (status, list, download, sync, web) won't work\n"
            "until you set one. Capture it from the vendor app's APP&SK& write\n"
            "(see PROTOCOL.md), then run:\n"
            f"  pocket-libre config --set {key_path}=YOUR-KEY",
            border_style="yellow",
        ))

    flag = f" --profile {target_profile}" if target_profile else ""
    console.print(Panel(
        "[bold green]Setup complete![/bold green]\n\n"
        "Get started:\n"
        f"  [bold]pocket-libre{flag} web[/bold]     Open the web interface (recommended)\n"
        f"  [bold]pocket-libre{flag} sync[/bold]    Download + transcribe + summarize all recordings\n\n"
        "Other commands:\n"
        f"  [bold]pocket-libre{flag} status[/bold]  Check device battery & storage\n"
        f"  [bold]pocket-libre{flag} list[/bold]    List recordings on device"
        + ("\n  [bold]pocket-libre profiles[/bold]        Show every configured recorder"
           if target_profile else ""),
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

    # Always the file as written: the profile-folded view would save one
    # profile's values over the global sections and drop the others.
    config = ctx.obj["raw_config"]

    if set_value:
        if "=" not in set_value or "." not in set_value.split("=")[0]:
            raise click.UsageError(
                "Format: --set section.key=value (e.g., device.address=ABC123)\n"
                "Profile settings take a third part: profiles.hers.address=ABC123"
            )
        path, value = set_value.split("=", 1)
        parts = path.split(".")
        if len(parts) == 2:
            section, key = parts
            config.setdefault(section, {})[key] = value
        elif len(parts) == 3 and parts[0] == PROFILES_SECTION:
            _, name, key = parts
            if not VALID_PROFILE_NAME.match(name):
                raise click.UsageError(
                    f"{name!r} is not a usable profile name. Use lowercase letters, "
                    "digits, '-' or '_', starting with a letter or digit."
                )
            config.setdefault(PROFILES_SECTION, {}).setdefault(name, {})[key] = value
        else:
            raise click.UsageError(
                f"Don't know where to put {path!r}. Use section.key, "
                "or profiles.<name>.key for a profile setting."
            )
        save_config(config)
        console.print(f"[green]Set {path}[/green]")
        for warning in profile_warnings(config):
            console.print(f"[yellow]  {warning}[/yellow]")
        return

    if not config:
        console.print("[yellow]No config file found.[/yellow] Run: [bold]pocket-libre setup[/bold]")
        return

    active = ctx.obj["profile"]
    if active:
        console.print(f"[dim]Active profile: [/dim][bold]{active}[/bold]")

    def _print_table(values: dict, indent: str = "  "):
        for key, val in values.items():
            if isinstance(val, dict):
                continue
            display = val
            if key in ("anthropic_key", "hf_token", "session_key") and val and len(str(val)) > 8:
                display = f"...{str(val)[-4:]}"
            console.print(f"{indent}{key} = {display}")

    for key, val in config.items():
        if not isinstance(val, dict):
            console.print(f"[bold cyan]{key}[/bold cyan] = {val}")

    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        if section == PROFILES_SECTION:
            for name, table in values.items():
                marker = "  [green](active)[/green]" if name == active else ""
                header = escape(f"[profiles.{name}]")
                console.print(f"\n[bold cyan]{header}[/bold cyan]{marker}")
                if isinstance(table, dict):
                    _print_table(table)
            continue
        console.print(f"\n[bold cyan]{escape(f'[{section}]')}[/bold cyan]")
        _print_table(values)


@cli.command("profiles")
@click.pass_context
def list_profiles_cmd(ctx):
    """List configured recorders, their libraries, and their web ports."""
    config = ctx.obj["raw_config"]
    profiles = list_profiles(config)

    if not profiles:
        console.print(
            "[yellow]No profiles configured.[/yellow] This config describes a single "
            "device.\n[dim]Add one per recorder with: "
            "pocket-libre setup --profile <name>[/dim]"
        )
        return

    from rich.table import Table

    active = ctx.obj["profile"]
    default = config.get("default_profile")

    table = Table(show_header=True, header_style="bold")
    table.add_column("")
    table.add_column("Profile")
    table.add_column("Label")
    table.add_column("Device")
    table.add_column("Key")
    table.add_column("Port")
    table.add_column("Library")

    for name in sorted(profiles):
        marks = []
        if name == active:
            marks.append("[green]*[/green]")
        if name == default:
            marks.append("[dim]d[/dim]")
        key = resolve_session_key(config, profile=name)
        own_key = bool(profiles[name].get("session_key"))
        table.add_row(
            " ".join(marks),
            f"[bold]{name}[/bold]",
            profile_label(config, name),
            str(profiles[name].get("address") or "[yellow]not set[/yellow]"),
            ("own" if own_key else "shared") if key else "[yellow]missing[/yellow]",
            str(resolve_web_port(config, profile=name)),
            get_output_dir(config, profile=name),
        )

    console.print(table)
    console.print("[dim]* active   d default   "
                  f"Key 'shared' means it falls back to {escape('[device]')}"
                  ".session_key[/dim]")
    for warning in profile_warnings(config):
        console.print(f"[yellow]  {warning}[/yellow]")


@cli.group("speakers", invoke_without_command=True)
@click.pass_context
def speakers_group(ctx):
    """Put names to voices in this library.

    \b
    Diarization labels are per recording: SPEAKER_01 in one file is not the
    same person as SPEAKER_01 in the next. A name comes from a voice enrolled
    once, then matched by its embedding in every later recording.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(speakers_list)


def _voice_library(ctx):
    from pocket_libre.speakers import VoiceLibrary

    _require_resolved_profile(ctx)
    library_dir = Path(get_output_dir(ctx.obj["config"]))
    return library_dir, VoiceLibrary.load(library_dir)


def _recording_voices(library_dir: Path, recording: str) -> tuple[Path, dict]:
    """Find a recording's stored embeddings from a `DATE/TIMESTAMP` reference."""
    from pocket_libre.speakers import load_recording_voices

    reference = recording.strip().replace("\\", "/")
    if "/" not in reference:
        raise click.UsageError(
            "Give the recording as DATE/TIMESTAMP, for example "
            f"2026-10-04/20261004153000 (got {recording!r})."
        )
    date_part, stamp = reference.rsplit("/", 1)
    path = library_dir / date_part / f"{stamp}_voices.json"
    voices = load_recording_voices(path)
    if not voices:
        raise click.UsageError(
            f"No speaker embeddings stored for {reference}.\n"
            "They are written when a recording is processed by the "
            "faster-whisper-xxl backend with diarization on."
        )
    return path, voices


@speakers_group.command("list")
@click.pass_context
def speakers_list(ctx):
    """Show the voices enrolled in this library."""
    library_dir, library = _voice_library(ctx)
    console.print(f"[dim]Library: {library_dir}[/dim]")
    if not library.voices:
        console.print(
            "[yellow]No voices enrolled.[/yellow]\n"
            "[dim]Enroll one from a recording you can identify by ear:\n"
            "  pocket-libre speakers enroll <name> "
            "--recording DATE/TIMESTAMP --label SPEAKER_01[/dim]"
        )
        return
    for name in library.names:
        samples = len(library.voices[name])
        console.print(f"  [bold]{name}[/bold]  {samples} sample(s)")
    console.print(f"[dim]Match threshold: {library.threshold:.2f}[/dim]")


@speakers_group.command("enroll")
@click.argument("name")
@click.option("--recording", required=True, metavar="DATE/TIMESTAMP",
              help="Recording to take the voice from.")
@click.option("--label", required=True,
              help="Which diarized speaker in that recording (e.g. SPEAKER_01).")
@click.pass_context
def speakers_enroll(ctx, name: str, recording: str, label: str):
    """Teach this library a voice, from one speaker in one recording."""
    library_dir, library = _voice_library(ctx)
    _path, voices = _recording_voices(library_dir, recording)

    if label not in voices:
        raise click.UsageError(
            f"{recording} has no speaker {label!r}. It has: "
            + ", ".join(sorted(voices))
        )

    library.enroll(name, voices[label])
    library.save()
    console.print(
        f"[green]Enrolled {name} from {recording} ({label}).[/green] "
        f"{len(library.voices[name])} sample(s) on file."
    )
    console.print("[dim]Add a sample from another recording to make the match "
                  "hold up across rooms and microphones.[/dim]")


@speakers_group.command("forget")
@click.argument("name")
@click.pass_context
def speakers_forget(ctx, name: str):
    """Remove an enrolled voice."""
    _library_dir, library = _voice_library(ctx)
    if not library.forget(name):
        raise click.UsageError(
            f"No voice named {name!r}. Enrolled: "
            + (", ".join(library.names) or "none")
        )
    library.save()
    console.print(f"[green]Forgot {name}.[/green]")


@speakers_group.command("test")
@click.option("--recording", required=True, metavar="DATE/TIMESTAMP",
              help="Recording to score against the enrolled voices.")
@click.option("--threshold", default=None, type=float,
              help="Try a different match threshold, for this run only.")
@click.pass_context
def speakers_test(ctx, recording: str, threshold: float | None):
    """Score one recording's speakers against every enrolled voice.

    \b
    Pick a threshold from real numbers rather than trusting the default: the
    right cut-off depends on the microphone and on the voices.
    """
    from rich.table import Table

    from pocket_libre.speakers import identify

    library_dir, library = _voice_library(ctx)
    _path, voices = _recording_voices(library_dir, recording)

    if not library.voices:
        console.print("[yellow]No voices enrolled yet, so there is nothing to "
                      "score against.[/yellow]")
        console.print("Speakers in this recording: " + ", ".join(sorted(voices)))
        return

    limit = library.threshold if threshold is None else threshold
    matched = identify(voices, library, threshold=limit)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Speaker")
    for name in library.names:
        table.add_column(name, justify="right")
    table.add_column("Match")

    for label in sorted(voices):
        scores = dict(library.score(voices[label]))
        row = [label] + [f"{scores.get(name, 0.0):.3f}" for name in library.names]
        row.append(matched.get(label) or "[dim]unnamed[/dim]")
        table.add_row(*row)

    console.print(table)
    console.print(f"[dim]Threshold {limit:.2f}. Below it a speaker stays unnamed, "
                  "which is the right answer when it is someone else.[/dim]")


@speakers_group.command("threshold")
@click.argument("value", type=float)
@click.pass_context
def speakers_threshold(ctx, value: float):
    """Set the match threshold for this library."""
    if not 0.0 < value < 1.0:
        raise click.UsageError("A cosine similarity threshold lies between 0 and 1.")
    _library_dir, library = _voice_library(ctx)
    library.threshold = value
    library.save()
    console.print(f"[green]Match threshold set to {value:.2f}.[/green]")


@cli.command("search")
@click.argument("query", nargs=-1, required=True)
@click.option("--limit", default=20, type=int, help="How many results to show.")
@click.option("--kind", "kinds", multiple=True,
              type=click.Choice(["transcript", "summary", "actions"]),
              help="Restrict to one kind of document. Repeatable.")
@click.option("--reindex", is_flag=True, help="Rebuild the index from scratch first.")
@click.pass_context
def search_library(ctx, query: tuple, limit: int, kinds: tuple, reindex: bool):
    """Search this library's transcripts and summaries.

    \b
    The index lives inside the library and is refreshed before each search,
    so it is never stale and never reaches another profile's recordings.
    """
    from pocket_libre.index import MATCH_CLOSE, MATCH_OPEN, build, search

    _require_resolved_profile(ctx)
    library = Path(get_output_dir(ctx.obj["config"]))
    if not library.is_dir():
        raise click.UsageError(f"No library at {library} yet. Sync something first.")

    if reindex:
        stats = build(library, rebuild=True)
        console.print(f"[dim]Indexed {stats['added']} document(s).[/dim]")

    phrase = " ".join(query)
    hits = search(library, phrase, limit=limit, kinds=kinds or None)

    if not hits:
        console.print(f"[yellow]Nothing matched {phrase!r}.[/yellow]")
        return

    console.print(f"[dim]{len(hits)} result(s) in {library}[/dim]\n")
    for hit in hits:
        console.print(f"[bold]{hit.reference}[/bold]  [dim]{hit.kind}[/dim]")
        # Escape first, then turn the match markers into styling, so nothing in
        # a transcript can inject console markup.
        marked = (escape(hit.snippet)
                  .replace(MATCH_OPEN, "[bold yellow]")
                  .replace(MATCH_CLOSE, "[/bold yellow]"))
        console.print(f"  {marked}\n")


@cli.command("reindex")
@click.pass_context
def reindex_library(ctx):
    """Rebuild this library's search index from the files on disk."""
    from pocket_libre.index import build, index_path

    _require_resolved_profile(ctx)
    library = Path(get_output_dir(ctx.obj["config"]))
    stats = build(library, rebuild=True)
    console.print(
        f"[green]Indexed {stats['added']} document(s)[/green] "
        f"[dim]-> {index_path(library)}[/dim]"
    )


@cli.command("export")
@click.option("--recording", default=None, metavar="DATE/TIMESTAMP",
              help="Export one recording. Omit to export everything not yet written.")
@click.option("--to", "destination", default=None,
              help="Where to write the notes. Defaults to the profile's export path.")
@click.option("--overwrite", is_flag=True,
              help="Replace notes that already exist, losing any hand edits.")
@click.option("--dry-run", is_flag=True, help="Show what would be written.")
@click.pass_context
def export_notes(ctx, recording: str | None, destination: str | None,
                 overwrite: bool, dry_run: bool):
    """Write processed recordings out as Markdown notes.

    \b
    One self-contained note per recording: summary, action items as checkboxes,
    and the full transcript. Off unless this profile sets an export path, so
    nobody's recordings land in someone else's notes by default.
    """
    from pocket_libre.analyze import load_analyses
    from pocket_libre.export import action_items_from_entities, export_note

    _require_resolved_profile(ctx)
    config = ctx.obj["config"]
    profile = ctx.obj["profile"]
    library = Path(get_output_dir(config))

    enabled = get(config, "export", "vault", default=False)
    target = destination or get(config, "export", "vault_path", default="")
    if not target:
        raise click.UsageError(
            "No export path for this profile. Set one with:\n"
            f"  pocket-libre config --set profiles.{profile or '<name>'}"
            ".vault_path=<folder>\n"
            f"  pocket-libre config --set profiles.{profile or '<name>'}"
            ".vault_export=true\n"
            "or pass --to <folder> for a one-off."
        )
    if not enabled and not destination:
        raise click.UsageError(
            f"Export is off for this profile. Turn it on with:\n"
            f"  pocket-libre config --set profiles.{profile or '<name>'}"
            ".vault_export=true\n"
            "or pass --to <folder> for a one-off."
        )

    out = Path(os.path.expanduser(str(target)))

    if not library.is_dir():
        raise click.UsageError(f"No library at {library} yet. Sync something first.")

    wanted = []
    if recording:
        reference = recording.strip().replace("\\", "/")
        if "/" not in reference:
            raise click.UsageError("Give the recording as DATE/TIMESTAMP.")
        day, stamp = reference.rsplit("/", 1)
        wanted.append((day, stamp))
    else:
        for date_dir in sorted(p for p in library.iterdir() if p.is_dir()):
            for transcript in sorted(date_dir.glob("*_transcript.txt")):
                wanted.append((date_dir.name, transcript.name[: -len("_transcript.txt")]))

    if not wanted:
        console.print("[yellow]Nothing processed to export yet.[/yellow]")
        return

    written, skipped = 0, 0
    for day, stamp in wanted:
        rec_dir = library / day
        transcript_path = rec_dir / f"{stamp}_transcript.txt"
        if not transcript_path.is_file():
            console.print(f"[yellow]{day}/{stamp}: no transcript, skipped.[/yellow]")
            skipped += 1
            continue

        summary_path = rec_dir / f"{stamp}_summary.md"
        summary = (summary_path.read_text(encoding="utf-8")
                   if summary_path.is_file() else None)
        analyses = load_analyses(rec_dir, stamp)
        actions = action_items_from_entities(analyses.get("entities"))

        transcript = transcript_path.read_text(encoding="utf-8")

        if dry_run:
            console.print(
                f"  {day}/{stamp} -> {out}  "
                f"[dim]{len(actions)} action item(s)[/dim]"
            )
            continue

        try:
            path = export_note(
                out,
                recording=f"{day}/{stamp}",
                recorded_on=day,
                transcript=transcript,
                summary=summary,
                actions=actions,
                profile=profile,
                overwrite=overwrite,
            )
        except FileExistsError as e:
            console.print(f"[dim]{day}/{stamp}: already exported ({Path(str(e)).name}).[/dim]")
            skipped += 1
            continue

        written += 1
        console.print(f"[green]{day}/{stamp}[/green] -> {path}")

    if dry_run:
        console.print(f"\n[dim]{len(wanted)} recording(s) would be written to {out}.[/dim]")
        return

    console.print(
        f"\n[bold green]{written} note(s) written.[/bold green]"
        + (f" [dim]{skipped} skipped.[/dim]" if skipped else "")
        + ("\n[dim]Use --overwrite to replace existing notes.[/dim]" if skipped else "")
    )


@cli.command("tasks")
@click.option("--recording", default=None, metavar="DATE/TIMESTAMP",
              help="One recording. Omit for every processed recording.")
@click.pass_context
def list_tasks(ctx, recording: str | None):
    """Print the commitments found in processed recordings, as checkboxes.

    \b
    Ready to paste into a task list. Nothing is ever added anywhere
    automatically, and a task only carries a date if the transcript said one.
    """
    from pocket_libre.analyze import load_analyses
    from pocket_libre.export import action_items_from_entities

    _require_resolved_profile(ctx)
    library = Path(get_output_dir(ctx.obj["config"]))
    if not library.is_dir():
        raise click.UsageError(f"No library at {library} yet.")

    references = []
    if recording:
        reference = recording.strip().replace("\\", "/")
        if "/" not in reference:
            raise click.UsageError("Give the recording as DATE/TIMESTAMP.")
        references.append(tuple(reference.rsplit("/", 1)))
    else:
        for date_dir in sorted(p for p in library.iterdir() if p.is_dir()):
            for found in sorted(date_dir.glob("*_entities.json")):
                references.append((date_dir.name, found.name[: -len("_entities.json")]))

    marker = _due_marker()
    total = 0
    for day, stamp in references:
        items = action_items_from_entities(
            load_analyses(library / day, stamp).get("entities")
        )
        if not items:
            continue
        console.print(f"\n[dim]{day}/{stamp}[/dim]")
        for item in items:
            console.print(escape(item.as_checkbox(due_marker=marker)))
        total += len(items)

    if not total:
        console.print(
            "[yellow]No action items found.[/yellow]\n"
            "[dim]They come from the 'entities' analysis, which needs an "
            "Anthropic key. Check: pocket-libre config[/dim]"
        )


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
    session_key = _require_session_key(session_key, config)
    out_root = Path(get_output_dir(config, output_dir))
    from pocket_libre.commands import download_with_retry

    async def _run():
        async with PocketCommander(address) as cmd:
            console.print("[dim]Authenticating...[/dim]")
            if not await cmd.authenticate(session_key):
                console.print("[red]Auth failed.[/red]")
                return []
            all_recs = await cmd.list_all_recordings()

        if since:
            all_recs = [r for r in all_recs if r.date >= since]

        if not all_recs:
            console.print("[yellow]No recordings found.[/yellow]")
            return []

        console.print(f"[bold]{len(all_recs)} recording(s) to download[/bold]\n")

        # Each download gets its own connection, so a dropped link is
        # retried instead of ending the run (see download_with_retry).
        downloaded_paths = []
        for i, rec in enumerate(all_recs, 1):
            rec_dir = out_root / rec.date
            rec_dir.mkdir(parents=True, exist_ok=True)
            out_path = rec_dir / f"{rec.timestamp}.mp3"

            if out_path.exists():
                console.print(f"  [{i}/{len(all_recs)}] {rec.date}/{rec.timestamp} [dim](already exists, skipping)[/dim]")
                downloaded_paths.append(out_path)
                continue

            console.print(
                f"  [{i}/{len(all_recs)}] {rec.date}/{rec.timestamp} "
                f"(~{rec.estimated_bytes // 1024:,} KB)..."
            )

            def progress(current, total):
                if total > 0:
                    pct = 100 * current // total
                    console.print(f"\r    [dim]{pct}%[/dim]", end="")

            data = await download_with_retry(address, session_key, rec, progress_callback=progress)
            console.print()

            if data:
                out_path.write_bytes(data)
                downloaded_paths.append(out_path)
                console.print(f"    [green]Saved {len(data):,} bytes[/green]")
            else:
                console.print("    [red]Download failed; nothing saved. Re-run to try again.[/red]")

        console.print(f"\n[bold green]Downloaded {len(downloaded_paths)} recording(s) to {out_root}[/bold green]")
        return downloaded_paths

    downloaded_paths = asyncio.run(_run())

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
@click.option("--key", "session_key", default=None, help="Session key.")
@click.option("--skip-process", is_flag=True, help="Only download, skip transcription and summary.")
@click.option("--prompt", default=None, help="Custom summary prompt (use {transcript} placeholder).")
@click.pass_context
def sync(ctx, address: str | None, output_dir: str | None, since: str | None,
         whisper_model: str | None, style: str | None,
         anthropic_key: str | None, hf_token: str | None,
         session_key: str | None, skip_process: bool, prompt: str | None):
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
                console.print("  [red]Failed to download.[/red]")
                continue

            audio_path.write_bytes(data)
            console.print(f"  [green]Saved {len(data):,} bytes[/green]")

            if skip_process:
                continue

            # Transcribe and attach speakers
            options = options_from_config(config, whisper_model)
            console.print(
                f"  [dim]Transcribing ({options['backend']}, {options['model']})...[/dim]"
            )
            try:
                labeled, transcription = transcribe_and_label(
                    audio_path, hf_token=hf_token, anthropic_key=anthropic_key,
                    library_dir=out_root,
                    voices_path=rec_dir / f"{rec.timestamp}_voices.json",
                    **options,
                )
            except Exception as e:
                console.print(f"  [red]Transcription failed: {e}[/red]")
                continue

            from pocket_libre.summarize import format_transcript_for_summary
            transcript_text = format_transcript_for_summary(labeled)
            transcript_path = rec_dir / f"{rec.timestamp}_transcript.txt"
            transcript_path.write_text(transcript_text, encoding="utf-8")
            detected = f", {transcription.language}" if transcription.language else ""
            console.print(
                f"  [green]Transcript saved ({len(labeled)} segments"
                f"{detected})[/green]"
            )

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

    options = options_from_config(config, whisper_model)
    console.print(Panel(
        f"[bold]Processing: {input_path}[/bold]\n"
        f"Backend: {options['backend']} | Model: {options['model']} | "
        f"Language: {options['language']} | Style: {style}",
        border_style="cyan",
    ))

    # Transcribe, then attach speakers
    console.print("\n[bold cyan]Step 1/2: Transcribing and identifying speakers..."
                  "[/bold cyan]\n")
    try:
        labeled, transcription = transcribe_and_label(
            input_file, hf_token=hf_token, anthropic_key=anthropic_key,
            library_dir=get_output_dir(config),
            voices_path=out_dir / f"{stem}_voices.json",
            **options,
        )
    except TranscriptionError as e:
        console.print(f"[red]{e}[/red]")
        return

    detected = f", language {transcription.language}" if transcription.language else ""
    console.print(f"[green]Transcribed: {len(labeled)} segments{detected}[/green]")
    if transcription.speakers:
        console.print(f"[green]Speakers: {', '.join(transcription.speakers)}[/green]")

    from pocket_libre.summarize import format_transcript_for_summary
    transcript_text = format_transcript_for_summary(labeled)

    transcript_path = out_dir / f"{stem}_transcript.txt"
    transcript_path.write_text(transcript_text, encoding="utf-8")
    console.print(f"[green]Transcript saved: {transcript_path}[/green]")

    # Summarize
    if skip_summary:
        console.print("\n[dim]Skipping summary.[/dim]")
    else:
        console.print("\n[bold cyan]Step 2/2: Summarizing...[/bold cyan]\n")
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
@click.option("--all", "all_profiles", is_flag=True,
              help="Watch every configured profile, one device at a time.")
@click.pass_context
def watch(ctx, address: str | None, session_key: str | None, interval: float,
          output_dir: str | None, do_process: bool, all_profiles: bool):
    """Watch for the device and sync new recordings automatically.

    \b
    Runs until interrupted. Scans for your Pocket every --interval seconds;
    when it appears, downloads anything not already on disk. Backs off to
    5-minute checks while the device is away.

    \b
    With --all, every profile is watched in turn and each recording lands in
    its own profile's library.
    """
    from pocket_libre.watch import sync_new_recordings, watch_loop

    if all_profiles:
        if address or session_key or output_dir:
            raise click.UsageError(
                "--all watches every profile, so --address, --key and "
                "--output-dir cannot apply. Drop them, or watch one profile."
            )
        _watch_all_profiles(ctx, interval, do_process)
        return

    _require_resolved_profile(ctx)
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
            backend_options=options_from_config(config, whisper_model),
        )

    try:
        stats = asyncio.run(watch_loop(address, _sync_once, poll_interval=interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
        return

    console.print(
        f"[dim]{stats.scans} scans, {stats.recordings_synced} recordings synced.[/dim]"
    )


def _watch_all_profiles(ctx, interval: float, do_process: bool) -> None:
    """Watch every configured profile, sequentially, each into its own library."""
    from pocket_libre.watch import WatchTarget, sync_new_recordings, watch_many

    raw = ctx.obj["raw_config"]
    profiles = list_profiles(raw)
    if not profiles:
        raise click.UsageError(
            "--all needs profiles. This config describes a single device, so "
            "plain 'pocket-libre watch' is what you want.\n"
            "Add a profile per recorder with: pocket-libre setup --profile <name>"
        )

    targets = []
    skipped = []
    for name in sorted(profiles):
        # One folded config per profile: each closure below can only ever see
        # its own device, library and credentials.
        scoped = effective_config(raw, name)
        addr = resolve_address(scoped)
        key = resolve_session_key(scoped)
        if not addr or not key:
            missing = "address" if not addr else "session key"
            skipped.append(f"{name} (no {missing})")
            continue

        out_root = Path(get_output_dir(scoped))
        targets.append(WatchTarget(
            name=name,
            address=addr,
            sync_once=functools.partial(
                sync_new_recordings,
                address=addr,
                session_key=key,
                out_root=out_root,
                process=do_process,
                whisper_model=get(scoped, "defaults", "whisper_model", default="base.en"),
                summary_style=get(scoped, "defaults", "summary_style", default="meeting"),
                anthropic_key=resolve_anthropic_key(scoped),
                hf_token=resolve_hf_token(scoped),
                backend_options=options_from_config(scoped),
            ),
        ))

    if not targets:
        raise click.UsageError(
            "No profile is ready to watch: " + ", ".join(skipped) + ".\n"
            "Finish one with: pocket-libre setup --profile <name>"
        )

    lines = "\n".join(
        f"  {t.name:<12} {t.address}  ->  {get_output_dir(effective_config(raw, t.name))}"
        for t in targets
    )
    console.print(Panel(
        f"[bold]Watching {len(targets)} profile(s), one device at a time[/bold]\n\n"
        f"{lines}\n\n"
        f"Interval:   {interval:.0f}s per profile\n"
        f"Processing: {'on' if do_process else 'off'}\n"
        + (f"Skipped:    {', '.join(skipped)}\n" if skipped else "")
        + "\nPress Ctrl+C to stop.",
        border_style="cyan",
    ))

    try:
        stats = asyncio.run(watch_many(targets, poll_interval=interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
        return

    for name in sorted(stats):
        s = stats[name]
        console.print(
            f"[dim]{name}: {s.scans} scans, {s.recordings_synced} recordings synced, "
            f"{s.failures} failure(s).[/dim]"
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
              help="Without --date/--timestamp: only recordings from this date (YYYY-MM-DD) on.")
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
@click.pass_context
def wifi_transfer(ctx, address: str | None, session_key: str | None, date: str | None,
                  timestamp: str | None, since: str | None, output: str | None,
                  output_dir: str | None, overwrite: bool, wifi_backend: str,
                  iface: str | None, force: bool):
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

    from pocket_libre.commands import is_safe_id
    from pocket_libre.hostwifi import HostWifiError, backend
    from pocket_libre.protocol import FILES_PER_AP_SESSION
    from pocket_libre.wifi import (
        DEFAULT_HOST,
        WifiSession,
        WifiTransferError,
        files_per_ap_session,
        firmware_line,
    )

    if (date is None) != (timestamp is None):
        raise click.UsageError("Pass --date and --timestamp together, or neither.")
    if output and date is None:
        raise click.UsageError("--output is for a single recording; use --output-dir.")
    if date is not None and not (is_safe_id(date) and is_safe_id(timestamp)):
        raise click.UsageError("--date and --timestamp must be plain identifiers.")

    config = ctx.obj["config"]
    address = _require_address(address, config)
    session_key = _require_session_key(session_key, config)
    out_root = Path(get_output_dir(config, output_dir))

    def log(text: str) -> None:
        console.print(f"[dim]{text}[/dim]")

    def lost_link(error: Exception | None, not_attempted: int) -> None:
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
                jobs = [(rec, Path(output) if output else Path(f"{timestamp}.mp3"))]
            else:
                recs = await cmd.list_all_recordings()
                if since:
                    recs = [r for r in recs if r.date >= since]
                jobs = [(r, out_root / r.date / f"{r.timestamp}.mp3") for r in recs]
            todo = [(r, p) for r, p in jobs if overwrite or not p.exists()]
            skipped = len(jobs) - len(todo)
            if skipped:
                console.print(f"[dim]{skipped} recording(s) already downloaded, skipping.[/dim]")
            if not todo:
                console.print("[yellow]Nothing to download.[/yellow]")
                return 0, 0

            console.print(
                f"[bold]{len(todo)} recording(s) to download over WiFi.[/bold] "
                "This machine's WiFi switches to the device's network until done."
            )
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
                            continue
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
    if failed:
        raise SystemExit(1)


# ── Web Interface ───────────────────────────────


@cli.command()
@click.option("--port", default=None, type=int,
              help="Port to serve on. Defaults to the profile's own port.")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically.")
@click.option("--all", "all_profiles", is_flag=True,
              help="Serve every profile, each on its own port, in its own process.")
@click.pass_context
def web(ctx, host: str, port: int | None, no_browser: bool, all_profiles: bool):
    """Launch the Pocket Libre web interface.

    Opens a browser-based UI for managing recordings, transcripts,
    and summaries. No terminal required after launch.

    \b
    With --all, one server is started per profile, each bound to that
    profile's library and reachable on that profile's port.
    """
    import webbrowser

    import uvicorn

    if all_profiles:
        if port is not None:
            raise click.UsageError(
                "--all gives every profile its own port, so --port cannot apply. "
                "Set a profile's port with: "
                "pocket-libre config --set profiles.<name>.web_port=<port>"
            )
        _serve_all_profiles(ctx, host, no_browser)
        return

    _require_resolved_profile(ctx)
    profile = ctx.obj["profile"]
    config = ctx.obj["config"]
    port = resolve_web_port(config, port)

    # 0.0.0.0 is not a connectable address — point the browser at loopback.
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    whose = f"\nProfile:   {profile_label(config, profile)} ({profile})" if profile else ""
    console.print(Panel(
        f"[bold]Pocket Libre Web UI[/bold]{whose}\n"
        f"Library:   {get_output_dir(config)}\n\n"
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

    # Bind the app to this profile before serving, and hand uvicorn the object
    # rather than an import string so the setting cannot be lost to a reimport.
    from pocket_libre.web import app as web_app

    web_app.set_active_profile(profile)
    uvicorn.run(web_app.app, host=host, port=port, log_level="warning")


def _serve_all_profiles(ctx, host: str, no_browser: bool) -> None:
    """Start one web server per profile, each in its own process.

    Separate processes, not threads: the app binds its profile in module-level
    state, so two servers sharing an interpreter would serve each other's
    libraries. A process per profile makes that impossible.
    """
    import subprocess
    import sys
    import time
    import webbrowser

    raw = ctx.obj["raw_config"]
    profiles = list_profiles(raw)
    if not profiles:
        raise click.UsageError(
            "--all needs profiles. This config describes a single device, so "
            "plain 'pocket-libre web' is what you want.\n"
            "Add a profile per recorder with: pocket-libre setup --profile <name>"
        )

    ports = {name: resolve_web_port(raw, profile=name) for name in sorted(profiles)}
    clashes = [p for p in set(ports.values()) if list(ports.values()).count(p) > 1]
    if clashes:
        raise click.UsageError(
            f"Two profiles want the same port ({', '.join(map(str, sorted(clashes)))}). "
            "Give each one its own: "
            "pocket-libre config --set profiles.<name>.web_port=<port>"
        )

    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    lines = "\n".join(
        f"  {profile_label(raw, name):<14} http://{browse_host}:{ports[name]}"
        f"  ->  {get_output_dir(raw, profile=name)}"
        for name in sorted(profiles)
    )
    console.print(Panel(
        f"[bold]Pocket Libre Web UI, one server per profile[/bold]\n\n{lines}\n\n"
        "Press Ctrl+C to stop all of them.",
        border_style="cyan",
    ))

    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(Panel(
            "[bold yellow]This binds a non-loopback address.[/bold yellow]\n\n"
            "The web interface has no authentication. Anyone who can reach\n"
            "these ports can read both libraries, control both devices,\n"
            "and spend your API credits.\n\n"
            "Only do this on a network you trust.",
            title="Warning",
            border_style="yellow",
        ))

    children = []
    try:
        for name in sorted(profiles):
            children.append(subprocess.Popen([
                sys.executable, "-m", "pocket_libre.cli",
                "--profile", name, "web",
                "--host", host, "--port", str(ports[name]), "--no-browser",
            ]))

        if not no_browser:
            time.sleep(1.5)
            for name in sorted(profiles):
                webbrowser.open(f"http://{browse_host}:{ports[name]}")

        for child in children:
            child.wait()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping.[/yellow]")
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    cli()
