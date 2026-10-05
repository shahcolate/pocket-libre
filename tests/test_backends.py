"""Transcription backends: SRT parsing, argv, and the standalone build's quirks."""

import subprocess
from pathlib import Path

import pytest

from pocket_libre import backends

# Real output from faster-whisper-xxl 1.8 with --diarize pyannote_v3.1.
SAMPLE_SRT = """1
00:00:00,000 --> 00:00:05,220
[SPEAKER_01]: I saw your answer and okay, I won't know more.

2
00:00:05,380 --> 00:00:06,180
[SPEAKER_01]: I contacted her.

3
00:00:06,340 --> 00:00:14,180
[SPEAKER_02]: Actually, she did it so well that you actually can't...
"""


# ── Parsing ─────────────────────────────────────


def test_srt_segments_carry_speakers():
    segments = backends.parse_srt(SAMPLE_SRT)
    assert len(segments) == 3
    assert segments[0].speaker == "SPEAKER_01"
    assert segments[2].speaker == "SPEAKER_02"
    assert segments[0].text == "I saw your answer and okay, I won't know more."
    assert segments[0].start == 0.0
    assert segments[0].end == pytest.approx(5.22)


def test_srt_without_speakers_still_parses():
    plain = "1\n00:00:01,500 --> 00:00:02,000\nJust words.\n"
    segments = backends.parse_srt(plain)
    assert len(segments) == 1
    assert segments[0].speaker is None
    assert segments[0].start == pytest.approx(1.5)


def test_srt_handles_crlf_and_multiline_cues():
    text = "1\r\n00:00:00,000 --> 00:00:02,000\r\n[A]: one\r\ntwo\r\n"
    segments = backends.parse_srt(text)
    assert len(segments) == 1
    assert segments[0].text == "one two"
    assert segments[0].speaker == "A"


def test_srt_ignores_blocks_without_timing():
    assert backends.parse_srt("WEBVTT\n\ngarbage\n") == []


def test_speakers_are_listed_in_first_spoken_order():
    result = backends.Transcription(segments=backends.parse_srt(SAMPLE_SRT))
    assert result.speakers == ["SPEAKER_01", "SPEAKER_02"]


# ── Command building ────────────────────────────


def _command(**kwargs):
    return backends.build_faster_whisper_command(
        Path("fw.exe"), Path("a.mp3"), Path("out"), **kwargs,
    )


def test_command_asks_for_both_formats():
    """The SRT carries the speakers; the JSON carries the detected language."""
    command = _command()
    assert "--output_format" in command
    formats = command[command.index("--output_format") + 1:][:2]
    assert set(formats) == {"srt", "json"}


def test_command_always_sets_an_output_directory():
    # Left out, this build writes next to its own executable.
    assert "--output_dir" in _command()


def test_auto_language_samples_several_windows():
    """Detection on the first window alone is a coin flip on short audio."""
    command = _command(language="auto")
    assert "--language" not in command
    assert "--language_detection_segments" in command


def test_an_explicit_language_is_passed_through():
    command = _command(language="it")
    assert command[command.index("--language") + 1] == "it"
    assert "--language_detection_segments" not in command


def test_diarization_can_be_capped_and_disabled():
    capped = _command(max_speakers=2)
    assert capped[capped.index("--max_speakers") + 1] == "2"
    assert "--diarize" not in _command(diarize=None)
    assert "--max_speakers" not in _command(max_speakers=0)


# ── Locating the executable ─────────────────────


def test_explicit_directory_or_file_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "_KNOWN_DIRS", ())
    monkeypatch.setattr(backends.shutil, "which", lambda _name: None)
    exe = tmp_path / backends._EXE_NAME
    exe.write_text("", encoding="utf-8")

    assert backends.find_faster_whisper(tmp_path) == exe
    assert backends.find_faster_whisper(exe) == exe


def test_env_var_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "_KNOWN_DIRS", ())
    monkeypatch.setattr(backends.shutil, "which", lambda _name: None)
    exe = tmp_path / backends._EXE_NAME
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("FASTER_WHISPER_XXL", str(tmp_path))
    assert backends.find_faster_whisper() == exe


def test_missing_executable_is_reported_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(backends, "_KNOWN_DIRS", ())
    monkeypatch.setattr(backends.shutil, "which", lambda _name: None)
    monkeypatch.delenv("FASTER_WHISPER_XXL", raising=False)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")

    assert backends.find_faster_whisper() is None
    with pytest.raises(backends.TranscriptionError, match="was not found"):
        backends.transcribe_faster_whisper(audio)


# ── Running the standalone build ────────────────


@pytest.fixture
def fake_exe(tmp_path, monkeypatch):
    exe = tmp_path / backends._EXE_NAME
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(backends, "_KNOWN_DIRS", ())
    monkeypatch.setattr(backends.shutil, "which", lambda _name: None)
    monkeypatch.setenv("FASTER_WHISPER_XXL", str(exe))
    return exe


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "20260101010101.mp3"
    path.write_bytes(b"\xff\xf3fake")
    return path


def _runner_writing(srt=None, json_text=None, returncode=0):
    """A fake subprocess.run that writes output files the way the real one does."""

    def run(command, **_kwargs):
        out_dir = Path(command[command.index("--output_dir") + 1])
        stem = Path(command[1]).stem
        if srt is not None:
            (out_dir / f"{stem}.srt").write_text(srt, encoding="utf-8")
        if json_text is not None:
            (out_dir / f"{stem}.json").write_text(json_text, encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, "", "")

    return run


def test_transcription_reads_speakers_from_srt_and_language_from_json(
    fake_exe, audio,
):
    result = backends.transcribe_faster_whisper(
        audio, runner=_runner_writing(SAMPLE_SRT, '{"language": "it"}'),
    )
    assert result.backend == backends.LOCAL_BACKEND
    assert result.language == "it"
    assert result.diarized is True
    assert result.speakers == ["SPEAKER_01", "SPEAKER_02"]


def test_output_on_disk_beats_a_nonzero_exit(fake_exe, audio):
    """This build sometimes crashes in its own cleanup, after writing the output.

    The files are the real result, so a crash with output present is a success.
    """
    result = backends.transcribe_faster_whisper(
        audio,
        runner=_runner_writing(SAMPLE_SRT, '{"language": "en"}', returncode=-1073740791),
    )
    assert len(result.segments) == 3


def test_no_output_at_all_is_an_error(fake_exe, audio):
    with pytest.raises(backends.TranscriptionError, match="wrote no transcript"):
        backends.transcribe_faster_whisper(audio, runner=_runner_writing(returncode=1))


def test_json_only_output_still_works_without_speakers(fake_exe, audio):
    payload = '{"language": "de", "segments": [{"start": 0, "end": 1, "text": "hallo"}]}'
    result = backends.transcribe_faster_whisper(
        audio, runner=_runner_writing(json_text=payload),
    )
    assert result.language == "de"
    assert result.diarized is False
    assert result.segments[0].text == "hallo"


def test_a_timeout_is_reported_clearly(fake_exe, audio):
    def run(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    with pytest.raises(backends.TranscriptionError, match="timed out"):
        backends.transcribe_faster_whisper(audio, timeout=5, runner=run)


def test_missing_audio_is_refused_before_launching_anything(fake_exe, tmp_path):
    def run(_command, **_kwargs):
        raise AssertionError("should not have run")

    with pytest.raises(backends.TranscriptionError, match="not found"):
        backends.transcribe_faster_whisper(tmp_path / "nope.mp3", runner=run)


# ── Dispatch and config ─────────────────────────


def test_backend_none_produces_an_empty_transcription(audio):
    result = backends.transcribe(audio, backend="none")
    assert result.segments == []
    assert result.backend == "none"


def test_unknown_backend_is_refused(audio):
    with pytest.raises(backends.TranscriptionError, match="Unknown transcription backend"):
        backends.transcribe(audio, backend="wishful")


def test_options_come_from_the_config():
    config = {"defaults": {
        "transcribe_backend": "faster-whisper-xxl",
        "language": "it",
        "device": "cpu",
        "max_speakers": "2",
    }}
    options = backends.options_from_config(config)
    assert options["backend"] == "faster-whisper-xxl"
    assert options["language"] == "it"
    assert options["device"] == "cpu"
    assert options["max_speakers"] == 2


def test_an_english_only_model_is_not_carried_into_the_local_backend():
    """`base.en` would silently transcribe Italian and German as nonsense."""
    config = {"defaults": {
        "transcribe_backend": "faster-whisper-xxl",
        "whisper_model": "base.en",
    }}
    assert backends.options_from_config(config)["model"] == backends.DEFAULT_LOCAL_MODEL


def test_the_default_backend_keeps_its_own_model():
    config = {"defaults": {"whisper_model": "base.en"}}
    options = backends.options_from_config(config)
    assert options["backend"] == backends.DEFAULT_BACKEND
    assert options["model"] == "base.en"


def test_a_nonsense_max_speakers_does_not_crash_the_run():
    config = {"defaults": {"max_speakers": "two"}}
    assert backends.options_from_config(config)["max_speakers"] is None


def test_labels_from_the_backend_are_trusted_without_rediarizing(
    fake_exe, audio, monkeypatch,
):
    def explode(*_args, **_kwargs):
        raise AssertionError("diarize_auto must not run when the backend labelled")

    import pocket_libre.diarize as diarize_module

    monkeypatch.setattr(diarize_module, "diarize_auto", explode)
    monkeypatch.setattr(
        backends, "transcribe",
        lambda *_a, **_k: backends.Transcription(
            segments=backends.parse_srt(SAMPLE_SRT), diarized=True,
        ),
    )
    labeled, _result = backends.transcribe_and_label(audio)
    assert labeled[0]["speaker"] == "SPEAKER_01"


def test_an_unlabelled_backend_falls_back_to_the_existing_chain(
    audio, monkeypatch,
):
    monkeypatch.setattr(
        backends, "transcribe",
        lambda *_a, **_k: backends.Transcription(
            segments=[backends.Segment(0.0, 1.0, "hello")], diarized=False,
        ),
    )
    called = {}

    import pocket_libre.diarize as diarize_module

    def fake_diarize(segments, audio_path=None, hf_token=None, anthropic_key=None):
        called["ran"] = True
        return []

    monkeypatch.setattr(diarize_module, "diarize_auto", fake_diarize)
    monkeypatch.setattr(
        diarize_module, "merge_transcript_with_speakers",
        lambda segments, _speakers: [
            {**s, "speaker": "Guessed"} for s in segments
        ],
    )

    labeled, _result = backends.transcribe_and_label(audio)
    assert called.get("ran") is True
    assert labeled[0]["speaker"] == "Guessed"
