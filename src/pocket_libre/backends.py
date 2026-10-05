"""Transcription backends.

`openai-whisper` runs in-process and is the default, unchanged.

`faster-whisper-xxl` drives a local standalone build of faster-whisper. It is
worth the extra moving part: it transcribes in any of Whisper's languages with
real auto-detection, labels speakers with a bundled pyannote, and runs on the
GPU, without installing torch into this environment and without a HuggingFace
token. On a laptop GPU it runs an order of magnitude faster than realtime.

One quirk of that build shapes the code below: its JSON writer does **not**
carry speaker labels. Diarization only reaches the SRT and TXT writers, as a
`[SPEAKER_01]: ` prefix on each cue. So both formats are requested and the SRT
is what the segments are built from, with the JSON consulted only for the
detected language.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BACKEND = "openai-whisper"
LOCAL_BACKEND = "faster-whisper-xxl"
BACKENDS = (DEFAULT_BACKEND, LOCAL_BACKEND, "none")

# Default for the standalone build: the accuracy of large-v3 at a fraction of
# the cost. Overridden per profile with `whisper_model`.
DEFAULT_LOCAL_MODEL = "large-v3-turbo"
DEFAULT_DIARIZE_MODEL = "pyannote_v3.1"

# Whisper's own language detection reads only the first window, which is a coin
# flip on a short recording and produces phonetic nonsense when it loses. The
# standalone build can sample several windows instead, so it does.
DEFAULT_DETECTION_SEGMENTS = 4

_EXE_NAME = "faster-whisper-xxl.exe" if os.name == "nt" else "faster-whisper-xxl"

# Where the standalone build usually ends up when installed by hand.
_KNOWN_DIRS = (
    Path.home() / "Tools" / "WhisperKit" / "Faster-Whisper-XXL",
    Path.home() / "Tools" / "Faster-Whisper-XXL",
    Path.home() / "Faster-Whisper-XXL",
    Path("C:/Faster-Whisper-XXL"),
)

_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_SRT_ARROW = re.compile(r"^(?P<start>[\d:,.]+)\s*-->\s*(?P<end>[\d:,.]+)")
_SPEAKER_PREFIX = re.compile(r"^\[(?P<speaker>[^\]]{1,64})\]:\s*")


class TranscriptionError(RuntimeError):
    """A backend could not produce a transcript."""


@dataclass
class Segment:
    """One stretch of speech: when it ran, what was said, and by whom."""

    start: float
    end: float
    text: str
    speaker: str | None = None

    def as_dict(self) -> dict:
        out = {"start": self.start, "end": self.end, "text": self.text}
        if self.speaker:
            out["speaker"] = self.speaker
        return out


@dataclass
class Transcription:
    """What every backend returns, whatever it runs underneath."""

    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    backend: str = ""
    diarized: bool = False
    # One embedding per diarized speaker, when the backend can produce them.
    # This is what makes a voice recognisable in a later recording.
    embeddings: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text).strip()

    @property
    def speakers(self) -> list[str]:
        """Distinct speaker labels, in the order they first speak."""
        seen: list[str] = []
        for segment in self.segments:
            if segment.speaker and segment.speaker not in seen:
                seen.append(segment.speaker)
        return seen

    def as_dicts(self) -> list[dict]:
        """Segments in the plain-dict shape the rest of the pipeline consumes."""
        return [s.as_dict() for s in self.segments]


# ── Locating the standalone build ───────────────


def find_faster_whisper(explicit: str | os.PathLike | None = None) -> Path | None:
    """Locate the faster-whisper-xxl executable, or None.

    Order: an explicit path (file or containing directory), the
    FASTER_WHISPER_XXL environment variable, PATH, then the usual install
    directories.
    """
    candidates: list[Path] = []

    for raw in (explicit, os.environ.get("FASTER_WHISPER_XXL")):
        if not raw:
            continue
        path = Path(os.path.expanduser(str(raw)))
        candidates.append(path if path.suffix else path / _EXE_NAME)
        candidates.append(path / _EXE_NAME)

    found = shutil.which(_EXE_NAME) or shutil.which("faster-whisper-xxl")
    if found:
        candidates.append(Path(found))

    candidates.extend(directory / _EXE_NAME for directory in _KNOWN_DIRS)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ── Parsing what the standalone build writes ────


def _srt_seconds(stamp: str) -> float | None:
    match = _SRT_TIME.search(stamp)
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            + int(millis.ljust(3, "0")) / 1000)


def parse_srt(text: str) -> list[Segment]:
    """Read SRT cues into segments, lifting any `[SPEAKER_NN]:` prefix out.

    The speaker lives in the cue text rather than in a field of its own, which
    is the only place this build exposes diarization.
    """
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [line for line in block.strip().split("\n") if line.strip()]
        if not lines:
            continue
        # An optional cue number, then the timing line.
        timing_index = next(
            (i for i, line in enumerate(lines) if _SRT_ARROW.match(line.strip())), None
        )
        if timing_index is None:
            continue
        arrow = _SRT_ARROW.match(lines[timing_index].strip())
        start = _srt_seconds(arrow.group("start"))
        end = _srt_seconds(arrow.group("end"))
        if start is None or end is None:
            continue

        body = " ".join(line.strip() for line in lines[timing_index + 1:]).strip()
        speaker = None
        prefix = _SPEAKER_PREFIX.match(body)
        if prefix:
            speaker = prefix.group("speaker").strip()
            body = body[prefix.end():].strip()
        if not body:
            continue
        segments.append(Segment(start=start, end=end, text=body, speaker=speaker))
    return segments


def _language_from_json(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    language = payload.get("language") if isinstance(payload, dict) else None
    return str(language) if language else None


def _segments_from_json(path: Path) -> list[Segment]:
    """Fallback when no SRT was written. Carries no speaker labels."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    segments = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        segments.append(Segment(
            start=float(item.get("start", 0.0)),
            end=float(item.get("end", 0.0)),
            text=text,
            speaker=item.get("speaker"),
        ))
    return segments


# ── The standalone build ────────────────────────


def build_faster_whisper_command(
    exe: Path,
    audio_path: Path,
    output_dir: Path,
    *,
    model: str = DEFAULT_LOCAL_MODEL,
    device: str = "cuda",
    language: str | None = None,
    diarize: str | None = DEFAULT_DIARIZE_MODEL,
    max_speakers: int | None = None,
    detection_segments: int = DEFAULT_DETECTION_SEGMENTS,
    vad: bool = True,
    embeddings: bool = True,
) -> list[str]:
    """The exact argv used, kept separate so it can be asserted on in tests."""
    command = [
        str(exe), str(audio_path),
        "--model", model,
        "--device", device,
        "--output_dir", str(output_dir),
        # Both: the SRT carries the speakers, the JSON carries the language.
        "--output_format", "srt", "json",
        "--beep_off",
    ]
    if vad:
        command += ["--vad_filter", "true"]
    if language and language.lower() not in ("auto", ""):
        command += ["--language", language]
    elif detection_segments > 1:
        command += ["--language_detection_segments", str(detection_segments)]
    if diarize:
        command += ["--diarize", diarize]
        if max_speakers and max_speakers > 0:
            command += ["--max_speakers", str(max_speakers)]
        if embeddings:
            # One vector per speaker, dumped beside the audio. Costs nothing
            # extra on a run that is already diarizing, and it is the only way
            # to recognise the same voice in a later recording.
            command += ["--return_embeddings"]
    return command


def transcribe_faster_whisper(
    audio_path: str | os.PathLike,
    *,
    exe: str | os.PathLike | None = None,
    model: str = DEFAULT_LOCAL_MODEL,
    device: str = "cuda",
    language: str | None = None,
    diarize: str | None = DEFAULT_DIARIZE_MODEL,
    max_speakers: int | None = None,
    detection_segments: int = DEFAULT_DETECTION_SEGMENTS,
    embeddings: bool = True,
    timeout: float | None = None,
    runner=subprocess.run,
) -> Transcription:
    """Transcribe with the local standalone build.

    Speaker labels come back as `SPEAKER_00`-style names; turning those into
    real names is a separate step, and the embeddings are what make it possible.
    """
    audio = Path(audio_path)
    if not audio.is_file():
        raise TranscriptionError(f"Audio file not found: {audio}")

    binary = find_faster_whisper(exe)
    if binary is None:
        raise TranscriptionError(
            "faster-whisper-xxl was not found. Point at it with "
            "'pocket-libre config --set defaults.faster_whisper_path=<path>', "
            "set FASTER_WHISPER_XXL, or switch backend with "
            "'--set defaults.transcribe_backend=openai-whisper'."
        )

    with tempfile.TemporaryDirectory(prefix="pocket-libre-fw-") as tmp:
        out_dir = Path(tmp)
        command = build_faster_whisper_command(
            binary, audio, out_dir,
            model=model, device=device, language=language, diarize=diarize,
            max_speakers=max_speakers, detection_segments=detection_segments,
            embeddings=embeddings,
        )

        try:
            result = runner(command, capture_output=True, text=True,
                            timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise TranscriptionError(
                f"faster-whisper-xxl timed out after {timeout:.0f}s."
            ) from e
        except OSError as e:
            raise TranscriptionError(f"Could not run {binary}: {e}") from e

        srt_path = out_dir / f"{audio.stem}.srt"
        json_path = out_dir / f"{audio.stem}.json"

        # This build sometimes crashes during its own cleanup, after the output
        # is already written (a stack-buffer-overrun exit). The files on disk
        # are the real result, so they decide, not the exit code.
        if not srt_path.exists() and not json_path.exists():
            tail = (getattr(result, "stderr", "") or
                    getattr(result, "stdout", "") or "").strip()[-800:]
            code = getattr(result, "returncode", "unknown")
            raise TranscriptionError(
                f"faster-whisper-xxl wrote no transcript (exit {code}). {tail}"
            )

        language_detected = _language_from_json(json_path) if json_path.exists() else None
        if srt_path.exists():
            segments = parse_srt(srt_path.read_text(encoding="utf-8", errors="replace"))
        else:
            segments = _segments_from_json(json_path)

    # Dumped beside the audio, not into the output directory, so they are
    # collected (and cleared) after the run rather than from the temp folder.
    voices = {}
    if embeddings and diarize:
        from pocket_libre.speakers import read_embedding_dumps
        voices = read_embedding_dumps(audio)

    return Transcription(
        segments=segments,
        language=language_detected or (language if language != "auto" else None),
        backend=LOCAL_BACKEND,
        diarized=bool(diarize) and any(s.speaker for s in segments),
        embeddings=voices,
    )


# ── Whisper, in-process ─────────────────────────


def transcribe_openai_whisper(
    audio_path: str | os.PathLike,
    *,
    model: str = "base.en",
    language: str | None = None,
) -> Transcription:
    """Transcribe with openai-whisper in this process. No speaker labels."""
    try:
        import whisper
    except ImportError as e:
        raise TranscriptionError(
            "openai-whisper is not installed. Run 'pip install openai-whisper', "
            "or switch backend with "
            "'pocket-libre config --set defaults.transcribe_backend=faster-whisper-xxl'."
        ) from e

    loaded = whisper.load_model(model)
    options = {}
    if language and language.lower() not in ("auto", ""):
        options["language"] = language
    result = loaded.transcribe(str(audio_path), verbose=False, **options)

    segments = [
        Segment(start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                text=str(s.get("text", "")).strip())
        for s in result.get("segments", [])
        if str(s.get("text", "")).strip()
    ]
    return Transcription(
        segments=segments,
        language=result.get("language"),
        backend=DEFAULT_BACKEND,
        diarized=False,
    )


# ── Dispatch ────────────────────────────────────


def transcribe(
    audio_path: str | os.PathLike,
    *,
    backend: str = DEFAULT_BACKEND,
    model: str | None = None,
    language: str | None = None,
    device: str = "cuda",
    diarize: str | None = DEFAULT_DIARIZE_MODEL,
    max_speakers: int | None = None,
    exe: str | os.PathLike | None = None,
    detection_segments: int = DEFAULT_DETECTION_SEGMENTS,
    timeout: float | None = None,
) -> Transcription:
    """Transcribe `audio_path` with the named backend."""
    name = (backend or DEFAULT_BACKEND).strip().lower()

    if name == "none":
        return Transcription(backend="none")

    if name == LOCAL_BACKEND:
        return transcribe_faster_whisper(
            audio_path, exe=exe, model=model or DEFAULT_LOCAL_MODEL, device=device,
            language=language, diarize=diarize, max_speakers=max_speakers,
            detection_segments=detection_segments, timeout=timeout,
        )

    if name == DEFAULT_BACKEND:
        return transcribe_openai_whisper(
            audio_path, model=model or "base.en", language=language,
        )

    raise TranscriptionError(
        f"Unknown transcription backend {backend!r}. Choose one of: "
        + ", ".join(BACKENDS)
    )


def options_from_config(config: dict, whisper_model: str | None = None) -> dict:
    """Backend options resolved from a config. Pass the profile-folded one."""
    from pocket_libre.config import get

    backend = str(get(config, "defaults", "transcribe_backend",
                      default=DEFAULT_BACKEND)).strip().lower()
    model = whisper_model or get(config, "defaults", "whisper_model")
    if backend == LOCAL_BACKEND and (not model or str(model).endswith(".en")):
        # An English-only model would silently defeat a multilingual setup, and
        # the standalone build names its models differently anyway.
        model = DEFAULT_LOCAL_MODEL

    try:
        max_speakers = int(get(config, "defaults", "max_speakers", default=0) or 0)
    except (TypeError, ValueError):
        max_speakers = 0

    return {
        "backend": backend,
        "model": model,
        "language": get(config, "defaults", "language", default="auto"),
        "device": get(config, "defaults", "device", default="cuda"),
        "max_speakers": max_speakers or None,
        "exe": get(config, "defaults", "faster_whisper_path", default="") or None,
    }


def transcribe_and_label(
    audio_path: str | os.PathLike,
    *,
    hf_token: str | None = None,
    anthropic_key: str | None = None,
    library_dir: str | os.PathLike | None = None,
    voices_path: str | os.PathLike | None = None,
    **options,
) -> tuple[list[dict], Transcription]:
    """Transcribe, then make sure every segment carries a speaker.

    The one place transcription and speaker attribution are decided, so every
    caller - sync, watch, the web UI, `process` - gets the same answer.

    A backend that labels speakers itself is believed; otherwise the existing
    pyannote, then Claude, then heuristic chain fills them in, unchanged.

    When `library_dir` holds enrolled voices, matching speakers get their real
    names. `voices_path` is where this recording's own embeddings are kept, so
    a voice can still be enrolled from it afterwards.
    """
    result = transcribe(audio_path, **options)
    if not result.segments:
        return [], result

    if result.embeddings:
        from pocket_libre.speakers import save_recording_voices

        if voices_path:
            save_recording_voices(voices_path, result.embeddings)

    if result.diarized:
        labeled = [
            {"start": s.start, "end": s.end,
             "speaker": s.speaker or "Speaker", "text": s.text}
            for s in result.segments
        ]
        if library_dir and result.embeddings:
            from pocket_libre.speakers import VoiceLibrary, apply_names, identify

            library = VoiceLibrary.load(library_dir)
            labeled = apply_names(labeled, identify(result.embeddings, library))
        return labeled, result

    from pocket_libre.diarize import diarize_auto, merge_transcript_with_speakers

    plain = [{"start": s.start, "end": s.end, "text": s.text} for s in result.segments]
    try:
        speakers = diarize_auto(
            plain, audio_path=str(audio_path),
            hf_token=hf_token, anthropic_key=anthropic_key,
        )
        labeled = merge_transcript_with_speakers(plain, speakers)
    except Exception:
        labeled = [
            {"start": s["start"], "end": s["end"], "speaker": "Speaker", "text": s["text"]}
            for s in plain
        ]
    return labeled, result
