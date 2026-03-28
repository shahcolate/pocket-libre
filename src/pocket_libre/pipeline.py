"""Full sync pipeline. Connect -> Capture -> Transcribe -> Diarize -> Summarize."""

import asyncio
import os
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel


console = Console()


async def run_sync(
    address: str,
    duration: int = 60,
    output_dir: str = "~/pocket-recordings",
    whisper_model: str = "base.en",
    summary_style: str = "meeting",
    hf_token: str | None = None,
    anthropic_key: str | None = None,
    skip_summary: bool = False,
    custom_prompt: str | None = None,
):
    """Full pipeline: capture audio, transcribe, diarize, summarize.

    Saves everything to a timestamped folder:
      ~/pocket-recordings/2026-03-28_143022/
        recording.mp3       (raw audio)
        transcript.txt      (full transcript with speaker labels)
        summary.md          (AI-generated summary)
    """
    # Resolve output directory
    output_dir = os.path.expanduser(output_dir)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    session_dir = Path(output_dir) / timestamp
    session_dir.mkdir(parents=True, exist_ok=True)

    audio_path = session_dir / "recording.mp3"
    transcript_path = session_dir / "transcript.txt"
    summary_path = session_dir / "summary.md"

    console.print(
        Panel(
            f"[bold]Pocket Libre Sync[/bold]\n\n"
            f"Device:    {address}\n"
            f"Duration:  {duration}s capture window\n"
            f"Output:    {session_dir}\n"
            f"Whisper:   {whisper_model}\n"
            f"Summary:   {'skip' if skip_summary else summary_style}",
            border_style="cyan",
        )
    )

    # ── Step 1: Capture audio ──────────────────────────────
    console.print("\n[bold cyan]Step 1/4: Capturing audio...[/bold cyan]\n")

    from pocket_libre.capture import capture_audio

    await capture_audio(
        address=address,
        output_path=str(audio_path),
        duration=duration,
        char_uuid=None,  # auto-detect
    )

    if not audio_path.exists() or audio_path.stat().st_size < 100:
        console.print("[red]No audio captured. Aborting.[/red]")
        return None

    # Check for MP3 and trim
    raw = audio_path.read_bytes()
    if b"\xff\xf3" in raw[:256]:
        first_frame = raw.find(b"\xff\xf3")
        if first_frame > 0:
            audio_path.write_bytes(raw[first_frame:])
        console.print(f"[green]Audio saved: {audio_path}[/green]")
    else:
        console.print("[yellow]Warning: Audio may not be MP3 format.[/yellow]")

    # ── Step 2: Transcribe ─────────────────────────────────
    console.print("\n[bold cyan]Step 2/4: Transcribing...[/bold cyan]\n")

    try:
        import whisper
    except ImportError:
        console.print("[red]Whisper not installed. Skipping transcription.[/red]")
        return str(session_dir)

    model = whisper.load_model(whisper_model)
    result = model.transcribe(str(audio_path), verbose=False)

    full_text = result["text"].strip()
    segments = result.get("segments", [])

    console.print(f"[green]Transcribed: {len(segments)} segments[/green]")

    # ── Step 3: Speaker diarization ────────────────────────
    console.print("\n[bold cyan]Step 3/4: Identifying speakers...[/bold cyan]\n")

    from pocket_libre.diarize import (
        diarize_pyannote,
        diarize_simple,
        merge_transcript_with_speakers,
    )

    speaker_segments = []

    if hf_token:
        try:
            speaker_segments = diarize_pyannote(str(audio_path), hf_token)
        except Exception as e:
            console.print(f"[yellow]Diarization failed: {e}[/yellow]")
            console.print("[dim]Falling back to simple mode.[/dim]")

    if not speaker_segments:
        speaker_segments = diarize_simple(segments)
        if not hf_token:
            console.print(
                "[dim]No HuggingFace token provided. "
                "Using basic segmentation (no speaker ID).[/dim]"
            )
            console.print(
                "[dim]For speaker identification, set HUGGINGFACE_TOKEN "
                "and run: pip install pyannote.audio[/dim]"
            )

    labeled_segments = merge_transcript_with_speakers(segments, speaker_segments)

    # Build transcript text
    from pocket_libre.summarize import format_transcript_for_summary, format_time

    transcript_text = format_transcript_for_summary(labeled_segments)

    # Save transcript
    transcript_path.write_text(transcript_text, encoding="utf-8")
    console.print(f"[green]Transcript saved: {transcript_path}[/green]")

    # ── Step 4: Summarize ──────────────────────────────────
    if skip_summary:
        console.print("\n[dim]Skipping summary (--skip-summary).[/dim]")
    else:
        console.print("\n[bold cyan]Step 4/4: Summarizing...[/bold cyan]\n")

        api_key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY")

        if not api_key:
            console.print(
                "[yellow]No Anthropic API key found.[/yellow] "
                "Set ANTHROPIC_API_KEY env var or pass --anthropic-key.\n"
                "[dim]Skipping summary. You can still read the transcript.[/dim]"
            )
        else:
            from pocket_libre.summarize import summarize_transcript

            summary = summarize_transcript(
                transcript_text=transcript_text,
                api_key=api_key,
                style=summary_style,
                custom_prompt=custom_prompt,
            )

            if summary:
                # Build full output document
                header = f"# Recording: {timestamp}\n\n"
                full_doc = header + summary + "\n\n---\n\n## Full Transcript\n\n" + transcript_text
                summary_path.write_text(full_doc, encoding="utf-8")
                console.print(f"[green]Summary saved: {summary_path}[/green]")

    # ── Done ───────────────────────────────────────────────
    console.print(
        Panel(
            f"[bold green]Sync complete![/bold green]\n\n"
            f"Audio:      {audio_path}\n"
            f"Transcript: {transcript_path}\n"
            f"Summary:    {summary_path if summary_path.exists() else 'skipped'}\n\n"
            f"[dim]Play: open {audio_path}\n"
            f"Read: cat {summary_path}[/dim]",
            border_style="green",
        )
    )

    return str(session_dir)
