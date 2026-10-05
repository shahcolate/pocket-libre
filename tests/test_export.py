"""Action items and Markdown notes."""

import pytest

from pocket_libre import export

# ── Action items ────────────────────────────────


def test_a_stated_date_becomes_a_due_date():
    item = export.ActionItem(task="Send the contract", owner="David",
                             deadline="2026-10-09")
    assert item.due_date == "2026-10-09"
    assert item.as_checkbox() == "- [ ] David: Send the contract \U0001F4C5 2026-10-09"


def test_a_vague_deadline_is_quoted_not_converted():
    """Turning "next Tuesday" into a date would be inventing someone's deadline."""
    item = export.ActionItem(task="Call back", owner="Erika", deadline="next Tuesday")
    assert item.due_date is None
    assert item.as_checkbox() == "- [ ] Erika: Call back (said: next Tuesday)"


def test_no_deadline_means_no_date():
    assert export.ActionItem(task="Decide").as_checkbox() == "- [ ] Decide"
    assert export.ActionItem(task="Decide", deadline="null").as_checkbox() == \
        "- [ ] Decide"


def test_an_unknown_owner_is_left_off():
    for owner in (None, "", "  ", "unknown", "null"):
        assert export.ActionItem(task="Do it", owner=owner).as_checkbox() == \
            "- [ ] Do it"


def test_the_due_marker_can_be_plain_for_a_legacy_console():
    item = export.ActionItem(task="x", deadline="2026-01-01")
    assert item.as_checkbox(due_marker="due") == "- [ ] x due 2026-01-01"


def test_a_date_inside_a_sentence_is_still_picked_up():
    item = export.ActionItem(task="x", deadline="by 2026-12-31 at the latest")
    assert item.due_date == "2026-12-31"


def test_action_items_are_read_from_the_entities_analysis():
    items = export.action_items_from_entities({"action_items": [
        {"owner": "David", "task": "Send it", "deadline": "2026-10-09"},
        {"who": "Erika", "item": "Call back", "due": "soon"},
        "Book the room",
        {"task": ""},
        12345,
    ]})
    assert [i.task for i in items] == ["Send it", "Call back", "Book the room"]
    assert items[1].owner == "Erika"
    assert items[1].deadline == "soon"


def test_missing_or_malformed_entities_yield_no_items():
    assert export.action_items_from_entities(None) == []
    assert export.action_items_from_entities({}) == []
    assert export.action_items_from_entities({"action_items": "nope"}) == []


def test_rendering_an_empty_list_produces_nothing():
    assert export.render_actions([]) == ""


def test_rendered_actions_are_a_checkbox_list():
    rendered = export.render_actions(
        [export.ActionItem(task="One"), export.ActionItem(task="Two")],
        source="2026-10-04/1",
    )
    assert "## Action items" in rendered
    assert rendered.count("- [ ] ") == 2
    assert "2026-10-04/1" in rendered


# ── Notes ───────────────────────────────────────


TRANSCRIPT = (
    "[00:00] SPEAKER_01: we should call the supplier\n"
    "[00:06] David: I will do it tomorrow\n"
    "[01:02:03] SPEAKER_01: good\n"
)

SUMMARY = (
    "# 20261004120000 (2026-10-04 12:10)\n\n"
    "## Supplier contract review\n\n"
    "Agreed to keep the vendor.\n\n"
    "---\n\n## Full Transcript\n\nignored\n"
)


def test_speakers_come_from_the_transcript_in_spoken_order():
    assert export.speakers_in_transcript(TRANSCRIPT) == ["SPEAKER_01", "David"]
    assert export.speakers_in_transcript("no timestamps here") == []


def test_the_title_skips_the_generated_timestamp_heading():
    """The summary's own first heading is the recording stamp, not a title."""
    assert export.title_from_summary(
        SUMMARY, "fallback", skip_prefix="20261004120000",
    ) == "Supplier contract review"


def test_the_title_falls_back_when_there_is_no_summary():
    assert export.title_from_summary(None, "Recording x") == "Recording x"
    assert export.title_from_summary("no headings at all", "Recording x") == \
        "Recording x"


def test_a_note_carries_what_is_known_and_nothing_else():
    title, body = export.build_note(
        recording="2026-10-04/20261004120000",
        recorded_on="2026-10-04",
        transcript=TRANSCRIPT,
        summary=SUMMARY,
        actions=[export.ActionItem(task="Send it", deadline="2026-10-09")],
        profile="erika",
        language="it",
        today="2026-10-04",
    )
    assert title == "Supplier contract review"
    assert body.startswith("---\n")
    assert "profile: erika" in body
    assert "language: it" in body
    assert "speakers: [SPEAKER_01, David]" in body
    assert "## Action items" in body
    # The summary's own title and appended transcript are not repeated.
    assert body.count("## Transcript") == 1
    assert "Full Transcript" not in body
    assert "20261004120000 (2026-10-04 12:10)" not in body
    assert TRANSCRIPT.strip() in body


def test_a_note_without_a_summary_is_still_a_note():
    title, body = export.build_note(
        recording="2026-10-04/1", recorded_on="2026-10-04", transcript=TRANSCRIPT,
    )
    assert title == "Recording 2026-10-04/1"
    assert "## Transcript" in body
    assert "## Action items" not in body


def test_unknown_fields_are_simply_absent():
    _title, body = export.build_note(
        recording="2026-10-04/1", recorded_on="2026-10-04", transcript="x",
    )
    assert "language:" not in body
    assert "profile:" not in body
    assert "speakers:" not in body


def test_the_filename_is_the_date_and_the_title():
    assert export.note_filename("2026-10-04", "Supplier contract review", "1") == \
        "2026-10-04 supplier-contract-review.md"


def test_the_filename_survives_a_title_with_nothing_usable():
    assert export.note_filename("2026-10-04", "!!!", "20261004120000") == \
        "2026-10-04 20261004120000.md"


def test_exporting_writes_one_file(tmp_path):
    path = export.export_note(
        tmp_path / "notes",
        recording="2026-10-04/1",
        recorded_on="2026-10-04",
        transcript=TRANSCRIPT,
        summary=SUMMARY,
    )
    assert path.exists()
    assert path.name == "2026-10-04 supplier-contract-review.md"
    assert "## Transcript" in path.read_text(encoding="utf-8")


def test_an_existing_note_is_not_clobbered(tmp_path):
    """It may have been edited by hand since it was exported."""
    options = dict(recording="2026-10-04/1", recorded_on="2026-10-04",
                   transcript=TRANSCRIPT, summary=SUMMARY)
    first = export.export_note(tmp_path, **options)
    first.write_text("edited by hand", encoding="utf-8")

    with pytest.raises(FileExistsError):
        export.export_note(tmp_path, **options)
    assert first.read_text(encoding="utf-8") == "edited by hand"

    export.export_note(tmp_path, overwrite=True, **options)
    assert "## Transcript" in first.read_text(encoding="utf-8")
