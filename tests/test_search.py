"""search: saved transcripts, from the command line and the web."""

import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from pocket_libre.cli import cli
from pocket_libre.search import parse_line, recording_time, search
from pocket_libre.web import app as webapp


@pytest.fixture
def root(tmp_path):
    older = tmp_path / "2026-10-03"
    newer = tmp_path / "2026-10-05"
    older.mkdir()
    newer.mkdir()
    (older / "20261003143216_transcript.txt").write_text(
        "[00:01] Host: Welcome back to the budget review.\n"
        "[00:09] Guest: Thanks. The budget for next quarter is tight.\n"
        "\n"
        "[125:04] Host: We need a deadline for the report.\n", encoding="utf-8")
    (newer / "PH261005091500_transcript.txt").write_text(
        "[00:03] Speaker 1: Remind me about the dentist.\n"
        "[01:15] Speaker 2: Budget meeting moved to Friday.\n"
        "a line without a timestamp mentions the budget too\n", encoding="utf-8")
    (newer / "PH261005091500_summary.md").write_text("budget budget budget")  # not searched
    return tmp_path


def _where(hits):
    return [(h.name, h.line.clock) for h in hits]


def test_every_word_in_any_order_any_case(root):
    assert _where(search(root, "QUARTER budget")) == [("20261003143216", "00:09")]


def test_newest_recording_first_then_in_order(root):
    assert _where(search(root, "budget")) == [
        ("PH261005091500", "01:15"), ("PH261005091500", ""),
        ("20261003143216", "00:01"), ("20261003143216", "00:09")]


def test_spans_mark_each_word(root):
    (hit,) = search(root, "budget quarter")
    text = hit.line.text
    assert [text[a:b] for a, b in hit.spans] == ["budget", "quarter"]


def test_speaker_since_limit_and_context(root):
    assert _where(search(root, "the", speaker="host")) == [
        ("20261003143216", "00:01"), ("20261003143216", "125:04")]
    assert _where(search(root, "budget", since="2026-10-04")) == [
        ("PH261005091500", "01:15"), ("PH261005091500", "")]
    assert len(search(root, "budget", limit=1)) == 1
    (hit,) = search(root, "deadline", context=1)
    assert [ln.clock for ln in hit.before] == ["00:09"]   # the blank line is skipped
    assert hit.after == []


def test_regex(root):
    assert _where(search(root, r"dead(line)?|dentist", regex=True)) == [
        ("PH261005091500", "00:03"), ("20261003143216", "125:04")]
    with pytest.raises(ValueError, match="Bad pattern"):
        search(root, "(", regex=True)


def test_empty_query_and_missing_folder(root, tmp_path):
    with pytest.raises(ValueError):
        search(root, "   ")
    assert search(tmp_path / "nope", "budget") == []


def test_speaker_labels_and_timestamps_are_not_searched(root):
    assert search(root, "Guest") == []
    assert search(root, "00:09") == []


def test_line_parsing():
    line = parse_line(4, "[125:04] Host: Hi: there")
    assert (line.offset_s, line.clock, line.speaker, line.text) == (7504, "125:04", "Host", "Hi: there")
    assert parse_line(1, "plain words").speaker is None


def test_recording_time_from_names():
    assert recording_time("20261003143216") == datetime(2026, 10, 3, 14, 32, 16, tzinfo=timezone.utc)
    assert recording_time("PH261005091500") == datetime(2026, 10, 5, 9, 15, tzinfo=timezone.utc)
    assert recording_time("meeting-notes") is None


# ── Command line ────────────────────────────────


def _run(root, *args):
    return CliRunner().invoke(cli, ["search", "--output-dir", str(root), *args])


def test_command_shows_matches_by_recording(root):
    result = _run(root, "budget")
    assert result.exit_code == 0, result.output
    assert "2026-10-05 · PH261005091500 (09:15 UTC)" in result.output
    assert "[01:15] Speaker 2: Budget meeting moved to Friday." in result.output
    assert "4 matches in 2 recording(s)" in result.output


def test_command_says_when_it_stopped_at_the_limit(root):
    result = _run(root, "--limit", "2", "budget")
    assert "showing the newest 2 (--limit 0 for all)" in " ".join(result.output.split())


def test_command_json(root):
    result = _run(root, "--json", "deadline")
    assert json.loads(result.output)[0] | {"transcript": None} == {
        "date": "2026-10-03", "name": "20261003143216",
        "recorded_at": "2026-10-03T14:32:16+00:00", "offset_s": 7504, "clock": "125:04",
        "speaker": "Host", "text": "We need a deadline for the report.", "line": 4,
        "transcript": None}


def test_command_exits_1_without_matches(root):
    assert _run(root, "zebra").exit_code == 1
    assert _run(root, "--json", "zebra").exit_code == 1


def test_command_rejects_a_bad_pattern(root):
    result = _run(root, "--regex", "(")
    assert result.exit_code == 2
    assert "Bad pattern" in result.output


# ── Web ─────────────────────────────────────────


@pytest.fixture
def client(root, monkeypatch):
    monkeypatch.setattr(webapp, "load_config", lambda: {})
    monkeypatch.setattr(webapp, "get_output_dir", lambda config, cli_value=None: str(root))
    return TestClient(webapp.app)


def test_web_search(client):
    body = client.get("/api/search", params={"q": "budget", "limit": 1}).json()
    assert body["truncated"] is True
    assert body["hits"] == [{
        "date": "2026-10-05", "timestamp": "PH261005091500",
        "recorded_at": "2026-10-05T09:15:00+00:00", "offset_s": 75, "clock": "01:15",
        "speaker": "Speaker 2", "text": "Budget meeting moved to Friday.", "spans": [[0, 6]]}]


def test_web_search_rejects_a_bad_query(client):
    assert client.get("/api/search", params={"q": " "}).status_code == 400
    assert client.get("/api/search", params={"q": "(", "regex": "true"}).status_code == 400
