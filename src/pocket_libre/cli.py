"""CLI entry point for Pocket Libre."""

import click

from pocket_libre.scanner import scan_devices
from pocket_libre.explorer import explore_device
from pocket_libre.capture import capture_audio
from pocket_libre.transcribe import transcribe_audio
from pocket_libre.sniffer import sniff_all


@click.group()
@click.version_option()
def cli():
    """Pocket Libre: Liberate your Pocket AI recorder from the cloud."""
    pass


@cli.command()
@click.option("--timeout", default=10.0, help="Scan duration in seconds.")
@click.option("--filter", "name_filter", default=None, help="Filter devices by name (case-insensitive).")
def scan(timeout: float, name_filter: str | None):
    """Scan for nearby BLE devices. Use this to find your Pocket."""
    import asyncio

    asyncio.run(scan_devices(timeout=timeout, name_filter=name_filter))


@cli.command()
@click.option("--address", required=True, help="BLE address of your Pocket device.")
def explore(address: str):
    """Connect to a device and dump all GATT services and characteristics."""
    import asyncio

    asyncio.run(explore_device(address=address))


@cli.command()
@click.option("--address", required=True, help="BLE address of your Pocket device.")
@click.option("--duration", default=15, help="Sniff duration in seconds.")
def sniff(address: str, duration: int):
    """Subscribe to ALL notify characteristics and show what's streaming.

    This is the fastest way to figure out which characteristic carries audio.
    High-volume streams are highlighted and auto-saved.
    """
    import asyncio

    asyncio.run(sniff_all(address=address, duration=duration))


@cli.command()
@click.option("--address", required=True, help="BLE address of your Pocket device.")
@click.option("--output", default="recording.raw", help="Output file path.")
@click.option("--duration", default=30, help="Max capture duration in seconds.")
@click.option(
    "--char-uuid",
    default=None,
    help="UUID of the audio characteristic. If not set, attempts auto-detection.",
)
def capture(address: str, output: str, duration: int, char_uuid: str | None):
    """Connect to Pocket and capture audio data from a BLE characteristic."""
    import asyncio

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
@click.option(
    "--model",
    default="base.en",
    type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
    help="Whisper model size.",
)
@click.option("--output", default=None, help="Save transcript to file (default: print to stdout).")
@click.option(
    "--format",
    "output_format",
    default="text",
    type=click.Choice(["text", "json", "srt"]),
    help="Transcript output format.",
)
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
@click.option("--bit-depth", default=16, type=click.Choice([8, 16], case_sensitive=False), help="Bit depth.")
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


@cli.command()
@click.option("--address", required=True, help="BLE address of your Pocket device.")
@click.option("--duration", default=60, help="Capture window in seconds.")
@click.option("--output-dir", default="~/pocket-recordings", help="Where to save recordings.")
@click.option(
    "--whisper-model",
    default="base.en",
    type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
    help="Whisper model size.",
)
@click.option(
    "--style",
    default="meeting",
    type=click.Choice(["meeting", "notes", "call", "raw"]),
    help="Summary style.",
)
@click.option("--anthropic-key", default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY).")
@click.option("--hf-token", default=None, help="HuggingFace token for speaker diarization.")
@click.option("--skip-summary", is_flag=True, help="Skip AI summary, just transcribe.")
@click.option("--prompt", default=None, help="Custom summary prompt (use {transcript} placeholder).")
def sync(
    address: str,
    duration: int,
    output_dir: str,
    whisper_model: str,
    style: str,
    anthropic_key: str | None,
    hf_token: str | None,
    skip_summary: bool,
    prompt: str | None,
):
    """One-command sync: capture, transcribe, identify speakers, summarize.

    \b
    Connects to your Pocket, captures audio, runs Whisper locally,
    identifies speakers (if pyannote is installed), and summarizes
    using Claude Haiku (~$0.001 per recording).

    \b
    Output:
      ~/pocket-recordings/2026-03-28_143022/
        recording.mp3       Raw audio
        transcript.txt      Full transcript with speaker labels
        summary.md          AI-generated summary with action items

    \b
    Examples:
      pocket-libre sync --address 53E5A5FB-...
      pocket-libre sync --address 53E5A5FB-... --style notes --skip-summary
      pocket-libre sync --address 53E5A5FB-... --whisper-model small.en
    """
    import asyncio
    import os

    from pocket_libre.pipeline import run_sync

    # Check for env vars as fallbacks
    if not anthropic_key:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not hf_token:
        hf_token = os.environ.get("HUGGINGFACE_TOKEN")

    asyncio.run(
        run_sync(
            address=address,
            duration=duration,
            output_dir=output_dir,
            whisper_model=whisper_model,
            summary_style=style,
            hf_token=hf_token,
            anthropic_key=anthropic_key,
            skip_summary=skip_summary,
            custom_prompt=prompt,
        )
    )


@cli.command()
@click.option("--input", "input_path", required=True, help="Path to MP3 or WAV file.")
@click.option(
    "--whisper-model",
    default="base.en",
    type=click.Choice(["tiny.en", "base.en", "small.en", "medium.en", "large"]),
    help="Whisper model size.",
)
@click.option(
    "--style",
    default="meeting",
    type=click.Choice(["meeting", "notes", "call", "raw"]),
    help="Summary style.",
)
@click.option("--anthropic-key", default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY).")
@click.option("--hf-token", default=None, help="HuggingFace token for speaker diarization.")
@click.option("--skip-summary", is_flag=True, help="Skip AI summary.")
@click.option("--output", default=None, help="Output directory (default: same as input file).")
def process(
    input_path: str,
    whisper_model: str,
    style: str,
    anthropic_key: str | None,
    hf_token: str | None,
    skip_summary: bool,
    output: str | None,
):
    """Process an existing audio file: transcribe, diarize, summarize.

    Use this on recordings you already have (from sniff, capture, or anywhere).

    \b
    Examples:
      pocket-libre process --input sniff_dump.mp3
      pocket-libre process --input recording.mp3 --style notes --skip-summary
    """
    import os
    from pathlib import Path
    from datetime import datetime

    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    input_file = Path(input_path)
    if not input_file.exists():
        console.print(f"[red]File not found: {input_path}[/red]")
        return

    # Output location
    if output:
        out_dir = Path(os.path.expanduser(output))
    else:
        out_dir = input_file.parent

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_file.stem

    console.print(
        Panel(
            f"[bold]Processing: {input_path}[/bold]\n"
            f"Whisper: {whisper_model} | Style: {style}",
            border_style="cyan",
        )
    )

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
    from pocket_libre.diarize import (
        diarize_pyannote,
        diarize_simple,
        merge_transcript_with_speakers,
    )

    hf_token = hf_token or os.environ.get("HUGGINGFACE_TOKEN")
    speaker_segments = []

    if hf_token:
        try:
            speaker_segments = diarize_pyannote(str(input_file), hf_token)
        except Exception as e:
            console.print(f"[yellow]Diarization failed: {e}[/yellow]")

    if not speaker_segments:
        speaker_segments = diarize_simple(segments)
        if not hf_token:
            console.print("[dim]No HuggingFace token. Using basic segmentation.[/dim]")

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
        api_key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY")

        if not api_key:
            console.print(
                "[yellow]No ANTHROPIC_API_KEY set. Skipping summary.[/yellow]"
            )
        else:
            from pocket_libre.summarize import summarize_transcript

            summary = summarize_transcript(
                transcript_text=transcript_text,
                api_key=api_key,
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


if __name__ == "__main__":
    cli()
