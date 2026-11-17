"""Search the transcripts saved under the output folder.

Transcripts are <output>/<date>/<name>_transcript.txt, one segment a line:
"[MM:SS] Speaker: text". A line matches when it holds every word of the
query, in any order and case; with `regex`, when the pattern matches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TRANSCRIPT_SUFFIX = "_transcript.txt"
_LINE = re.compile(r"^\[(\d+):(\d{2})\]\s*([^:]{1,80}?):\s?(.*)$")


@dataclass
class Line:
    number: int                 # 1-based, in the transcript file
    offset_s: int | None        # into the recording
    speaker: str | None
    text: str

    @property
    def clock(self) -> str:
        if self.offset_s is None:
            return ""
        return f"{self.offset_s // 60:02d}:{self.offset_s % 60:02d}"


@dataclass
class Hit:
    date: str                   # the folder: the day it was recorded
    name: str                   # the recording's name (its .mp3 stem)
    path: Path                  # the transcript
    line: Line
    spans: list[tuple[int, int]]  # where the query matched in line.text
    before: list[Line] = field(default_factory=list)
    after: list[Line] = field(default_factory=list)

    @property
    def recorded_at(self) -> datetime | None:
        return recording_time(self.name)


def recording_time(name: str) -> datetime | None:
    """When a recording started, from its name (UTC, as the device clock is
    set): YYYYMMDDHHmmss, or PH + YYMMDDHHmmss."""
    if name.startswith("PH"):
        digits, fmt = name[2:14], "%y%m%d%H%M%S"
    else:
        digits, fmt = name[:14], "%Y%m%d%H%M%S"
    try:
        return datetime.strptime(digits, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_line(number: int, raw: str) -> Line:
    m = _LINE.match(raw)
    if not m:
        return Line(number, None, None, raw.strip())
    minutes, seconds, speaker, text = m.groups()
    return Line(number, int(minutes) * 60 + int(seconds), speaker.strip(), text.strip())


def transcripts(root: Path, since: str | None = None) -> list[tuple[str, str, Path]]:
    """(date, name, path) for every transcript under `root`, newest first.
    `since` (YYYY-MM-DD) leaves out earlier dates."""
    found = []
    if not root.is_dir():
        return found
    for date_dir in root.iterdir():
        if not date_dir.is_dir() or (since and date_dir.name < since):
            continue
        for path in date_dir.glob(f"*{TRANSCRIPT_SUFFIX}"):
            found.append((date_dir.name, path.name[:-len(TRANSCRIPT_SUFFIX)], path))
    return sorted(found, key=lambda f: (f[0], f[1]), reverse=True)


def matcher(query: str, regex: bool = False):
    """A function from text to the spans that match, empty if it doesn't.

    Raises ValueError for an empty query or a bad pattern.
    """
    if regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Bad pattern: {e}") from e

        def match(text: str) -> list[tuple[int, int]]:
            return [m.span() for m in pattern.finditer(text) if m.end() > m.start()]
        return match

    words = [w for w in query.split() if w]
    if not words:
        raise ValueError("Nothing to search for.")
    patterns = [re.compile(re.escape(w), re.IGNORECASE) for w in words]

    def match(text: str) -> list[tuple[int, int]]:
        spans = []
        for p in patterns:
            found = [m.span() for m in p.finditer(text)]
            if not found:
                return []
            spans.extend(found)
        return _merge(spans)
    return match


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def search(root, query: str, regex: bool = False, speaker: str | None = None,
           since: str | None = None, context: int = 0,
           limit: int | None = None) -> list[Hit]:
    """Every matching line, newest recording first, in order within one.

    `speaker` keeps lines whose speaker label contains it (any case). Only
    the spoken text is searched, not the timestamp or the speaker label.
    """
    match = matcher(query, regex)
    speaker_lc = speaker.lower() if speaker else None
    hits: list[Hit] = []
    for date, name, path in transcripts(Path(root), since):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        lines = [parse_line(i + 1, r) for i, r in enumerate(raw)]
        lines = [ln for ln in lines if ln.text]
        for i, line in enumerate(lines):
            if speaker_lc and speaker_lc not in (line.speaker or "").lower():
                continue
            spans = match(line.text)
            if not spans:
                continue
            hits.append(Hit(date, name, path, line, spans,
                            lines[max(0, i - context):i] if context else [],
                            lines[i + 1:i + 1 + context] if context else []))
            if limit and len(hits) >= limit:
                return hits
    return hits
