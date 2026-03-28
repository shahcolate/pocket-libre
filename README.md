# Pocket Libre

**Liberate your Pocket AI recorder from the cloud.**

Pocket Libre is a local-first CLI tool that connects directly to your [Pocket](https://heypocket.com) AI voice recorder over Bluetooth Low Energy, pulls raw audio, and transcribes it locally using OpenAI's Whisper. No app. No cloud. No forced summaries. Your conversations stay on your machine.

## Why

The Pocket hardware is great: tap to record, long battery, solid mics. But the default workflow forces all your audio through their cloud for transcription and summarization. You don't get a choice. Your private conversations, meetings, and ideas get shipped to a server before you can even listen to them.

Pocket Libre gives you that choice back.

## Protocol (Confirmed)

The Pocket streams **standard MP3 audio** over BLE. No proprietary codec. No encryption. No DRM.

| Detail | Value |
|--------|-------|
| Transport | Bluetooth Low Energy (BLE 5.x) |
| Audio format | MPEG-2 Layer 3 (MP3) |
| Sample rate | 16 kHz |
| Channels | Mono |
| Bitrate | ~32 kbps |
| Audio characteristic | `001120a1-2233-4455-6677-889912345678` |
| Device name prefix | `PKT01` |

See [PROTOCOL.md](PROTOCOL.md) for the full service map and discovery process.

## Prerequisites

- Python 3.10+
- A Mac with Bluetooth (tested on Apple Silicon)
- Your Pocket AI recorder

## Setup

```bash
git clone https://github.com/shahcolate/pocket-libre.git
cd pocket-libre

conda create -n pocket python=3.12 -y
conda activate pocket
conda install -c conda-forge llvmlite numba -y
pip install -e .
```

## Quick Start

### 1. Find your Pocket

Turn on your Pocket. Make sure it's not connected to the official app (turn off Bluetooth on your phone or enable Airplane Mode).

```bash
pocket-libre scan --filter pkt
```

Copy the full address from the output.

### 2. Sniff audio

```bash
pocket-libre sniff --address <ADDRESS> --duration 30
```

Start a recording on your Pocket, talk for a few seconds, then stop. The sniff captures live BLE data and auto-saves as MP3 when audio is detected.

### 3. Play it

```bash
open sniff_dump.mp3
```

### 4. Transcribe locally

```bash
pocket-libre transcribe --input sniff_dump.mp3 --model base.en
```

Runs Whisper on your machine. Nothing leaves your device.

### 5. One-command pipeline

```bash
pocket-libre sync --address <ADDRESS>
```

Connect, capture MP3, transcribe with Whisper, identify speakers, summarize with Claude Haiku (~$0.001/recording). Everything saved to `~/pocket-recordings/`.

### 6. Process existing files

```bash
pocket-libre process --input recording.mp3
```

## All Commands

| Command | Description |
|---------|-------------|
| `pocket-libre scan` | Find nearby BLE devices |
| `pocket-libre explore` | Dump GATT services and characteristics |
| `pocket-libre sniff` | Subscribe to all notify characteristics (discovery mode) |
| `pocket-libre capture` | Capture audio from a specific characteristic |
| `pocket-libre convert` | Convert raw audio to WAV |
| `pocket-libre transcribe` | Transcribe audio locally with Whisper |
| `pocket-libre sync` | Full pipeline: capture, transcribe, diarize, summarize |
| `pocket-libre process` | Process an existing audio file through the pipeline |

## Economics

| | Pocket Pro | Pocket Libre |
|--|-----------|-------------|
| Transcription | Cloud (their servers) | Local Whisper (your CPU) |
| Summarization | Cloud (forced) | Claude Haiku API (~$0.001/recording) |
| Annual cost | $79-179/year | ~$2/year |
| Privacy | Your audio leaves your device | Nothing leaves your device* |

*Summarization uses Anthropic API if enabled. Transcription is fully local.

## Project Status

- [x] BLE device scanning and discovery
- [x] GATT service/characteristic enumeration
- [x] Multi-characteristic sniffing with live dashboard
- [x] Audio format identification (MP3 confirmed, playable)
- [x] MP3 audio capture and playback
- [x] Local Whisper transcription
- [x] Speaker diarization (pyannote.audio optional)
- [x] Claude Haiku summarization (4 styles)
- [x] Full sync pipeline (capture > transcribe > diarize > summarize)
- [x] PacketLogger HCI capture analysis
- [ ] Stored recording retrieval (pull past recordings from device storage)
- [ ] WiFi bulk transfer
- [ ] Auto-connect and background sync

## How We Got Here

This project started with a simple question: can I use my Pocket recorder without routing my private conversations through someone else's cloud?

The answer is yes. The Pocket uses standard BLE GATT services and streams plain MP3 audio. We discovered this by:

1. Scanning the device's service tree with nRF Connect
2. Subscribing to all notify characteristics simultaneously
3. Identifying the high-volume data stream on `001120a1`
4. Recognizing the `0xFFF3` MP3 frame sync word in the raw bytes

No encryption. No obfuscation. Just MP3.

## Contributing

If you own a Pocket and want to help:

1. Run `pocket-libre explore` and share the output (different firmware versions may have different UUIDs)
2. Help map the write commands (what does the app send to request stored recordings?)
3. Capture BLE traffic with PacketLogger during an app sync session

Open an issue or PR. All contributions welcome.

## Legal

This project reverse-engineers a Bluetooth protocol for personal interoperability purposes, which is protected under DMCA Section 1201 exemptions. You own your device. You own your recordings. This tool helps you access both without a mandatory cloud intermediary.

## License

MIT
