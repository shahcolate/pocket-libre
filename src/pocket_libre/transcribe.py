"""Local transcription using OpenAI Whisper. Everything stays on your machine."""

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel


console = Console()


def transcribe_audio(
    input_path: str,
    model_name: str = "base.en",
    output_path: str | None = None,
    output_format: str = "text",
):
    """Transcribe an audio file using Whisper locally.

    No network calls. No API keys. The model runs entirely on your machine.
    """
    input_file = Path(input_path)
    if not input_file.exists():
        console.print(f"[red]File not found: {input_path}[/red]")
        return

    console.print(f"[bold]Loading Whisper model: {model_name}[/bold]")
    console.print("[dim]First run will download the model (one-time).[/dim]\n")

    try:
        import whisper
    except ImportError:
        console.print(
            "[red]Whisper not installed.[/red] "
            "Run: [bold]pip install openai-whisper[/bold]"
        )
        return

    model = whisper.load_model(model_name)

    console.print(f"[cyan]Transcribing: {input_path}[/cyan]\n")

    result = model.transcribe(str(input_file), verbose=False)

    text = result["text"].strip()
    segments = result.get("segments", [])
    language = result.get("language", "unknown")

    if output_format == "text":
        formatted = text
    elif output_format == "json":
        formatted = json.dumps(
            {
                "text": text,
                "language": language,
                "segments": [
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg["text"].strip(),
                    }
                    for seg in segments
                ],
            },
            indent=2,
        )
    elif output_format == "srt":
        lines = []
        for i, seg in enumerate(segments, 1):
            start = format_timestamp_srt(seg["start"])
            end = format_timestamp_srt(seg["end"])
            lines.append(f"{i}")
            lines.append(f"{start} --> {end}")
            lines.append(seg["text"].strip())
            lines.append("")
        formatted = "\n".join(lines)
    else:
        formatted = text

    if output_path:
        Path(output_path).write_text(formatted, encoding="utf-8")
        console.print(f"[bold green]Transcript saved to: {output_path}[/bold green]")
    else:
        console.print(Panel(formatted, title="Transcript", border_style="green"))

    console.print(f"\n[dim]Language: {language} | Segments: {len(segments)}[/dim]")


def format_timestamp_srt(seconds: float) -> str:
    """Format seconds as SRT timestamp (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
