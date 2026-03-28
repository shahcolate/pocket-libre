"""Audio format conversion utilities."""

import struct
import wave
from pathlib import Path

from rich.console import Console


console = Console()


def raw_to_wav(
    input_path: str,
    output_path: str,
    sample_rate: int = 16000,
    bit_depth: int = 16,
    channels: int = 1,
):
    """Convert raw PCM audio to WAV format.

    Default params assume 16-bit signed PCM at 16kHz mono,
    which is the most common format for BLE voice recorders.
    Adjust if your device uses different settings.
    """
    raw_data = Path(input_path).read_bytes()

    if not raw_data:
        console.print("[red]Input file is empty.[/red]")
        return

    sample_width = bit_depth // 8
    num_samples = len(raw_data) // (sample_width * channels)
    duration = num_samples / sample_rate

    with wave.open(output_path, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(raw_data)

    console.print(f"[bold green]Converted to WAV:[/bold green] {output_path}")
    console.print(
        f"[dim]{sample_rate}Hz, {bit_depth}-bit, "
        f"{'mono' if channels == 1 else 'stereo'}, "
        f"{duration:.1f}s, {len(raw_data):,} bytes[/dim]"
    )
    console.print(
        f"\nNext: transcribe with "
        f"[bold]pocket-libre transcribe --input {output_path}[/bold]"
    )

    if duration < 0.5:
        console.print(
            "\n[yellow]Warning: Very short audio. "
            "The sample rate or bit depth might be wrong. "
            "Try different values with --sample-rate and --bit-depth.[/yellow]"
        )
