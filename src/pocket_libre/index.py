"""Full-text search over a library, in SQLite.

One index per library, stored inside it, so a search can never reach into
another profile's recordings. Nothing leaves the machine: FTS5 ships with
Python's own sqlite3.

The index is derived data. It is rebuilt from the transcripts on disk whenever
they change, and deleting it costs nothing but the next few seconds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

INDEX_FILENAME = "index.sqlite3"

# Snippets mark the matched words with these. Control characters rather than
# brackets, because a transcript line already starts with "[00:00]" and a
# bracket delimiter would be indistinguishable from the timestamps.
MATCH_OPEN = ""
MATCH_CLOSE = ""

# What gets indexed, and the label each kind carries in results.
INDEXED_SUFFIXES = {
    "_transcript.txt": "transcript",
    "_summary.md": "summary",
    "_actions.md": "actions",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS indexed_files (
    path     TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5(
    date,
    timestamp,
    kind,
    body,
    path UNINDEXED,
    -- Accent folding so a search for "perche" also finds "perché", which
    -- matters the moment the transcripts are not in English.
    tokenize = "unicode61 remove_diacritics 2"
);
"""


@dataclass
class Hit:
    """One search result."""

    date: str
    timestamp: str
    kind: str
    snippet: str
    path: str

    @property
    def reference(self) -> str:
        return f"{self.date}/{self.timestamp}"


def index_path(library_dir: str | Path) -> Path:
    return Path(library_dir) / INDEX_FILENAME


def connect(library_dir: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the index for one library."""
    path = index_path(library_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def _indexable(library: Path):
    """Every transcript, summary and action list in the library, with its kind."""
    for date_dir in sorted(p for p in library.iterdir() if p.is_dir()):
        for file in sorted(date_dir.iterdir()):
            if not file.is_file():
                continue
            for suffix, kind in INDEXED_SUFFIXES.items():
                if file.name.endswith(suffix):
                    yield file, date_dir.name, file.name[: -len(suffix)], kind
                    break


def build(library_dir: str | Path, *, rebuild: bool = False) -> dict:
    """Bring the index up to date. Returns counts of what changed.

    Incremental by mtime and size, so running it before every search is cheap
    enough to do unconditionally.
    """
    library = Path(library_dir)
    stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
    if not library.is_dir():
        return stats

    connection = connect(library)
    try:
        if rebuild:
            connection.execute("DELETE FROM documents")
            connection.execute("DELETE FROM indexed_files")

        known = {
            row["path"]: (row["mtime_ns"], row["size"])
            for row in connection.execute("SELECT path, mtime_ns, size FROM indexed_files")
        }
        seen = set()

        for file, date, timestamp, kind in _indexable(library):
            key = str(file.relative_to(library)).replace("\\", "/")
            seen.add(key)
            info = file.stat()
            fingerprint = (info.st_mtime_ns, info.st_size)

            if known.get(key) == fingerprint:
                stats["unchanged"] += 1
                continue

            body = file.read_text(encoding="utf-8", errors="replace")
            if key in known:
                connection.execute("DELETE FROM documents WHERE path = ?", (key,))
                stats["updated"] += 1
            else:
                stats["added"] += 1

            connection.execute(
                "INSERT INTO documents(date, timestamp, kind, body, path) "
                "VALUES (?, ?, ?, ?, ?)",
                (date, timestamp, kind, body, key),
            )
            connection.execute(
                "INSERT OR REPLACE INTO indexed_files(path, mtime_ns, size) "
                "VALUES (?, ?, ?)",
                (key, info.st_mtime_ns, info.st_size),
            )

        for stale in set(known) - seen:
            connection.execute("DELETE FROM documents WHERE path = ?", (stale,))
            connection.execute("DELETE FROM indexed_files WHERE path = ?", (stale,))
            stats["removed"] += 1

        connection.commit()
    finally:
        connection.close()

    return stats


def _fts_query(text: str) -> str:
    """Turn a plain phrase into an FTS5 query without exposing its syntax.

    Every term is quoted, so a stray quote or a bare `NEAR` from a transcript
    search box is matched literally instead of raising a syntax error.
    """
    terms = [term for term in text.replace('"', " ").split() if term]
    if not terms:
        return ""
    return " ".join(f'"{term}"' for term in terms)


def search(library_dir: str | Path, query: str, *,
           limit: int = 20, kinds: tuple | None = None,
           refresh: bool = True) -> list[Hit]:
    """Search one library. Returns the best matches, most relevant first."""
    expression = _fts_query(query)
    if not expression:
        return []

    library = Path(library_dir)
    if refresh:
        build(library)
    if not index_path(library).exists():
        return []

    sql = (
        "SELECT date, timestamp, kind, path, "
        f"snippet(documents, 3, '{MATCH_OPEN}', '{MATCH_CLOSE}', ' ... ', 18) "
        "AS snippet "
        "FROM documents WHERE documents MATCH ?"
    )
    params: list = [expression]
    if kinds:
        sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(kinds)
    sql += " ORDER BY rank LIMIT ?"
    params.append(int(limit))

    connection = connect(library)
    try:
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()

    return [
        Hit(date=row["date"], timestamp=row["timestamp"], kind=row["kind"],
            snippet=" ".join(str(row["snippet"]).split()), path=row["path"])
        for row in rows
    ]
