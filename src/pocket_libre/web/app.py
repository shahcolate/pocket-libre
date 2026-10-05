"""FastAPI web interface for Pocket Libre."""

import asyncio
import functools
import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel

from pocket_libre.backends import options_from_config, transcribe_and_label
from pocket_libre.commands import PocketCommander, Recording
from pocket_libre.config import (
    PROFILES_SECTION,
    effective_config,
    get,
    get_output_dir,
    load_config,
    profile_accent,
    profile_key_for,
    profile_label,
    resolve_address,
    resolve_anthropic_key,
    resolve_hf_token,
    resolve_session_key,
    resolve_web_port,
    save_config,
)
from pocket_libre.protocol import MP3_SYNC_WORD

app = FastAPI(title="Pocket Libre")

STATIC_DIR = Path(__file__).parent / "static"
ble_lock = asyncio.Lock()

# Set once, before the server starts serving. Every request resolves against
# it, so a server raised for one profile has no route to another profile's
# recordings: the config it sees describes exactly one device and one library.
_active_profile: str | None = None


def set_active_profile(name: str | None) -> None:
    """Bind this server to one profile. Call before serving."""
    global _active_profile
    _active_profile = name


def active_profile_name() -> str | None:
    """Which profile this server is bound to, or None for a single-device config."""
    return _active_profile


def current_config() -> dict:
    """The config as this server sees it: the active profile, folded down."""
    return effective_config(load_config(), _active_profile)


# ── Path safety ─────────────────────────────────
#
# `date` and `timestamp` arrive as URL path parameters and are interpolated
# into filesystem paths. Starlette percent-decodes path parameters *after*
# routing, so a request for `/api/local/%2e%2e/x/transcript` yields
# date == ".." and escapes the output directory. Validate every component
# and re-check containment on the resolved path.

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_component(value: str, field: str) -> str:
    """Reject anything that could traverse out of the output directory."""
    if not value or not _SAFE_COMPONENT.match(value) or value in (".", ".."):
        raise HTTPException(400, f"Invalid {field}.")
    return value


def _recording_path(out_root: Path, date: str, timestamp: str, suffix: str) -> Path:
    """Build a path under out_root, refusing anything that escapes it."""
    _safe_component(date, "date")
    _safe_component(timestamp, "timestamp")
    root = out_root.resolve()
    candidate = (root / date / f"{timestamp}{suffix}").resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(400, "Path outside output directory.")
    return candidate


# ── Static Files ────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── Config ──────────────────────────────────────


class ConfigUpdate(BaseModel):
    device: dict | None = None
    api: dict | None = None
    output: dict | None = None
    defaults: dict | None = None


@app.get("/api/config")
async def get_config():
    config = current_config()
    # Mask sensitive keys
    safe = {}
    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        safe[section] = {}
        for k, v in values.items():
            # Always mask secrets, however short. The old `len > 8` guard
            # returned short keys verbatim.
            if k in ("anthropic_key", "hf_token", "session_key") and v:
                text = str(v)
                safe[section][k] = f"...{text[-4:]}" if len(text) > 4 else "..."
                safe[section][f"_{k}_set"] = True
            else:
                safe[section][k] = v
    return safe


@app.get("/api/profile")
async def get_profile():
    """Who this server belongs to, for the header, the title and the accent.

    Two identical tabs on adjacent ports is how someone ends up reading the
    wrong person's transcript, so the UI always says whose library it is.
    """
    config = load_config()
    name = _active_profile
    return {
        "name": name,
        "label": profile_label(config, name),
        "accent": profile_accent(config, name) if name else None,
        "library": get_output_dir(config, profile=name),
        "port": resolve_web_port(config, profile=name),
    }


@app.put("/api/config")
async def update_config(update: ConfigUpdate):
    # Written against the file as stored, never the folded view: saving that
    # would flatten this profile over the globals and drop the others. With a
    # profile active, every edit lands in that profile's own table.
    config = load_config()
    for section in ("device", "api", "output", "defaults"):
        new_vals = getattr(update, section)
        if not new_vals:
            continue
        if _active_profile:
            table = (config.setdefault(PROFILES_SECTION, {})
                     .setdefault(_active_profile, {}))
        else:
            table = config.setdefault(section, {})
        for k, v in new_vals.items():
            # Don't overwrite keys with masked values
            if isinstance(v, str) and v.startswith("..."):
                continue
            table[profile_key_for(section, k) if _active_profile else k] = v
    save_config(config)
    return {"status": "ok"}


def _require_device(config: dict) -> tuple[str, str]:
    """Resolve device address and session key, or raise 400 pointing to Settings."""
    address = resolve_address(config)
    if not address:
        raise HTTPException(400, "No device address configured. Go to Settings.")
    sk = resolve_session_key(config)
    if not sk:
        raise HTTPException(400, "No session key configured. Go to Settings.")
    return address, sk


# ── Device Scan & Status ────────────────────────


@app.get("/api/scan")
async def scan_for_devices():
    """Scan for Pocket devices over BLE."""
    try:
        from bleak import BleakScanner
        devices = await BleakScanner.discover(timeout=5.0, return_adv=True)
        results = []
        for d, adv in devices.values():
            if d.name and "pkt" in d.name.lower():
                rssi = adv.rssi if adv else None
                results.append({"name": d.name, "address": d.address, "rssi": rssi})
        return results
    except Exception as e:
        raise HTTPException(502, f"Scan failed: {e}") from e


@app.get("/api/device/busy")
async def device_busy():
    """Check if device is busy with a BLE operation."""
    return {"busy": ble_lock.locked()}


# ── Device Endpoints ────────────────────────────


@app.get("/api/device/status")
async def device_status():
    config = current_config()
    address, sk = _require_device(config)

    if ble_lock.locked():
        raise HTTPException(409, "Device is busy with another operation.")

    async with ble_lock:
        try:
            async with PocketCommander(address) as cmd:
                ok = await cmd.authenticate(sk)
                if not ok:
                    raise HTTPException(401, "Authentication failed.")

                battery = await cmd.get_battery()
                firmware = await cmd.get_firmware()
                used, total = await cmd.get_storage()
                state = await cmd.get_state()
                await cmd.set_time()

                return {
                    "battery": battery,
                    "firmware": firmware,
                    "storage_used_mb": used,
                    "storage_total_mb": total,
                    "state": state,
                    "state_name": {0: "Idle", 1: "Recording"}.get(state, f"Unknown ({state})"),
                    "address": address,
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Connection failed: {e}") from e


@app.get("/api/device/recordings")
async def device_recordings():
    config = current_config()
    address, sk = _require_device(config)

    if ble_lock.locked():
        raise HTTPException(409, "Device is busy.")

    async with ble_lock:
        try:
            async with PocketCommander(address) as cmd:
                if not await cmd.authenticate(sk):
                    raise HTTPException(401, "Auth failed.")

                all_recs = await cmd.list_all_recordings()
                return [
                    {
                        "date": r.date,
                        "timestamp": r.timestamp,
                        "duration_s": r.duration_s,
                        "estimated_bytes": r.estimated_bytes,
                        "duration_estimate": f"{max(r.duration_s, 0) // 60}m{max(r.duration_s, 0) % 60:02d}s",
                    }
                    for r in all_recs
                ]
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Connection failed: {e}") from e


# ── Download & Process (SSE) ────────────────────


@app.get("/api/download/{date}/{timestamp}")
async def download_recording(date: str, timestamp: str):
    """Download a recording over BLE with SSE progress updates."""
    config = current_config()
    address, sk = _require_device(config)
    out_root = Path(get_output_dir(config))
    out_path = _recording_path(out_root, date, timestamp, ".mp3")

    if ble_lock.locked():
        raise HTTPException(409, "Device is busy.")

    async def event_stream():
        async with ble_lock:
            try:
                yield _sse({"step": "connect", "message": "Connecting to device..."})

                async with PocketCommander(address) as cmd:
                    if not await cmd.authenticate(sk):
                        yield _sse({"step": "error", "message": "Authentication failed."})
                        return

                    rec = Recording(date=date, timestamp=timestamp, duration_s=0)
                    yield _sse({"step": "download", "message": f"Downloading {date}/{timestamp}...", "progress": 0})

                    queue = asyncio.Queue()

                    def progress_cb(current, total):
                        pct = 100 * current // total if total > 0 else 0
                        queue.put_nowait({"step": "download", "progress": pct, "current": current, "total": total})

                    download_task = asyncio.create_task(
                        cmd.download_ble(rec, progress_callback=progress_cb)
                    )

                    while not download_task.done():
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=1.0)
                            yield _sse(event)
                        except asyncio.TimeoutError:
                            pass

                    data = download_task.result()

                    if not data:
                        yield _sse({"step": "error", "message": "No data received."})
                        return

                    mp3_start = data.find(MP3_SYNC_WORD)
                    if mp3_start > 0:
                        data = data[mp3_start:]

                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(data)

                    yield _sse({
                        "step": "complete",
                        "message": f"Saved {len(data):,} bytes",
                        "path": str(out_path),
                        "size": len(data),
                    })

            except Exception as e:
                yield _sse({"step": "error", "message": str(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/process/{date}/{timestamp}")
async def process_recording(date: str, timestamp: str):
    """Download, transcribe, and summarize with SSE progress."""
    config = current_config()
    out_root = Path(get_output_dir(config))
    audio_path = _recording_path(out_root, date, timestamp, ".mp3")
    # Device access is only needed when the audio isn't on disk yet;
    # reprocessing a downloaded file must work without a session key.
    needs_download = not audio_path.exists()
    if needs_download:
        address, sk = _require_device(config)
    else:
        address = sk = None
    whisper_model = get(config, "defaults", "whisper_model", default="base.en")
    summary_style = get(config, "defaults", "summary_style", default="meeting")
    anthropic_key = resolve_anthropic_key(config)
    hf_token = resolve_hf_token(config)

    async def event_stream():
        rec_dir = audio_path.parent
        rec_dir.mkdir(parents=True, exist_ok=True)

        # Download if not already on disk
        if needs_download:
            if ble_lock.locked():
                yield _sse({"step": "error", "message": "Device is busy."})
                return

            async with ble_lock:
                try:
                    yield _sse({"step": "connect", "message": "Connecting..."})
                    async with PocketCommander(address) as cmd:
                        if not await cmd.authenticate(sk):
                            yield _sse({"step": "error", "message": "Auth failed."})
                            return

                        rec = Recording(date=date, timestamp=timestamp, duration_s=0)
                        yield _sse({"step": "download", "message": "Downloading...", "progress": 0})

                        queue = asyncio.Queue()
                        def progress_cb(current, total):
                            pct = 100 * current // total if total > 0 else 0
                            queue.put_nowait(pct)

                        task = asyncio.create_task(cmd.download_ble(rec, progress_callback=progress_cb))
                        while not task.done():
                            try:
                                pct = await asyncio.wait_for(queue.get(), timeout=1.0)
                                yield _sse({"step": "download", "progress": pct})
                            except asyncio.TimeoutError:
                                pass

                        data = task.result()
                        if not data:
                            yield _sse({"step": "error", "message": "No data received."})
                            return

                        mp3_start = data.find(MP3_SYNC_WORD)
                        if mp3_start > 0:
                            data = data[mp3_start:]
                        audio_path.write_bytes(data)
                        yield _sse({"step": "download", "progress": 100, "message": f"Downloaded {len(data):,} bytes"})

                except Exception as e:
                    yield _sse({"step": "error", "message": str(e)})
                    return
        else:
            yield _sse({"step": "download", "progress": 100, "message": "Already downloaded"})

        # Transcribe and attach speakers (in a thread, so the loop keeps serving)
        options = options_from_config(config, whisper_model)
        yield _sse({"step": "transcribe",
                    "message": f"Transcribing ({options['backend']}, {options['model']})..."})

        try:
            loop = asyncio.get_event_loop()
            labeled, transcription = await loop.run_in_executor(
                None,
                functools.partial(
                    transcribe_and_label, audio_path,
                    hf_token=hf_token, anthropic_key=anthropic_key,
                    library_dir=out_root,
                    voices_path=rec_dir / f"{timestamp}_voices.json",
                    **options,
                ),
            )
        except Exception as e:
            yield _sse({"step": "error", "message": f"Transcription failed: {e}"})
            return

        detected = f" ({transcription.language})" if transcription.language else ""
        yield _sse({"step": "transcribe",
                    "message": f"Transcribed: {len(labeled)} segments{detected}"})
        yield _sse({"step": "diarize",
                    "message": "Speakers: " + (", ".join(transcription.speakers) or "one")})

        from pocket_libre.summarize import format_transcript_for_summary
        transcript_text = format_transcript_for_summary(labeled)

        transcript_path = rec_dir / f"{timestamp}_transcript.txt"
        transcript_path.write_text(transcript_text, encoding="utf-8")
        yield _sse({"step": "diarize", "message": "Transcript saved"})

        # Summarize
        if anthropic_key:
            yield _sse({"step": "summarize", "message": "Summarizing with Claude..."})
            try:
                from pocket_libre.summarize import summarize_transcript
                summary = await loop.run_in_executor(
                    None,
                    lambda: summarize_transcript(
                        transcript_text=transcript_text,
                        api_key=anthropic_key,
                        style=summary_style,
                    ),
                )
                if summary:
                    summary_path = rec_dir / f"{timestamp}_summary.md"
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                    full_doc = f"# {timestamp} ({ts})\n\n{summary}\n\n---\n\n## Full Transcript\n\n{transcript_text}"
                    summary_path.write_text(full_doc, encoding="utf-8")
                    yield _sse({"step": "summarize", "message": "Summary saved"})
            except Exception as e:
                yield _sse({"step": "summarize", "message": f"Summary failed: {e}"})
        else:
            yield _sse({"step": "summarize", "message": "Skipped (no API key configured)"})

        yield _sse({"step": "complete", "message": "Processing complete!", "path": str(rec_dir)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Sync All ───────────────────────────────────


@app.get("/api/sync-all")
async def sync_all():
    """Download all new recordings, transcribe, and summarize. SSE progress."""
    config = current_config()
    address, sk = _require_device(config)
    out_root = Path(get_output_dir(config))
    whisper_model = get(config, "defaults", "whisper_model", default="base.en")
    options = options_from_config(config, whisper_model)
    summary_style = get(config, "defaults", "summary_style", default="meeting")
    anthropic_key = resolve_anthropic_key(config)
    hf_token = resolve_hf_token(config)

    if ble_lock.locked():
        raise HTTPException(409, "Device is busy.")

    async def event_stream():
        from pocket_libre.protocol import MP3_SYNC_WORD

        # Single BLE connection for listing + all downloads
        downloaded = []  # list of (rec, audio_path, data) tuples
        async with ble_lock:
            try:
                yield _sse({"step": "scan", "message": "Connecting to device..."})
                async with PocketCommander(address) as cmd:
                    if not await cmd.authenticate(sk):
                        yield _sse({"step": "error", "message": "Authentication failed."})
                        return

                    all_recs = await cmd.list_all_recordings()

                    if not all_recs:
                        yield _sse({"step": "complete", "message": "No recordings on device.", "new_count": 0})
                        return

                    # Determine which are new
                    new_recs = []
                    for rec in all_recs:
                        mp3_path = out_root / rec.date / f"{rec.timestamp}.mp3"
                        if not mp3_path.exists():
                            new_recs.append(rec)

                    if not new_recs:
                        yield _sse({"step": "complete", "message": f"All {len(all_recs)} recordings already synced.", "new_count": 0})
                        return

                    yield _sse({"step": "scan", "message": f"Found {len(all_recs)} recordings, {len(new_recs)} new"})

                    # Download all new recordings on the same connection
                    for i, rec in enumerate(new_recs, 1):
                        rec_dir = out_root / rec.date
                        rec_dir.mkdir(parents=True, exist_ok=True)
                        audio_path = rec_dir / f"{rec.timestamp}.mp3"

                        yield _sse({
                            "step": "download", "recording": i, "total": len(new_recs),
                            "name": f"{rec.date}/{rec.timestamp}", "progress": 0,
                        })

                        try:
                            data = await cmd.download_ble(rec, progress_callback=lambda cur, tot, _i=i, _t=len(new_recs): None)

                            if not data:
                                yield _sse({"step": "error", "message": f"Empty download for {rec.timestamp}"})
                                continue

                            # Trim to MP3 sync word
                            mp3_start = data.find(MP3_SYNC_WORD)
                            if mp3_start > 0:
                                data = data[mp3_start:]

                            audio_path.write_bytes(data)
                            downloaded.append((rec, audio_path))
                            yield _sse({"step": "download", "recording": i, "total": len(new_recs), "progress": 100,
                                        "message": f"Downloaded {len(data):,} bytes"})

                            # Brief pause between downloads to let device settle
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            yield _sse({"step": "error", "message": f"Download error for {rec.timestamp}: {e}"})
                            # Connection may be dead — break out and process what we have
                            break

            except Exception as e:
                yield _sse({"step": "error", "message": f"Connection failed: {e}"})
                if not downloaded:
                    return

        if not downloaded:
            yield _sse({"step": "complete", "message": "No recordings downloaded.", "new_count": 0})
            return

        # Phase 2: Process downloaded recordings (BLE lock released)
        total = len(downloaded)
        for i, (rec, audio_path) in enumerate(downloaded, 1):
            rec_dir = audio_path.parent

            # Transcribe and attach speakers
            yield _sse({"step": "transcribe", "recording": i, "total": total,
                        "message": f"Transcribing ({options['backend']})..."})
            try:
                loop = asyncio.get_event_loop()
                labeled, transcription = await loop.run_in_executor(
                    None,
                    functools.partial(
                        transcribe_and_label, audio_path,
                        hf_token=hf_token, anthropic_key=anthropic_key,
                        library_dir=out_root,
                        voices_path=rec_dir / f"{rec.timestamp}_voices.json",
                        **options,
                    ),
                )
                detected = f" ({transcription.language})" if transcription.language else ""
                yield _sse({"step": "transcribe", "recording": i, "total": total,
                            "message": f"{len(labeled)} segments{detected}"})
            except Exception as e:
                yield _sse({"step": "transcribe", "recording": i, "total": total,
                            "message": f"Failed: {e}"})
                continue

            from pocket_libre.summarize import format_transcript_for_summary
            transcript_text = format_transcript_for_summary(labeled)
            transcript_path = rec_dir / f"{rec.timestamp}_transcript.txt"
            transcript_path.write_text(transcript_text, encoding="utf-8")

            # Summarize
            if anthropic_key:
                yield _sse({"step": "summarize", "recording": i, "total": total, "message": "Summarizing..."})
                try:
                    from pocket_libre.summarize import summarize_transcript
                    summary = await loop.run_in_executor(
                        None,
                        lambda t=transcript_text: summarize_transcript(
                            transcript_text=t, api_key=anthropic_key, style=summary_style
                        ),
                    )
                    if summary:
                        summary_path = rec_dir / f"{rec.timestamp}_summary.md"
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                        full_doc = f"# {rec.timestamp} ({ts})\n\n{summary}\n\n---\n\n## Full Transcript\n\n{transcript_text}"
                        summary_path.write_text(full_doc, encoding="utf-8")
                        yield _sse({"step": "summarize", "recording": i, "total": total, "message": "Done"})
                except Exception as e:
                    yield _sse({"step": "summarize", "recording": i, "total": total, "message": f"Failed: {e}"})
            else:
                yield _sse({"step": "summarize", "recording": i, "total": total, "message": "Skipped (no API key)"})

            # Run AI analyses (entities, mind map, etc.)
            if anthropic_key:
                enabled_str = get(config, "analysis", "enabled", default="summary,entities")
                analysis_types = [t.strip() for t in enabled_str.split(",") if t.strip() and t.strip() != "summary"]
                if analysis_types:
                    yield _sse({"step": "analyze", "recording": i, "total": total, "message": f"Running {', '.join(analysis_types)}..."})
                    try:
                        from pocket_libre.analyze import run_analyses, save_analyses
                        results = await loop.run_in_executor(
                            None,
                            lambda t=transcript_text, types=analysis_types: run_analyses(
                                t, anthropic_key, types
                            ),
                        )
                        if results:
                            save_analyses(results, rec_dir, rec.timestamp)
                            yield _sse({"step": "analyze", "recording": i, "total": total,
                                        "message": f"Done: {', '.join(results.keys())}"})
                    except Exception as e:
                        yield _sse({"step": "analyze", "recording": i, "total": total, "message": f"Failed: {e}"})

        yield _sse({"step": "complete", "message": f"Synced {total} recording(s)", "new_count": total})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Local Library ───────────────────────────────


@app.get("/api/local/recordings")
async def local_recordings():
    config = current_config()
    out_root = Path(get_output_dir(config))

    if not out_root.exists():
        return []

    recordings = []
    for date_dir in sorted(out_root.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for mp3 in sorted(date_dir.glob("*.mp3"), reverse=True):
            stem = mp3.stem
            has_transcript = (date_dir / f"{stem}_transcript.txt").exists()
            has_summary = (date_dir / f"{stem}_summary.md").exists()
            has_entities = (date_dir / f"{stem}_entities.json").exists()
            has_mind_map = (date_dir / f"{stem}_mind_map.json").exists()
            recordings.append({
                "date": date_dir.name,
                "timestamp": stem,
                "size_bytes": mp3.stat().st_size,
                "has_transcript": has_transcript,
                "has_summary": has_summary,
                "has_entities": has_entities,
                "has_mind_map": has_mind_map,
                "session_id": f"{date_dir.name}/{stem}",
            })

    return recordings


@app.get("/api/search")
async def search_recordings(q: str = "", limit: int = 20, kind: str = ""):
    """Full-text search across this profile's library.

    The index lives inside the library, so a search cannot reach another
    profile's recordings.
    """
    from pocket_libre.index import search

    config = current_config()
    out_root = Path(get_output_dir(config))
    if not q.strip() or not out_root.is_dir():
        return []

    kinds = tuple(k for k in kind.split(",") if k.strip()) or None
    hits = search(out_root, q, limit=max(1, min(int(limit), 100)), kinds=kinds)
    return [
        {"date": hit.date, "timestamp": hit.timestamp, "kind": hit.kind,
         "snippet": hit.snippet, "session_id": hit.reference}
        for hit in hits
    ]


@app.get("/api/local/{date}/{timestamp}/transcript")
async def get_transcript(date: str, timestamp: str):
    config = current_config()
    path = _recording_path(Path(get_output_dir(config)), date, timestamp, "_transcript.txt")
    if not path.exists():
        raise HTTPException(404, "Transcript not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/local/{date}/{timestamp}/summary")
async def get_summary(date: str, timestamp: str):
    config = current_config()
    path = _recording_path(Path(get_output_dir(config)), date, timestamp, "_summary.md")
    if not path.exists():
        raise HTTPException(404, "Summary not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/local/{date}/{timestamp}/audio")
async def get_audio(date: str, timestamp: str):
    config = current_config()
    path = _recording_path(Path(get_output_dir(config)), date, timestamp, ".mp3")
    if not path.exists():
        raise HTTPException(404, "Audio file not found")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{timestamp}.mp3")


# ── Chat & Analysis ─────────────────────────────


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat/{date}/{timestamp}")
async def chat_recording(date: str, timestamp: str, body: ChatRequest):
    """Ask a question about a recording's transcript."""
    config = current_config()
    anthropic_key = resolve_anthropic_key(config)
    if not anthropic_key:
        raise HTTPException(400, "No Anthropic API key configured. Add one in Settings.")

    out_root = Path(get_output_dir(config))
    transcript_path = _recording_path(out_root, date, timestamp, "_transcript.txt")
    if not transcript_path.exists():
        raise HTTPException(404, "Transcript not found. Process this recording first.")

    transcript = transcript_path.read_text(encoding="utf-8")

    from pocket_libre.analyze import chat_with_recording
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: chat_with_recording(transcript, body.message, anthropic_key),
    )
    return {"response": response}


@app.get("/api/local/{date}/{timestamp}/analyses")
async def get_analyses(date: str, timestamp: str):
    """Get all analysis results for a recording."""
    config = current_config()
    rec_dir = _recording_path(Path(get_output_dir(config)), date, timestamp, "").parent
    if not rec_dir.exists():
        raise HTTPException(404, "Recording not found")

    from pocket_libre.analyze import load_analyses
    results = load_analyses(rec_dir, timestamp)
    return results


@app.post("/api/analyze/{date}/{timestamp}")
async def run_analysis(date: str, timestamp: str):
    """Run AI analyses on an already-transcribed recording."""
    config = current_config()
    anthropic_key = resolve_anthropic_key(config)
    if not anthropic_key:
        raise HTTPException(400, "No Anthropic API key configured.")

    out_root = Path(get_output_dir(config))
    transcript_path = _recording_path(out_root, date, timestamp, "_transcript.txt")
    rec_dir = transcript_path.parent
    if not transcript_path.exists():
        raise HTTPException(404, "Transcript not found. Process this recording first.")

    transcript = transcript_path.read_text(encoding="utf-8")

    # Get enabled analyses from config
    enabled_str = get(config, "analysis", "enabled", default="summary,entities")
    enabled = [t.strip() for t in enabled_str.split(",") if t.strip()]
    # Remove "summary" — it's handled separately
    analysis_types = [t for t in enabled if t != "summary"]

    from pocket_libre.analyze import run_analyses, save_analyses
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None,
        lambda: run_analyses(transcript, anthropic_key, analysis_types),
    )

    if results:
        save_analyses(results, rec_dir, timestamp)

    return {"analyses": list(results.keys()), "count": len(results)}


# ── Helpers ─────────────────────────────────────


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
