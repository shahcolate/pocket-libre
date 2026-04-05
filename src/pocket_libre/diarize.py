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


def diarize_llm(whisper_segments: list[dict], api_key: str) -> list[SpeakerSegment]:
    """Use Claude Haiku to identify speakers from transcript content.

    Analyzes the text of each segment to determine speaker changes based
    on context, tone, and conversational flow. ~$0.001 per call.
    """
    try:
        import anthropic
    except ImportError:
        console.print("[yellow]anthropic not installed, falling back to simple diarization[/yellow]")
        return diarize_simple(whisper_segments)

    # Build a numbered transcript for Claude to label
    lines = []
    for i, seg in enumerate(whisper_segments):
        t = int(seg["start"])
        lines.append(f"{i}: [{t//60:02d}:{t%60:02d}] {seg['text'].strip()}")
    numbered_transcript = "\n".join(lines)

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        system=(
            "You identify distinct speakers in transcripts. Analyze speaking patterns, "
            "context, and conversational flow to determine when the speaker changes. "
            "Return ONLY a JSON array mapping line numbers to speaker labels. "
            "Use descriptive labels when possible (e.g. 'Interviewer', 'Host', 'Manager') "
            "or 'Speaker 1', 'Speaker 2' etc. if roles are unclear."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Identify the speakers in this transcript. Each line is numbered.\n"
                f"Return a JSON array like: [{{\"line\": 0, \"speaker\": \"Speaker 1\"}}, ...]\n"
                f"Only include lines where the speaker CHANGES (first line always included).\n\n"
                f"{numbered_transcript}"
            ),
        }],
    )

    cost = (message.usage.input_tokens * 0.25 + message.usage.output_tokens * 1.25) / 1_000_000
    console.print(f"[dim]Speaker ID: {message.usage.input_tokens}+{message.usage.output_tokens} tokens (~${cost:.4f})[/dim]")

    # Parse response
    import json
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))

    try:
        changes = json.loads(text)
    except json.JSONDecodeError:
        console.print("[yellow]Could not parse speaker labels, falling back to simple[/yellow]")
        return diarize_simple(whisper_segments)

    # Build a line->speaker mapping
    speaker_map = {}
    current_speaker = "Speaker 1"
    for change in sorted(changes, key=lambda c: c.get("line", 0)):
        line_num = change.get("line", 0)
        current_speaker = change.get("speaker", current_speaker)
        speaker_map[line_num] = current_speaker

    # Assign speakers to all segments
    segments = []
    active_speaker = "Speaker 1"
    unique_speakers = set()
    for i, seg in enumerate(whisper_segments):
        if i in speaker_map:
            active_speaker = speaker_map[i]
        unique_speakers.add(active_speaker)
        segments.append(SpeakerSegment(
            start=seg["start"],
            end=seg["end"],
            speaker=active_speaker,
        ))

    console.print(f"[green]Identified {len(unique_speakers)} speaker(s) via AI[/green]")
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


def diarize_auto(
    whisper_segments: list[dict],
    audio_path: str | None = None,
    hf_token: str | None = None,
    anthropic_key: str | None = None,
) -> list[SpeakerSegment]:
    """Auto-select best diarization method.

    Priority: pyannote (if installed + token) > Claude Haiku (if key) > simple fallback.
    """
    # Try pyannote first
    if hf_token and audio_path:
        try:
            segments = diarize_pyannote(audio_path, hf_token)
            if segments:
                return segments
        except Exception as e:
            console.print(f"[dim]pyannote failed: {e}[/dim]")

    # Try LLM diarization
    if anthropic_key and whisper_segments:
        try:
            segments = diarize_llm(whisper_segments, anthropic_key)
            if segments:
                return segments
        except Exception as e:
            console.print(f"[dim]LLM diarization failed: {e}[/dim]")

    # Simple fallback
    return diarize_simple(whisper_segments)


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
