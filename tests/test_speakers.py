"""Voice identity: embeddings, enrollment, and matching across recordings."""

import json

import pytest

from pocket_libre import speakers


def vector(seed: float, size: int = 8) -> list[float]:
    return [seed + i * 0.01 for i in range(size)]


# ── Similarity ──────────────────────────────────


def test_identical_vectors_match_exactly():
    assert speakers.cosine_similarity(vector(1.0), vector(1.0)) == pytest.approx(1.0)


def test_opposite_vectors_do_not_match():
    a = [1.0, 0.0]
    assert speakers.cosine_similarity(a, [-1.0, 0.0]) == pytest.approx(-1.0)


def test_degenerate_input_scores_zero_instead_of_dividing_by_zero():
    assert speakers.cosine_similarity([], [1.0]) == 0.0
    assert speakers.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert speakers.cosine_similarity([1.0, 2.0], [1.0]) == 0.0


# ── Reading what the backend dumps ──────────────


def test_embedding_dumps_are_read_and_cleared(tmp_path):
    """They land in the library directory, so they must not be left behind."""
    audio = tmp_path / "20260101010101.mp3"
    audio.write_bytes(b"x")
    for label, value in (("SPEAKER_00", 0.1), ("SPEAKER_01", 0.9)):
        dump = tmp_path / f"{audio.name}_diarize_pyannote_v3.1_{label}.txt"
        dump.write_text("\n".join(str(value + i) for i in range(4)), encoding="utf-8")

    found = speakers.read_embedding_dumps(audio)
    assert set(found) == {"SPEAKER_00", "SPEAKER_01"}
    assert found["SPEAKER_01"][0] == pytest.approx(0.9)
    assert not list(tmp_path.glob("*_diarize_*.txt"))


def test_dumps_can_be_kept_when_asked(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    dump = tmp_path / "a.mp3_diarize_pyannote_v3.1_SPEAKER_00.txt"
    dump.write_text("0.5\n0.5", encoding="utf-8")
    speakers.read_embedding_dumps(audio, cleanup=False)
    assert dump.exists()


def test_an_unreadable_dump_is_skipped_not_fatal(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    (tmp_path / "a.mp3_diarize_x_SPEAKER_00.txt").write_text("not a number", "utf-8")
    assert speakers.read_embedding_dumps(audio) == {}


def test_recording_voices_round_trip(tmp_path):
    path = tmp_path / "x_voices.json"
    speakers.save_recording_voices(path, {"SPEAKER_01": vector(0.3)})
    assert speakers.load_recording_voices(path)["SPEAKER_01"][0] == pytest.approx(0.3)


def test_missing_or_broken_voices_file_is_empty(tmp_path):
    assert speakers.load_recording_voices(tmp_path / "nope.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert speakers.load_recording_voices(broken) == {}


# ── The library ─────────────────────────────────


def test_enrolling_and_forgetting(tmp_path):
    library = speakers.VoiceLibrary.load(tmp_path)
    library.enroll("Erika", vector(0.5))
    library.enroll("Erika", vector(0.51))
    library.save()

    reloaded = speakers.VoiceLibrary.load(tmp_path)
    assert reloaded.names == ["Erika"]
    assert len(reloaded.voices["Erika"]) == 2, "samples are kept, not averaged"
    assert reloaded.forget("Erika") is True
    assert reloaded.forget("Erika") is False


def test_a_voice_needs_a_name_and_a_vector(tmp_path):
    library = speakers.VoiceLibrary.load(tmp_path)
    with pytest.raises(ValueError):
        library.enroll("  ", vector(0.1))
    with pytest.raises(ValueError):
        library.enroll("Erika", [])


def test_a_corrupt_library_loads_empty_rather_than_crashing(tmp_path):
    (tmp_path / speakers.LIBRARY_FILENAME).write_text("{oops", encoding="utf-8")
    library = speakers.VoiceLibrary.load(tmp_path)
    assert library.voices == {}
    assert library.threshold == speakers.DEFAULT_THRESHOLD


def test_threshold_survives_a_save(tmp_path):
    library = speakers.VoiceLibrary.load(tmp_path)
    library.threshold = 0.8
    library.enroll("A", vector(0.1))
    library.save()
    assert speakers.VoiceLibrary.load(tmp_path).threshold == 0.8


def test_a_nonsense_threshold_in_the_file_falls_back_to_the_default(tmp_path):
    (tmp_path / speakers.LIBRARY_FILENAME).write_text(
        json.dumps({"threshold": "loud", "voices": {}}), encoding="utf-8",
    )
    assert speakers.VoiceLibrary.load(tmp_path).threshold == speakers.DEFAULT_THRESHOLD


# ── Matching ────────────────────────────────────


def _library(tmp_path, **voices):
    library = speakers.VoiceLibrary.load(tmp_path)
    for name, vec in voices.items():
        library.enroll(name, vec)
    return library


def test_a_known_voice_gets_its_name(tmp_path):
    library = _library(tmp_path, Erika=[1.0, 0.0, 0.0])
    names = speakers.identify({"SPEAKER_01": [0.99, 0.01, 0.0]}, library)
    assert names == {"SPEAKER_01": "Erika"}


def test_an_unknown_voice_stays_unnamed(tmp_path):
    """Below the threshold, no name is the right answer."""
    library = _library(tmp_path, Erika=[1.0, 0.0, 0.0])
    assert speakers.identify({"SPEAKER_01": [0.0, 1.0, 0.0]}, library) == {}


def test_two_speakers_can_never_become_the_same_person(tmp_path):
    """One name per label and one label per name, best pairs first."""
    library = _library(tmp_path, Erika=[1.0, 0.0])
    names = speakers.identify(
        {"SPEAKER_01": [1.0, 0.02], "SPEAKER_02": [1.0, 0.01]}, library,
    )
    assert list(names.values()) == ["Erika"]
    assert len(names) == 1


def test_the_stronger_pair_wins_when_two_labels_compete(tmp_path):
    library = _library(tmp_path, Erika=[1.0, 0.0])
    names = speakers.identify(
        {"weak": [1.0, 0.5], "strong": [1.0, 0.0]}, library, threshold=0.5,
    )
    assert names == {"strong": "Erika"}


def test_nothing_to_match_against_returns_nothing(tmp_path):
    empty = speakers.VoiceLibrary.load(tmp_path)
    assert speakers.identify({"SPEAKER_01": vector(0.1)}, empty) == {}
    assert speakers.identify({}, _library(tmp_path, A=vector(0.1))) == {}


def test_best_match_reports_the_score(tmp_path):
    library = _library(tmp_path, Erika=[1.0, 0.0], David=[0.0, 1.0])
    name, score = library.best_match([0.95, 0.05])
    assert name == "Erika"
    assert score > 0.9


def test_names_are_applied_only_where_matched():
    segments = [
        {"start": 0, "end": 1, "speaker": "SPEAKER_01", "text": "a"},
        {"start": 1, "end": 2, "speaker": "SPEAKER_02", "text": "b"},
    ]
    out = speakers.apply_names(segments, {"SPEAKER_01": "Erika"})
    assert out[0]["speaker"] == "Erika"
    assert out[1]["speaker"] == "SPEAKER_02"
    assert speakers.apply_names(segments, {}) is segments


def test_dominant_speaker_is_whoever_holds_the_floor_longest():
    segments = [
        {"start": 0, "end": 10, "speaker": "A", "text": "x"},
        {"start": 10, "end": 12, "speaker": "B", "text": "y"},
        {"start": 12, "end": 14, "speaker": "B", "text": "z"},
    ]
    assert speakers.dominant_speaker(segments) == "A"
    assert speakers.dominant_speaker([]) is None
    assert speakers.dominant_speaker([{"start": 0, "end": 1}]) is None
