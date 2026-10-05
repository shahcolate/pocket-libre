"""Putting names to voices.

Diarization labels are per recording. `SPEAKER_01` in one file and `SPEAKER_01`
in the next are not the same person, so a name map keyed on those labels would
be worse than no names at all: it would confidently attribute words to whoever
happened to be labelled first last time.

What makes names possible is the speaker embedding the local backend can dump:
one 256-dimension WeSpeaker vector per speaker, which *is* comparable across
recordings. Enroll a voice once and every later recording can be matched
against it with a cosine similarity, in plain Python.

Nothing here is automatic guesswork: a voice gets a name because someone
enrolled it, and a match below the threshold stays `SPEAKER_01`.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

LIBRARY_FILENAME = "speakers.json"

# Cosine similarity above which two embeddings are taken to be the same person.
# A starting point, not a law: it depends on the microphone and the voices, so
# `pocket-libre speakers test` prints the real numbers to calibrate against.
DEFAULT_THRESHOLD = 0.65

# The local backend writes one file per speaker next to the audio, named
# `<audio name>_diarize_<model>_<LABEL>.txt`, holding one float per line.
_DUMP_PATTERN = re.compile(r"_diarize_.*?_(?P<label>[A-Za-z0-9_]+)\.txt$")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two embeddings, 0.0 when either has no magnitude."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def read_embedding_dumps(audio_path: str | Path, *, cleanup: bool = True) -> dict:
    """Collect the per-speaker embeddings the backend dumped beside the audio.

    They are written into the library directory, so they are read and then
    removed: the useful copy is the one stored with the recording.
    """
    audio = Path(audio_path)
    found: dict[str, list[float]] = {}

    for candidate in audio.parent.glob(f"{audio.name}_diarize_*.txt"):
        match = _DUMP_PATTERN.search(candidate.name)
        if not match:
            continue
        try:
            numbers = [
                float(line) for line in
                candidate.read_text(encoding="utf-8").split()
                if line.strip()
            ]
        except (OSError, ValueError):
            continue
        if numbers:
            found[match.group("label")] = numbers
        if cleanup:
            try:
                candidate.unlink()
            except OSError:
                pass

    return found


def save_recording_voices(path: str | Path, embeddings: dict) -> None:
    """Keep a recording's embeddings next to it, so a voice can be enrolled later."""
    if not embeddings:
        return
    Path(path).write_text(
        json.dumps({"version": 1, "embeddings": embeddings}, indent=1),
        encoding="utf-8",
    )


def load_recording_voices(path: str | Path) -> dict:
    """The embeddings stored with one recording, or an empty mapping."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        str(label): [float(x) for x in vector]
        for label, vector in raw.items()
        if isinstance(vector, list) and vector
    }


@dataclass
class VoiceLibrary:
    """The enrolled voices for one library, stored as `speakers.json`.

    Several samples per person are kept rather than averaged: a voice recorded
    across different rooms matches better against its own range than against a
    single blurred centroid.
    """

    path: Path | None = None
    threshold: float = DEFAULT_THRESHOLD
    voices: dict[str, list[list[float]]] = field(default_factory=dict)

    @classmethod
    def load(cls, library_dir: str | Path) -> VoiceLibrary:
        path = Path(library_dir) / LIBRARY_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path)

        voices: dict[str, list[list[float]]] = {}
        for name, samples in (payload.get("voices") or {}).items():
            if not isinstance(samples, list):
                continue
            vectors = [
                [float(x) for x in sample]
                for sample in samples
                if isinstance(sample, list) and sample
            ]
            if vectors:
                voices[str(name)] = vectors

        try:
            threshold = float(payload.get("threshold", DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD

        return cls(path=path, threshold=threshold, voices=voices)

    def save(self) -> None:
        if self.path is None:
            raise ValueError("This voice library has no path to save to.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({
                "version": 1,
                "threshold": self.threshold,
                "updated": date.today().isoformat(),
                "voices": self.voices,
            }, indent=1),
            encoding="utf-8",
        )

    @property
    def names(self) -> list[str]:
        return sorted(self.voices)

    def enroll(self, name: str, vector: list[float]) -> None:
        """Add one sample for `name`."""
        clean = str(name).strip()
        if not clean:
            raise ValueError("A voice needs a name.")
        if not vector:
            raise ValueError(f"No embedding to enroll for {clean!r}.")
        self.voices.setdefault(clean, []).append([float(x) for x in vector])

    def forget(self, name: str) -> bool:
        return self.voices.pop(str(name).strip(), None) is not None

    def score(self, vector: list[float]) -> list[tuple[str, float]]:
        """Every enrolled name with its best similarity to `vector`, best first."""
        scored = [
            (name, max(cosine_similarity(vector, sample) for sample in samples))
            for name, samples in self.voices.items()
        ]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)

    def best_match(self, vector: list[float]) -> tuple[str | None, float]:
        scored = self.score(vector)
        if not scored:
            return None, 0.0
        return scored[0]


def identify(embeddings: dict, library: VoiceLibrary,
             threshold: float | None = None) -> dict:
    """Map diarization labels to enrolled names.

    One name per label and one label per name: the strongest pairs are taken
    first, so two voices in one recording can never both come back as the same
    person. Anything below the threshold is left unnamed.
    """
    if not embeddings or not library.voices:
        return {}

    limit = library.threshold if threshold is None else threshold

    candidates = sorted(
        (
            (score, label, name)
            for label, vector in embeddings.items()
            for name, score in library.score(vector)
            if score >= limit
        ),
        reverse=True,
    )

    names: dict[str, str] = {}
    claimed: set[str] = set()
    for _score, label, name in candidates:
        if label in names or name in claimed:
            continue
        names[label] = name
        claimed.add(name)
    return names


def apply_names(segments: list[dict], names: dict) -> list[dict]:
    """Rewrite the `speaker` of each segment that has a matched name."""
    if not names:
        return segments
    return [
        {**segment, "speaker": names.get(segment.get("speaker"), segment.get("speaker"))}
        for segment in segments
    ]


def dominant_speaker(segments: list[dict]) -> str | None:
    """Whoever holds the floor longest. The recorder's owner, usually.

    Only ever offered as a suggestion for enrollment: a recording where the
    other person does most of the talking would make it wrong, and a wrong name
    on a transcript is worse than no name.
    """
    totals: dict[str, float] = {}
    for segment in segments:
        speaker = segment.get("speaker")
        if not speaker:
            continue
        duration = float(segment.get("end", 0.0)) - float(segment.get("start", 0.0))
        totals[speaker] = totals.get(speaker, 0.0) + max(duration, 0.0)
    if not totals:
        return None
    return max(totals, key=lambda key: totals[key])
