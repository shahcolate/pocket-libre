"""Turning a processed recording into something to act on.

Two jobs, both pure text and both offline:

`render_actions` writes the action items the `entities` analysis already
extracts as a checkbox list. It never invents a date: a task gets a due date
only when the transcript stated one, because a made-up deadline on someone
else's task is worse than no deadline.

`export_markdown` writes one self-contained note per recording, for a notes
vault or any folder of Markdown. It is off unless a profile asks for it: one
person's recordings do not belong in another person's vault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Only an unambiguous, already-absolute date is carried over. "next Tuesday"
# or "end of the month" are left in the task text, where they are honest.
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# "20261004120000", with or without a trailing "(2026-10-04 12:10)".
_GENERATED_TITLE = re.compile(r"^\d{8,}")

# A transcript line: "[MM:SS] Name: what they said".
_TRANSCRIPT_LINE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*(?P<speaker>[^:]+):")


@dataclass
class ActionItem:
    """One commitment someone made, as extracted from a transcript."""

    task: str
    owner: str | None = None
    deadline: str | None = None

    @property
    def due_date(self) -> str | None:
        """The deadline, but only when it is already an absolute date."""
        if not self.deadline:
            return None
        match = _ISO_DATE.search(str(self.deadline))
        return match.group(0) if match else None

    def as_checkbox(self, due_marker: str = "\N{CALENDAR}") -> str:
        """`- [ ] owner: task 📅 YYYY-MM-DD`, with each part only if real.

        `due_marker` exists because a Windows console in a legacy code page
        cannot encode the calendar emoji, and crashing while printing a task
        list would be a silly way to lose one.
        """
        text = self.task.strip().rstrip(".")
        if self.owner and self.owner.strip().lower() not in ("", "unknown", "null"):
            text = f"{self.owner.strip()}: {text}"

        due = self.due_date
        if due:
            return f"- [ ] {text} {due_marker} {due}".replace("  ", " ")
        if self.deadline and not due:
            # Said out loud but not a date: keep the words, lose the pretence.
            stated = str(self.deadline).strip()
            if stated.lower() not in ("none", "null", ""):
                return f"- [ ] {text} (said: {stated})"
        return f"- [ ] {text}"


def action_items_from_entities(entities: dict | None) -> list[ActionItem]:
    """Read action items out of the `entities` analysis, tolerating its shapes."""
    if not isinstance(entities, dict):
        return []
    raw = entities.get("action_items")
    if not isinstance(raw, list):
        return []

    items = []
    for entry in raw:
        if isinstance(entry, str):
            task = entry.strip()
            if task:
                items.append(ActionItem(task=task))
            continue
        if not isinstance(entry, dict):
            continue
        task = str(entry.get("task") or entry.get("item") or "").strip()
        if not task:
            continue
        owner = entry.get("owner") or entry.get("who")
        deadline = entry.get("deadline") or entry.get("due")
        items.append(ActionItem(
            task=task,
            owner=str(owner).strip() if owner else None,
            deadline=str(deadline).strip() if deadline else None,
        ))
    return items


def render_actions(items: list[ActionItem], *, heading: str = "## Action items",
                   source: str | None = None,
                   due_marker: str = "\N{CALENDAR}") -> str:
    """A checkbox list, ready to paste into a task file."""
    if not items:
        return ""
    lines = [heading, ""]
    lines.extend(item.as_checkbox(due_marker=due_marker) for item in items)
    if source:
        lines += ["", f"*From {source}.*"]
    return "\n".join(lines) + "\n"


# ── One note per recording ──────────────────────


def slug(text: str, *, limit: int = 60) -> str:
    """A filename-safe fragment of `text`."""
    cleaned = _SLUG_STRIP.sub("-", str(text).lower()).strip("-")
    return cleaned[:limit].strip("-")


def speakers_in_transcript(transcript: str) -> list[str]:
    """Who speaks in a `[MM:SS] Name: text` transcript, in first-spoken order."""
    found: list[str] = []
    for line in transcript.splitlines():
        match = _TRANSCRIPT_LINE.match(line)
        if not match:
            continue
        name = match.group("speaker").strip()
        if name and name not in found:
            found.append(name)
    return found


def title_from_summary(summary: str | None, fallback: str,
                       skip_prefix: str | None = None) -> str:
    """The first heading in `summary` that reads like a title.

    The generated summary opens with the recording's own timestamp as its
    `# ` heading, which would make a useless note name, so headings starting
    with that stamp are passed over rather than used.
    """
    for line in (summary or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            continue
        candidate = stripped.lstrip("#").strip()
        if not candidate:
            continue
        if skip_prefix and candidate.startswith(str(skip_prefix)):
            continue
        # A heading that opens with a long run of digits is the generated
        # timestamp title, whatever follows it in brackets.
        if _GENERATED_TITLE.match(candidate):
            continue
        return candidate
    return fallback


def build_note(
    *,
    recording: str,
    recorded_on: str,
    transcript: str,
    summary: str | None = None,
    actions: list[ActionItem] | None = None,
    speakers: list[str] | None = None,
    language: str | None = None,
    profile: str | None = None,
    links: list[str] | None = None,
    title: str | None = None,
    today: str | None = None,
) -> tuple[str, str]:
    """Render one recording as a Markdown note. Returns (title, body).

    Frontmatter carries only what is known. The transcript goes in last and
    whole: a summary is a convenience, not a replacement for what was said.
    """
    stamp = recording.rsplit("/", 1)[-1]
    heading = title or title_from_summary(
        summary, f"Recording {recording}", skip_prefix=stamp,
    )
    if speakers is None:
        speakers = speakers_in_transcript(transcript)
    created = today or date.today().isoformat()

    front = [
        "---",
        f"title: {heading}",
        f"recorded: {recorded_on}",
        f"created: {created}",
        "source: pocket-libre",
    ]
    if profile:
        front.append(f"profile: {profile}")
    if language:
        front.append(f"language: {language}")
    if speakers:
        front.append("speakers: [" + ", ".join(speakers) + "]")
    front.append(f"recording: {recording}")
    front.append("---")

    body = ["\n".join(front), ""]
    if links:
        body += [" · ".join(links), ""]
    body += [f"# {heading}", ""]

    if summary:
        # Drop the generated document's own title and its appended transcript:
        # both are represented better here.
        cleaned = summary.split("\n## Full Transcript", 1)[0]
        cleaned = re.sub(r"^#\s+\S+.*\n+", "", cleaned, count=1)
        body += [cleaned.strip(), ""]

    if actions:
        body += [render_actions(actions).rstrip(), ""]

    body += ["## Transcript", "", transcript.strip(), ""]
    return heading, "\n".join(body)


def note_filename(recorded_on: str, title: str, recording: str) -> str:
    """`YYYY-MM-DD <slug>.md`, falling back to the recording's own stamp."""
    fragment = slug(title) or slug(recording) or "recording"
    return f"{recorded_on} {fragment}.md"


def export_note(
    destination: str | Path,
    *,
    recording: str,
    recorded_on: str,
    transcript: str,
    overwrite: bool = False,
    **note_options,
) -> Path:
    """Write one note into `destination`. Returns the path written.

    Refuses to clobber an existing note unless asked: that file may have been
    edited by hand since it was exported.
    """
    title, body = build_note(
        recording=recording, recorded_on=recorded_on, transcript=transcript,
        **note_options,
    )
    folder = Path(destination)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / note_filename(recorded_on, title, recording)

    if path.exists() and not overwrite:
        raise FileExistsError(path)

    path.write_text(body, encoding="utf-8")
    return path
