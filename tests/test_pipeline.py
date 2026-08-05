"""Transcript formatting, diarization merging, and cost estimation."""

import pytest

from pocket_libre.diarize import (
    SpeakerSegment,
    diarize_simple,
    merge_transcript_with_speakers,
)
from pocket_libre.pricing import (
    INPUT_PRICE_PER_MTOK,
    OUTPUT_PRICE_PER_MTOK,
    cost_usd,
)
from pocket_libre.summarize import (
    SUMMARY_PROMPTS,
    format_time,
    format_transcript_for_summary,
)
from pocket_libre.transcribe import format_timestamp_srt

# ── Time formatting ─────────────────────────────


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "00:00"), (5, "00:05"), (65, "01:05"), (600, "10:00"), (3661, "61:01")],
)
def test_format_time(seconds, expected):
    assert format_time(seconds) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00,000"),
        (1.5, "00:00:01,500"),
        (61.25, "00:01:01,250"),
        (3661.0, "01:01:01,000"),
    ],
)
def test_format_timestamp_srt(seconds, expected):
    assert format_timestamp_srt(seconds) == expected


# ── Transcript formatting ───────────────────────


def test_format_transcript_includes_speaker_and_time():
    segments = [{"start": 0.0, "end": 2.0, "speaker": "Speaker 1", "text": "Hello"}]
    assert format_transcript_for_summary(segments) == "[00:00] Speaker 1: Hello"


def test_format_transcript_joins_lines():
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "A", "text": "One"},
        {"start": 65.0, "end": 70.0, "speaker": "B", "text": "Two"},
    ]
    assert format_transcript_for_summary(segments).splitlines() == [
        "[00:00] A: One",
        "[01:05] B: Two",
    ]


def test_format_transcript_empty():
    assert format_transcript_for_summary([]) == ""


# ── Diarization ─────────────────────────────────


def test_diarize_simple_preserves_segment_bounds():
    whisper = [{"start": 0.0, "end": 1.0, "text": "a"}]
    out = diarize_simple(whisper)
    assert (out[0].start, out[0].end, out[0].speaker) == (0.0, 1.0, "Speaker")


def test_merge_labels_by_midpoint():
    whisper = [{"start": 0.0, "end": 2.0, "text": "Hello"}]
    speakers = [SpeakerSegment(0.0, 5.0, "Alice")]
    assert merge_transcript_with_speakers(whisper, speakers)[0]["speaker"] == "Alice"


def test_merge_without_speakers_falls_back():
    whisper = [{"start": 0.0, "end": 2.0, "text": "Hello"}]
    merged = merge_transcript_with_speakers(whisper, [])
    assert merged[0]["speaker"] == "Speaker"
    assert merged[0]["text"] == "Hello"


def test_merge_collapses_consecutive_same_speaker():
    whisper = [
        {"start": 0.0, "end": 2.0, "text": "One"},
        {"start": 2.0, "end": 4.0, "text": "Two"},
    ]
    speakers = [SpeakerSegment(0.0, 10.0, "Alice")]
    merged = merge_transcript_with_speakers(whisper, speakers)
    assert len(merged) == 1
    assert merged[0]["text"] == "One Two"
    assert merged[0]["end"] == 4.0


def test_merge_keeps_speaker_changes_separate():
    whisper = [
        {"start": 0.0, "end": 2.0, "text": "One"},
        {"start": 4.0, "end": 6.0, "text": "Two"},
    ]
    speakers = [SpeakerSegment(0.0, 3.0, "Alice"), SpeakerSegment(3.0, 8.0, "Bob")]
    merged = merge_transcript_with_speakers(whisper, speakers)
    assert [m["speaker"] for m in merged] == ["Alice", "Bob"]


def test_merge_marks_uncovered_span_unknown():
    whisper = [{"start": 100.0, "end": 102.0, "text": "Hello"}]
    speakers = [SpeakerSegment(0.0, 5.0, "Alice")]
    assert merge_transcript_with_speakers(whisper, speakers)[0]["speaker"] == "Unknown"


def test_merge_strips_whitespace():
    whisper = [{"start": 0.0, "end": 2.0, "text": "  padded  "}]
    assert merge_transcript_with_speakers(whisper, [])[0]["text"] == "padded"


def test_merge_does_not_mutate_input():
    whisper = [{"start": 0.0, "end": 2.0, "text": "One"}]
    merge_transcript_with_speakers(whisper, [SpeakerSegment(0.0, 5.0, "A")])
    assert whisper[0] == {"start": 0.0, "end": 2.0, "text": "One"}


# ── Prompts ─────────────────────────────────────


@pytest.mark.parametrize("style", ["meeting", "notes", "call", "raw"])
def test_every_documented_style_exists_and_takes_a_transcript(style):
    assert style in SUMMARY_PROMPTS
    rendered = SUMMARY_PROMPTS[style].format(transcript="XYZZY")
    assert "XYZZY" in rendered
    assert "{transcript}" not in rendered


# ── Pricing ─────────────────────────────────────


def test_cost_is_zero_for_no_tokens():
    assert cost_usd(0, 0) == 0


def test_cost_matches_published_rates():
    assert cost_usd(1_000_000, 0) == pytest.approx(INPUT_PRICE_PER_MTOK)
    assert cost_usd(0, 1_000_000) == pytest.approx(OUTPUT_PRICE_PER_MTOK)


def test_output_tokens_cost_more_than_input():
    assert cost_usd(0, 1000) > cost_usd(1000, 0)
