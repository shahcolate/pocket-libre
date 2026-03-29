"""FastAPI web interface for Pocket Libre."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pocket_libre.commands import PocketCommander, Recording
from pocket_libre.config import (
    load_config, save_config,
    resolve_address, resolve_session_key,
    resolve_anthropic_key, resolve_hf_token,
    get_output_dir, get,
)
from pocket_libre.protocol import MP3_SYNC_WORD

app = FastAPI(title="Pocket Libre")

STATIC_DIR = Path(__file__).parent / "static"
ble_lock = asyncio.Lock()


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
    config = load_config()
    # Mask sensitive keys
    safe = {}
    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        safe[section] = {}
        for k, v in values.items():
            if k in ("anthropic_key", "hf_token") and v and len(str(v)) > 8:
                safe[section][k] = f"...{str(v)[-4:]}"
                safe[section][f"_{k}_set"] = True
            else:
                safe[section][k] = v
    return safe


@app.put("/api/config")
async def update_config(update: ConfigUpdate):
    config = load_config()
    for section in ("device", "api", "output", "defaults"):
        new_vals = getattr(update, section)
        if new_vals:
            if section not in config:
                config[section] = {}
            for k, v in new_vals.items():
                # Don't overwrite keys with masked values
                if isinstance(v, str) and v.startswith("..."):
                    continue
                config[section][k] = v
    save_config(config)
    return {"status": "ok"}


# ── Device Endpoints ────────────────────────────


@app.get("/api/device/status")
async def device_status():
    config = load_config()
    address = resolve_address(config)
    if not address:
        raise HTTPException(400, "No device address configured. Go to Settings.")
    sk = resolve_session_key(config)

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
                    "storage_used_kb": used,
                    "storage_total_kb": total,
                    "state": state,
                    "state_name": {0: "Idle", 1: "Recording"}.get(state, f"Unknown ({state})"),
                    "address": address,
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Connection failed: {e}")


@app.get("/api/device/recordings")
async def device_recordings():
    config = load_config()
    address = resolve_address(config)
    if not address:
        raise HTTPException(400, "No device address configured.")
    sk = resolve_session_key(config)

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
                        "size_kb": r.size_kb,
                        "duration_estimate": f"{r.size_kb * 1024 // 4000 // 60}m{r.size_kb * 1024 // 4000 % 60:02d}s" if r.size_kb > 0 else "0m00s",
                    }
                    for r in all_recs
                ]
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Connection failed: {e}")


# ── Download & Process (SSE) ────────────────────


@app.get("/api/download/{date}/{timestamp}")
async def download_recording(date: str, timestamp: str):
    """Download a recording over BLE with SSE progress updates."""
    config = load_config()
    address = resolve_address(config)
    if not address:
        raise HTTPException(400, "No device address configured.")
    sk = resolve_session_key(config)
    out_root = Path(get_output_dir(config))

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

                    rec = Recording(date=date, timestamp=timestamp, size_kb=0)
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

                    rec_dir = out_root / date
                    rec_dir.mkdir(parents=True, exist_ok=True)
                    out_path = rec_dir / f"{timestamp}.mp3"
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
    config = load_config()
    address = resolve_address(config)
    if not address:
        raise HTTPException(400, "No device address configured.")
    sk = resolve_session_key(config)
    out_root = Path(get_output_dir(config))
    whisper_model = get(config, "defaults", "whisper_model", default="base.en")
    summary_style = get(config, "defaults", "summary_style", default="meeting")
    anthropic_key = resolve_anthropic_key(config)
    hf_token = resolve_hf_token(config)

    async def event_stream():
        rec_dir = out_root / date
        rec_dir.mkdir(parents=True, exist_ok=True)
        audio_path = rec_dir / f"{timestamp}.mp3"

        # Download if not already on disk
        if not audio_path.exists():
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

                        rec = Recording(date=date, timestamp=timestamp, size_kb=0)
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

        # Transcribe (runs in thread to not block)
        yield _sse({"step": "transcribe", "message": f"Loading Whisper ({whisper_model})..."})

        try:
            import whisper
            loop = asyncio.get_event_loop()
            model = await loop.run_in_executor(None, whisper.load_model, whisper_model)
            yield _sse({"step": "transcribe", "message": "Transcribing..."})
            result = await loop.run_in_executor(None, lambda: model.transcribe(str(audio_path), verbose=False))
            segments = result.get("segments", [])
            yield _sse({"step": "transcribe", "message": f"Transcribed: {len(segments)} segments"})
        except ImportError:
            yield _sse({"step": "error", "message": "Whisper not installed. Run: pip install openai-whisper"})
            return
        except Exception as e:
            yield _sse({"step": "error", "message": f"Transcription failed: {e}"})
            return

        # Diarize
        yield _sse({"step": "diarize", "message": "Identifying speakers..."})
        try:
            from pocket_libre.diarize import diarize_pyannote, diarize_simple, merge_transcript_with_speakers

            speaker_segments = []
            if hf_token:
                try:
                    speaker_segments = await loop.run_in_executor(
                        None, diarize_pyannote, str(audio_path), hf_token
                    )
                except Exception:
                    pass

            if not speaker_segments:
                speaker_segments = diarize_simple(segments)

            labeled = merge_transcript_with_speakers(segments, speaker_segments)
        except Exception:
            labeled = [{"start": s["start"], "end": s["end"], "speaker": "Speaker", "text": s["text"]} for s in segments]

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


# ── Local Library ───────────────────────────────


@app.get("/api/local/recordings")
async def local_recordings():
    config = load_config()
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
            recordings.append({
                "date": date_dir.name,
                "timestamp": stem,
                "size_bytes": mp3.stat().st_size,
                "has_transcript": has_transcript,
                "has_summary": has_summary,
                "session_id": f"{date_dir.name}/{stem}",
            })

    return recordings


@app.get("/api/local/{date}/{timestamp}/transcript")
async def get_transcript(date: str, timestamp: str):
    config = load_config()
    path = Path(get_output_dir(config)) / date / f"{timestamp}_transcript.txt"
    if not path.exists():
        raise HTTPException(404, "Transcript not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/local/{date}/{timestamp}/summary")
async def get_summary(date: str, timestamp: str):
    config = load_config()
    path = Path(get_output_dir(config)) / date / f"{timestamp}_summary.md"
    if not path.exists():
        raise HTTPException(404, "Summary not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/local/{date}/{timestamp}/audio")
async def get_audio(date: str, timestamp: str):
    config = load_config()
    path = Path(get_output_dir(config)) / date / f"{timestamp}.mp3"
    if not path.exists():
        raise HTTPException(404, "Audio file not found")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{timestamp}.mp3")


# ── Helpers ─────────────────────────────────────


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
