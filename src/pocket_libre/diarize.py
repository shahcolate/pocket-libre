"""Speaker diarization. Identifies who is speaking at each point in the audio.

Uses pyannote.audio if available (best quality), falls back to a simple
energy-based heuristic for basic speaker separation.

pyannote.audio requires:
  pip install pyannote.audio
  A HuggingFace token (free): https://huggingface.co/settings/tokens
  Accept terms at: https://huggingface.co/pyannote/speaker-diarization-3.1
"""

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console


console = Console()


@dataclass
class SpeakerSegment:
    """A labeled segment of speech."""
    start: float
    end: float
    speaker: str


def diarize_pyannote(audio_path: str, hf_token: str) -> list[SpeakerSegment]:
    """Diarize using pyannote.audio (high quality, needs HuggingFace token)."""
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        console.print(
            "[red]pyannote.audio not installed.[/red] "
            "Run: [bold]pip install pyannote.audio[/bold]"
        )
        return []

    console.print("[dim]Loading pyannote speaker diarization model...[/dim]")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )

    console.print("[dim]Running diarization...[/dim]")
    diarization = pipeline(audio_path)

    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append(SpeakerSegment(
            start=turn.start,
            end=turn.end,
            speaker=speaker,
        ))

    # Rename speakers to friendly names (Speaker 1, Speaker 2, etc.)
    unique_speakers = list(dict.fromkeys(seg.speaker for seg in segments))
    name_map = {spk: f"Speaker {i+1}" for i, spk in enumerate(unique_speakers)}

    for seg in segments:
        seg.speaker = name_map[seg.speaker]

    console.print(f"[green]Found {len(unique_speakers)} speaker(s)[/green]")
    return segments


def diarize_simple(whisper_segments: list[dict]) -> list[SpeakerSegment]:
    """Simple fallback: treat each Whisper segment as a single speaker turn.

    This doesn't actually identify speakers, but provides the segment
    structure so the transcript is still time-stamped. Speaker labels
    are all "Speaker" since we can't differentiate without pyannote.
    """
    return [
        SpeakerSegment(
            start=seg["start"],
            end=seg["end"],
            speaker="Speaker",
        )
        for seg in whisper_segments
    ]


def merge_transcript_with_speakers(
    whisper_segments: list[dict],
    speaker_segments: list[SpeakerSegment],
) -> list[dict]:
    """Merge Whisper transcript segments with speaker labels.

    For each Whisper segment, find the speaker who was talking at the
    midpoint of that segment.
    """
    if not speaker_segments:
        return [
            {
                "start": seg["start"],
                "end": seg["end"],
                "speaker": "Speaker",
                "text": seg["text"].strip(),
            }
            for seg in whisper_segments
        ]

    result = []
    for seg in whisper_segments:
        midpoint = (seg["start"] + seg["end"]) / 2

        # Find which speaker was active at the midpoint
        speaker = "Unknown"
        for spk_seg in speaker_segments:
            if spk_seg.start <= midpoint <= spk_seg.end:
                speaker = spk_seg.speaker
                break

        result.append({
            "start": seg["start"],
            "end": seg["end"],
            "speaker": speaker,
            "text": seg["text"].strip(),
        })

    # Merge consecutive segments from the same speaker
    merged = []
    for seg in result:
        if merged and merged[-1]["speaker"] == seg["speaker"]:
            merged[-1]["end"] = seg["end"]
            merged[-1]["text"] += " " + seg["text"]
        else:
            merged.append(dict(seg))

    return merged
