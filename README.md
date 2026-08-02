# Pocket Libre

**Liberate your Pocket AI recorder from the cloud.**

Pocket Libre replaces the vendor app for your [Pocket](https://heypocket.com) AI voice recorder. Download recordings directly over Bluetooth, transcribe locally with Whisper, identify speakers, and summarize with your own API keys. No vendor cloud. Your conversations stay on your machine.

## Quick Start

### 1. Install

```bash
git clone https://github.com/shahcolate/pocket-libre.git
cd pocket-libre
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

> **For speaker identification** (optional): `pip install -e ".[diarize]"` (requires PyTorch)

### 2. Get your session key

Pocket Libre authenticates with the same 16-character session key the official app
writes over BLE (`APP&SK&…`). Capture it once:

1. Install Apple’s **Bluetooth for iOS** logging profile + **PacketLogger** (Mac),
   **or** enable Android **Bluetooth HCI snoop**.
2. Connect/sync with the **official Pocket app** while capturing.
3. Search the capture for `APP&SK&` — the next **16 characters** are your key.
4. Confirm you also see `MCU&SK&OK`.

Full steps and PacketLogger framing pitfalls: [PROTOCOL.md](PROTOCOL.md).

> `pocket-libre sniff` cannot see the key — it only watches notifications on its own connection.

### 3. Setup

```bash
pocket-libre setup
```

The wizard walks you through device address (BLE scan), session key, optional API keys,
output directory, Whisper model, **sync mode**, and **process mode**.

Settings live in `~/.pocket-libre/config.toml`.

### 4. Use

**Web interface** (recommended):
```bash
pocket-libre web
```
Opens `http://127.0.0.1:8265` — status, device file list, download/process/delete, library, settings.

**Command line**:
```bash
pocket-libre status              # Battery, storage, firmware
pocket-libre list                # Recordings on device
pocket-libre sync                # Honors sync_mode + process_mode from config
pocket-libre delete --date 2026-08-02 --timestamp PH260802164240
pocket-libre process --input ~/Pocket\ Libre/2026-08-02/file.mp3
```

> After `setup`, you usually don’t need `--address` / `--key` on every command.

## How sync works

Sync is **on demand** (no background watcher yet). Each run of `pocket-libre sync`
or the web **Sync All** button:

1. Lists files on the device
2. Downloads recordings not already present under your output directory
3. Optionally processes them (Whisper / summaries)
4. Optionally deletes them from the device after a successful download

Controlled by config:

| Key | Values | Meaning |
|-----|--------|---------|
| `sync_mode` | `sync` / `sync-and-delete` | Keep on device, or delete after successful download |
| `process_mode` | `download` / `download-and-process` | Audio only, or also Whisper + summaries |

```bash
pocket-libre config --set defaults.sync_mode=sync-and-delete
pocket-libre config --set defaults.process_mode=download

# One-off overrides:
pocket-libre sync --mode sync-and-delete --process-mode download
```

Delete only runs after a **successful download** (local file written). Local library
files are never removed by sync/delete-from-device.

## Web Interface

```bash
pocket-libre web                 # default: 127.0.0.1:8265
pocket-libre web --no-browser
```

- **Dashboard** — battery, firmware, storage; Sync All
- **Device Recordings** — list, download, download+process, **delete from device**
- **Library** — local audio, transcripts, summaries, chat/analysis
- **Settings** — address, session key, API keys, sync/process modes, Whisper model

**Note:** `pocket-libre web` does not auto-reload. Restart it after pulling code changes
or the UI/API may 404 on new routes.

Keep the bind on localhost unless you intentionally expose the unauthenticated API.

## All Commands

| Command | Description |
|---------|-------------|
| `pocket-libre setup` | Interactive setup wizard |
| `pocket-libre web` | Launch web interface |
| `pocket-libre config` | View/edit configuration |
| `pocket-libre scan` | Find nearby BLE devices |
| `pocket-libre status` | Device battery, firmware, storage |
| `pocket-libre list` | List recordings on device |
| `pocket-libre download` | Download a specific recording |
| `pocket-libre delete` | Delete a recording from the device |
| `pocket-libre download-all` | Download all recordings |
| `pocket-libre sync` | Sync new recordings (modes from config) |
| `pocket-libre process` | Process an existing audio file |
| `pocket-libre transcribe` | Transcribe an audio file with Whisper |
| `pocket-libre explore` | Dump GATT services and characteristics |
| `pocket-libre sniff` | Subscribe to BLE notifications |
| `pocket-libre probe` | Probe write characteristics |
| `pocket-libre convert` | Convert raw audio to WAV |

## Configuration

`~/.pocket-libre/config.toml`:

```toml
[device]
address = "YOUR-DEVICE-ADDRESS"
session_key = "YOUR-SESSION-KEY"

[api]
anthropic_key = "sk-ant-..."
hf_token = "hf_..."

[output]
directory = "~/Pocket Libre"

[defaults]
whisper_model = "base.en"
summary_style = "meeting"
sync_mode = "sync"                         # or "sync-and-delete"
process_mode = "download-and-process"      # or "download"
```

```bash
pocket-libre config --set device.address=YOUR_ADDRESS
pocket-libre config --set defaults.sync_mode=sync-and-delete
pocket-libre config --set defaults.process_mode=download
```

Downloaded audio defaults to `~/Pocket Libre/<date>/<timestamp>.mp3`.

## Architecture notes (learnings)

- **Cloud path:** official app pulls from the device (BLE/Wi‑Fi SoftAP/USB), then uploads.
  The hardware is not a standalone internet client today.
- **BLE auth:** cleartext 16-char session key; durable across normal reconnects.
- **Delete:** same date/timestamp shape as download — `APP&D&…` → `MCU&D`
  (see [PROTOCOL.md](PROTOCOL.md)).
- **Timestamps:** usually `YYYYMMDDHHmmss`; some call recordings use a `PH…` id — pass through as-is.
- **Captures:** `*.pklg` / HCI snoop logs contain secrets. They are gitignored; don’t publish them.
- **Emulators:** Android emulators cannot talk BLE to a physical Pocket — use a real phone for key capture.

## API Keys

| Key | What it does | Cost | Where to get it |
|-----|-------------|------|-----------------|
| **Anthropic** | AI summaries of recordings | ~$0.001/recording | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) |
| **HuggingFace** | Speaker identification (who said what) | Free | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |

Both are optional. Transcription always runs locally via Whisper — no API key needed.

## Economics

| | Pocket Pro | Pocket Libre |
|--|-----------|-------------|
| Transcription | Cloud (their servers) | Local Whisper (your CPU) |
| Summarization | Cloud (forced) | Claude Haiku (~$0.001/recording) |
| Annual cost | $79-179/year | ~$2/year |
| Privacy | Audio leaves your device | Nothing leaves your device* |

*Summarization uses Anthropic API if enabled. Transcription is fully local.

## Protocol

The Pocket uses a simple ASCII command protocol over BLE GATT. Audio is standard MP3 (16kHz mono, ~32kbps). No encryption, no DRM.

See [PROTOCOL.md](PROTOCOL.md) for the full protocol reference, session-key capture guide, and delete command.

## Project Status

- [x] BLE device scanning and discovery
- [x] Full command protocol decoded (APP&/MCU&)
- [x] Device status, file listing, stored recording download
- [x] Delete recording from device (`APP&D&`)
- [x] Configurable sync modes (keep vs delete; download vs process)
- [x] MP3 audio capture and playback
- [x] Local Whisper transcription
- [x] Speaker diarization (pyannote.audio)
- [x] Claude Haiku summarization (4 styles)
- [x] Full sync pipeline
- [x] Web interface (including device delete)
- [x] Config file and setup wizard
- [ ] WiFi bulk transfer (protocol mapped, HTTP endpoint TBD)
- [ ] Auto-connect and background sync

## Contributing

If you own a Pocket and want to help:

1. Run `pocket-libre explore` and share the output (redact addresses if you want)
2. Capture BLE with PacketLogger during official-app actions we haven’t mapped (Wi‑Fi HTTP path, wipe, remote record)
3. Help discover the WiFi SoftAP HTTP endpoint
4. Never commit session keys or `.pklg` traces

Open an issue or PR. All contributions welcome.

## Legal

This project reverse-engineers a Bluetooth protocol for personal interoperability purposes, protected under DMCA Section 1201 exemptions. You own your device. You own your recordings.

## License

MIT
