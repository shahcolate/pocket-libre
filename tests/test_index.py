"""Full-text search over a library."""


from pocket_libre import index


def library_with(tmp_path, **by_reference):
    """Build a library: {"2026-10-04/20261004120000": "transcript text"}."""
    for reference, body in by_reference.items():
        date, stamp = reference.split("/")
        day = tmp_path / date
        day.mkdir(parents=True, exist_ok=True)
        (day / f"{stamp}_transcript.txt").write_text(body, encoding="utf-8")
    return tmp_path


def test_a_phrase_is_found_in_a_transcript(tmp_path):
    root = library_with(tmp_path, **{
        "2026-10-04/20261004120000": "[00:00] Ada: we should call the supplier back",
    })
    hits = index.search(root, "supplier")
    assert len(hits) == 1
    assert hits[0].reference == "2026-10-04/20261004120000"
    assert hits[0].kind == "transcript"
    assert "supplier" in hits[0].snippet.lower()


def test_accents_do_not_hide_a_result(tmp_path):
    """Transcripts are not all in English."""
    root = library_with(tmp_path, **{
        "2026-10-04/1": "non so perché ha chiamato",
    })
    assert index.search(root, "perche")
    assert index.search(root, "perché")


def test_summaries_and_actions_are_searchable_too(tmp_path):
    day = tmp_path / "2026-10-04"
    day.mkdir(parents=True)
    (day / "1_transcript.txt").write_text("spoken words", encoding="utf-8")
    (day / "1_summary.md").write_text("# Title\n\nagreed on the budget", encoding="utf-8")
    (day / "1_actions.md").write_text("- [ ] send the invoice", encoding="utf-8")

    assert {hit.kind for hit in index.search(tmp_path, "budget")} == {"summary"}
    assert {hit.kind for hit in index.search(tmp_path, "invoice")} == {"actions"}


def test_results_can_be_restricted_to_one_kind(tmp_path):
    day = tmp_path / "2026-10-04"
    day.mkdir(parents=True)
    (day / "1_transcript.txt").write_text("budget talk", encoding="utf-8")
    (day / "1_summary.md").write_text("budget agreed", encoding="utf-8")

    hits = index.search(tmp_path, "budget", kinds=("summary",))
    assert [hit.kind for hit in hits] == ["summary"]


def test_a_miss_returns_nothing(tmp_path):
    root = library_with(tmp_path, **{"2026-10-04/1": "nothing relevant"})
    assert index.search(root, "zebra") == []


def test_an_empty_query_is_not_a_match_for_everything(tmp_path):
    root = library_with(tmp_path, **{"2026-10-04/1": "words"})
    assert index.search(root, "   ") == []


def test_fts_syntax_in_the_query_is_matched_literally(tmp_path):
    """A search box must not be able to raise a syntax error."""
    root = library_with(tmp_path, **{"2026-10-04/1": "plain words here"})
    for hostile in ['NEAR "unclosed', "AND OR *", '"', "words OR"]:
        assert index.search(root, hostile) == [] or index.search(root, hostile)


def test_a_quoted_term_still_finds_its_word(tmp_path):
    root = library_with(tmp_path, **{"2026-10-04/1": "the supplier called"})
    assert index.search(root, '"supplier"')


def test_indexing_is_incremental(tmp_path):
    root = library_with(tmp_path, **{"2026-10-04/1": "first"})
    assert index.build(root)["added"] == 1
    assert index.build(root)["unchanged"] == 1

    (root / "2026-10-04" / "1_transcript.txt").write_text("second", encoding="utf-8")
    stats = index.build(root)
    assert stats["updated"] == 1
    assert index.search(root, "second")
    assert index.search(root, "first") == [], "the old text must not linger"


def test_a_deleted_recording_leaves_the_index(tmp_path):
    root = library_with(tmp_path, **{"2026-10-04/1": "ephemeral"})
    index.build(root)
    (root / "2026-10-04" / "1_transcript.txt").unlink()
    assert index.build(root)["removed"] == 1
    assert index.search(root, "ephemeral") == []


def test_rebuilding_from_scratch_works(tmp_path):
    root = library_with(tmp_path, **{"2026-10-04/1": "words"})
    index.build(root)
    stats = index.build(root, rebuild=True)
    assert stats["added"] == 1
    assert index.search(root, "words")


def test_a_missing_library_is_not_an_error(tmp_path):
    missing = tmp_path / "nothing-here"
    assert index.build(missing) == {
        "added": 0, "updated": 0, "removed": 0, "unchanged": 0,
    }
    assert index.search(missing, "anything") == []


def test_each_library_has_its_own_index(tmp_path):
    """A search must not be able to reach the other profile's recordings."""
    mine = library_with(tmp_path / "mine", **{"2026-10-04/1": "my private words"})
    hers = library_with(tmp_path / "hers", **{"2026-10-04/2": "her private words"})

    assert index.search(mine, "my") and not index.search(mine, "her private")
    assert index.search(hers, "her") and not index.search(hers, "my private")
    assert index.index_path(mine) != index.index_path(hers)
    assert index.index_path(mine).exists()
