# Pocket Libre

**Liberate your Pocket AI recorder from the cloud.**

Pocket Libre replaces the vendor app for your [Pocket](https://heypocket.com) AI voice recorder. Download recordings directly over Bluetooth, transcribe locally with Whisper, identify speakers, and summarize with your own API keys. No vendor cloud. Your conversations stay on your machine.

## Quick Start

### 1. Install

```bash
git clone https://github.com/shahcolate/pocket-libre.git
cd pocket-libre
pip install -e .
```

> **For speaker identification** (optional): `pip install -e ".[diarize]"` (requires PyTorch)

### 2. Setup

```bash
pocket-libre setup
```

The setup wizard walks you through:
- Finding your Pocket device (BLE scan)
- Entering your API keys (Anthropic for summaries, HuggingFace for speaker ID)
- Choosing your output directory and preferences

### 3. Use

**Web interface** (recommended):
```bash
pocket-libre web
```
Opens a browser UI at `http://localhost:8265` where you can view device status, download recordings, play audio, read transcripts and summaries — no terminal required.

**Command line**:
```bash
pocket-libre status              # Check battery, storage, firmware
pocket-libre list                # List recordings on device
pocket-libre download-all        # Download all recordings
pocket-libre sync                # Capture + transcribe + summarize
pocket-libre process --input recording.mp3  # Process an existing file
```

> **Tip**: After running `pocket-libre setup`, you don't need to pass `--address` or API keys on every command — they're saved in `~/.pocket-libre/config.toml`.

## Web Interface

Run `pocket-libre web` to launch the browser-based UI:

- **Dashboard** — device battery, firmware, storage at a glance
- **Device Recordings** — see what's on your Pocket, download or process with one click
- **Library** — browse downloaded recordings with audio player, transcripts, and summaries
- **Settings** — configure device address, API keys, whisper model, and output directory

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
| `pocket-libre download-all` | Download all recordings |
| `pocket-libre wifi-transfer` | Download via WiFi (faster) |
| `pocket-libre sync` | Full pipeline: capture + transcribe + summarize |
| `pocket-libre process` | Process an existing audio file |
| `pocket-libre transcribe` | Transcribe audio locally with Whisper |
| `pocket-libre explore` | Dump GATT services and characteristics |
| `pocket-libre sniff` | Subscribe to all BLE notifications |
| `pocket-libre probe` | Probe write characteristics |
| `pocket-libre convert` | Convert raw audio to WAV |

## Configuration

All settings are stored in `~/.pocket-libre/config.toml`:

```toml
[device]
address = "YOUR-DEVICE-ADDRESS"
session_key = "xJiEbRKnKrhCqvoZ"

[api]
anthropic_key = "sk-ant-..."
hf_token = "hf_..."

[output]
directory = "~/Pocket Libre"

[defaults]
whisper_model = "base.en"
summary_style = "meeting"
```

You can also set values directly:
```bash
pocket-libre config --set device.address=YOUR_ADDRESS
pocket-libre config --set api.anthropic_key=sk-ant-...
```

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

See [PROTOCOL.md](PROTOCOL.md) for the full protocol reference.

## Project Status

- [x] BLE device scanning and discovery
- [x] Full command protocol decoded (APP&/MCU&)
- [x] Device status, file listing, stored recording download
- [x] MP3 audio capture and playback
- [x] Local Whisper transcription
- [x] Speaker diarization (pyannote.audio)
- [x] Claude Haiku summarization (4 styles)
- [x] Full sync pipeline
- [x] Web interface
- [x] Config file and setup wizard
- [ ] WiFi bulk transfer (protocol mapped, HTTP endpoint TBD)
- [ ] Auto-connect and background sync

## Contributing

If you own a Pocket and want to help:

1. Run `pocket-libre explore` and share the output
2. Help discover the WiFi HTTP endpoint (connect to device AP, probe ports)
3. Capture BLE traffic with PacketLogger during an app WiFi transfer

Open an issue or PR. All contributions welcome.

## Legal

This project reverse-engineers a Bluetooth protocol for personal interoperability purposes, protected under DMCA Section 1201 exemptions. You own your device. You own your recordings.

## License

MIT
