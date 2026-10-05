"""Background sync: wait for the device to appear, then pull new recordings.

Runs an indefinite loop that scans for the configured device, syncs anything
new when it shows up, and backs off when it doesn't. Intended to be left
running — drop the Pocket on the desk and recordings land on disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

console = Console()

# Back off from `poll_interval` up to this ceiling after repeated misses,
# so an absent device does not mean a scan every few seconds all day.
MAX_BACKOFF_SECONDS = 300.0


@dataclass
class WatchStats:
    """Running totals for the current watch session."""

    scans: int = 0
    sync_runs: int = 0
    recordings_synced: int = 0
    failures: int = 0


async def device_present(address: str, timeout: float = 5.0) -> bool:
    """True if the device is currently advertising."""
    from bleak import BleakScanner

    try:
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        return device is not None
    except Exception:
        return False


def next_backoff(current: float, base: float, ceiling: float = MAX_BACKOFF_SECONDS) -> float:
    """Double the wait after a miss, capped at `ceiling`."""
    if current < base:
        return base
    return min(current * 2, ceiling)


async def watch_loop(
    address: str,
    sync_once: Callable[[], Awaitable[int]],
    poll_interval: float = 60.0,
    presence_check: Callable[[str], Awaitable[bool]] | None = None,
    max_iterations: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> WatchStats:
    """Poll for the device and run `sync_once` whenever it is present.

    `sync_once` returns how many recordings it pulled. The callable seams
    (`presence_check`, `sleep`, `max_iterations`) exist so the loop can be
    driven deterministically in tests without BLE hardware or real waiting.
    """
    check = presence_check or device_present
    stats = WatchStats()
    # Starts below `poll_interval` so the first miss waits the base interval
    # rather than immediately doubling it.
    backoff = 0.0
    iterations = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        stats.scans += 1

        present = await check(address)
        if present:
            console.print("[green]Device found — syncing...[/green]")
            stats.sync_runs += 1
            try:
                count = await sync_once()
                stats.recordings_synced += count
                if count:
                    console.print(f"[bold green]Synced {count} new recording(s).[/bold green]")
                else:
                    console.print("[dim]Nothing new.[/dim]")
            except Exception as e:
                stats.failures += 1
                console.print(f"[yellow]Sync failed: {e}[/yellow]")
            # Clear the backoff so the next miss starts from the base
            # interval again rather than from wherever it had grown to.
            backoff = 0.0
            await sleep(poll_interval)
        else:
            backoff = next_backoff(backoff, poll_interval)
            console.print(
                f"[dim]Device not found. Next check in {backoff:.0f}s.[/dim]"
            )
            await sleep(backoff)

    return stats


@dataclass
class WatchTarget:
    """One recorder to watch: a profile name, its address, and how to sync it."""

    name: str
    address: str
    sync_once: Callable[[], Awaitable[int]]


async def watch_many(
    targets: list[WatchTarget],
    poll_interval: float = 60.0,
    presence_check: Callable[[str], Awaitable[bool]] | None = None,
    max_iterations: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, WatchStats]:
    """Poll several recorders, one at a time, and sync whichever is present.

    Strictly sequential: the Pocket accepts a single BLE connection, and one
    adapter cannot scan for two devices at once. Each target keeps its own
    backoff, so one recorder being away all day does not slow down the scan
    rate of the one on the desk.

    Time only advances through `sleep`, which the caller can replace to drive
    the loop deterministically in tests.
    """
    check = presence_check or device_present
    stats = {t.name: WatchStats() for t in targets}
    backoff = {t.name: 0.0 for t in targets}
    due = {t.name: 0.0 for t in targets}
    now = 0.0
    rounds = 0

    while targets and (max_iterations is None or rounds < max_iterations):
        rounds += 1
        ready = [t for t in targets if due[t.name] <= now]

        for target in ready:
            current = stats[target.name]
            current.scans += 1
            if await check(target.address):
                console.print(f"[green]{target.name}: device found — syncing...[/green]")
                current.sync_runs += 1
                try:
                    count = await target.sync_once()
                    current.recordings_synced += count
                    if count:
                        console.print(
                            f"[bold green]{target.name}: synced {count} new "
                            "recording(s).[/bold green]"
                        )
                    else:
                        console.print(f"[dim]{target.name}: nothing new.[/dim]")
                except Exception as e:
                    current.failures += 1
                    console.print(f"[yellow]{target.name}: sync failed: {e}[/yellow]")
                # Clear the backoff so the next miss starts from the base
                # interval again rather than from wherever it had grown to.
                backoff[target.name] = 0.0
                due[target.name] = now + poll_interval
            else:
                backoff[target.name] = next_backoff(backoff[target.name], poll_interval)
                due[target.name] = now + backoff[target.name]
                console.print(
                    f"[dim]{target.name}: not found. "
                    f"Next check in {backoff[target.name]:.0f}s.[/dim]"
                )

        step = max(min(due.values()) - now, 0.0)
        await sleep(step)
        now += step

    return stats


async def sync_new_recordings(
    address: str,
    session_key: str,
    out_root: Path,
    process: bool = False,
    whisper_model: str = "base.en",
    summary_style: str = "meeting",
    anthropic_key: str | None = None,
    hf_token: str | None = None,
    backend_options: dict | None = None,
) -> int:
    """Download every recording not already on disk. Returns the count."""
    from pocket_libre.commands import PocketCommander, download_with_retry
    from pocket_libre.protocol import MP3_SYNC_WORD

    async with PocketCommander(address) as cmd:
        if not await cmd.authenticate(session_key):
            raise RuntimeError("Authentication failed.")
        all_recs = await cmd.list_all_recordings()

    new_recs = [
        r for r in all_recs
        if not (out_root / r.date / f"{r.timestamp}.mp3").exists()
    ]
    if not new_recs:
        return 0

    synced = 0
    for rec in new_recs:
        rec_dir = out_root / rec.date
        rec_dir.mkdir(parents=True, exist_ok=True)
        audio_path = rec_dir / f"{rec.timestamp}.mp3"

        console.print(f"[dim]Downloading {rec.date}/{rec.timestamp}...[/dim]")
        data = await download_with_retry(address, session_key, rec)
        if not data:
            continue

        start = data.find(MP3_SYNC_WORD)
        if start > 0:
            data = data[start:]
        audio_path.write_bytes(data)
        synced += 1

        if process:
            try:
                _process_recording(
                    audio_path, rec.timestamp, rec_dir,
                    whisper_model, summary_style, anthropic_key, hf_token,
                    backend_options, out_root,
                )
            except Exception as e:
                console.print(f"[yellow]Processing failed for {rec.timestamp}: {e}[/yellow]")

    return synced


def _process_recording(
    audio_path: Path,
    timestamp: str,
    rec_dir: Path,
    whisper_model: str,
    summary_style: str,
    anthropic_key: str | None,
    hf_token: str | None,
    backend_options: dict | None = None,
    library_dir: Path | None = None,
) -> None:
    """Transcribe, diarize, and summarize one downloaded recording."""
    from pocket_libre.backends import transcribe_and_label
    from pocket_libre.summarize import format_transcript_for_summary

    options = dict(backend_options or {})
    options.setdefault("model", whisper_model)
    labeled, _result = transcribe_and_label(
        audio_path, hf_token=hf_token, anthropic_key=anthropic_key,
        library_dir=library_dir,
        voices_path=rec_dir / f"{timestamp}_voices.json",
        **options,
    )

    transcript_text = format_transcript_for_summary(labeled)
    (rec_dir / f"{timestamp}_transcript.txt").write_text(transcript_text, encoding="utf-8")

    if not anthropic_key:
        return

    from datetime import datetime

    from pocket_libre.summarize import summarize_transcript

    summary = summarize_transcript(
        transcript_text=transcript_text, api_key=anthropic_key, style=summary_style,
    )
    if summary:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        doc = (
            f"# {timestamp} ({stamp})\n\n{summary}\n\n---\n\n"
            f"## Full Transcript\n\n{transcript_text}"
        )
        (rec_dir / f"{timestamp}_summary.md").write_text(doc, encoding="utf-8")
