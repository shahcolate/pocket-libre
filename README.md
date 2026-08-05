# Pocket Libre

**Liberate your Pocket AI recorder from the cloud.**

[![CI](https://github.com/shahcolate/pocket-libre/actions/workflows/ci.yml/badge.svg)](https://github.com/shahcolate/pocket-libre/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Pocket Libre replaces the vendor app for your [Pocket](https://heypocket.com) AI
voice recorder. It pulls recordings straight off the device over Bluetooth,
transcribes them locally with Whisper, identifies who spoke, and summarizes with
your own API key.

Your conversations never touch anyone else's servers.

```bash
pip install -e . && pocket-libre setup && pocket-libre web
```

<!-- Screenshot: the web UI dashboard showing device battery, storage, and the
     recording library. Add as docs/screenshot-dashboard.png and link here. -->

---

## Why

The Pocket is good hardware wrapped in a subscription. The vendor app uploads
your audio, transcribes it on their servers, and charges $79–179/year for the
privilege. The device itself does none of that — it just stores MP3 files and
hands them over on request.

So this talks to it directly.

| | Pocket Pro | Pocket Libre |
|--|-----------|--------------|
| Transcription | Their servers | Local Whisper, your CPU |
| Summarization | Cloud, mandatory | Claude Haiku, ~$0.02/recording |
| Where your audio lives | Their infrastructure | Your disk |
| Annual cost | $79–179 | ~$5 in API credits |
| Works offline | No | Yes (except summaries) |

Transcription and speaker identification are fully local. Summarization is the
only step that makes a network call, and it is optional — skip it and you still
get timestamped, speaker-labeled transcripts.

## Requirements

- Python 3.10 or newer
- A Pocket recorder (tested on `PKT01`, firmware 1.3.3)
- Bluetooth LE support
- The device's 16-character session key — see [Getting your session key](#getting-your-session-key)

## Install

```bash
git clone https://github.com/shahcolate/pocket-libre.git
cd pocket-libre
pip install -e .
```

Speaker identification is optional and pulls in PyTorch:

```bash
pip install -e ".[diarize]"
```

## Setup

```bash
pocket-libre setup
```

The wizard scans for your device, takes your session key and API keys, and
writes everything to `~/.pocket-libre/config.toml` (mode `600`). After this you
never pass `--address` or keys on the command line again.

### Getting your session key

The 16-character session key authenticates the BLE connection. It is issued to
the vendor app during pairing, so you have to capture it once from an HCI trace:

- **macOS/iOS** — [PacketLogger](https://developer.apple.com/bluetooth/) (ships
  with Additional Tools for Xcode). Start a capture, open the vendor app, let it
  connect, then search the trace for `APP&SK&`.
- **Android** — enable *Bluetooth HCI snoop log* in Developer Options, connect
  with the vendor app, then pull `btsnoop_hci.log` and open it in Wireshark.

`pocket-libre sniff` cannot find it for you — it only sees notifications on its
own connection, and the key is something the app *writes*.

Full protocol details are in [PROTOCOL.md](PROTOCOL.md).

## Use

### Web interface

```bash
pocket-libre web
```

Opens `http://127.0.0.1:8265` — device status, one-click download and
processing, an audio player, transcripts, summaries, mind maps, and a chat box
for asking questions about any recording.

> The web interface has **no authentication**. It binds to localhost by
> default, which is what you want. Passing `--host 0.0.0.0` exposes your
> transcripts and your device to everyone on the network.

### Command line

```bash
pocket-libre status                 # Battery, firmware, storage
pocket-libre list                   # What's on the device
pocket-libre sync                   # Download + transcribe + summarize everything new
pocket-libre watch                  # Same, but automatically whenever the device appears
pocket-libre process --input x.mp3  # Process an audio file you already have
```

`watch` is the set-and-forget mode: leave it running, and recordings sync
whenever the Pocket comes in range. It backs off to five-minute checks while the
device is away.

## All commands

| Command | Description |
|---------|-------------|
| `setup` | Interactive setup wizard |
| `web` | Launch the web interface |
| `watch` | Auto-sync whenever the device is in range |
| `config` | View or edit configuration |
| `scan` | Find nearby BLE devices |
| `status` | Device battery, firmware, storage |
| `list` | List recordings on device |
| `download` | Download one recording |
| `download-all` | Download every recording |
| `sync` | Download, transcribe, and summarize what's new |
| `process` | Process an existing audio file |
| `transcribe` | Transcribe locally with Whisper |
| `convert` | Convert raw audio to WAV |
| `wifi-transfer` | Download over WiFi instead of BLE ([see caveat](#wifi-transfer)) |
| `wifi-discover` | Probe for the device's HTTP endpoint |
| `explore` | Dump GATT services and characteristics |
| `sniff` | Subscribe to all BLE notifications |
| `probe` | Probe write characteristics |

Every command takes `--help`.

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

[analysis]
enabled = "summary,entities"
```

Set values directly with `pocket-libre config --set device.address=...`.
Resolution order is CLI flag → environment variable → config file → default,
so `ANTHROPIC_API_KEY` in your shell overrides the file.

## API keys

| Key | What it does | Cost | Where |
|-----|--------------|------|-------|
| **Anthropic** | Summaries, entity extraction, mind maps, chat | ~$0.02/recording | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| **HuggingFace** | Speaker identification via pyannote | Free | [huggingface.co](https://huggingface.co/settings/tokens) |

Both optional. Without an Anthropic key you still get local transcription;
without a HuggingFace token, speaker labels fall back to Claude-based
diarization, and then to a single unnamed speaker.

Cost estimate assumes Claude Haiku 4.5 at $1/$5 per million input/output tokens
and a typical 30-minute recording with summary, entities, and mind map enabled.
Every command prints its actual token usage and cost.

## Protocol

The Pocket speaks a plain ASCII command protocol over BLE GATT. Audio is
standard MP3 (16 kHz mono, ~32 kbps). No encryption, no DRM, no proprietary
codec. [PROTOCOL.md](PROTOCOL.md) has the full command reference.

## WiFi transfer

BLE transfers run at 3–4 KB/s, so a long recording can take hours. The device
can raise a WiFi access point and serve files over HTTP instead, and the BLE
side of that handshake is fully decoded.

**The HTTP endpoint it serves files from is not yet confirmed.** `wifi-transfer`
drives the whole flow and probes for the endpoint, but until someone with a
device pins it down, it may not find it. If you own a Pocket, this is the single
most useful thing you can contribute:

```bash
# Join the device's WiFi network first, then:
pocket-libre wifi-discover --date 2026-03-28 --timestamp 20260328001919
```

Paste the output into [an issue](https://github.com/shahcolate/pocket-libre/issues).
Once it is known, everyone gets fast transfers.

## Status

- [x] BLE scanning and device discovery
- [x] Full command protocol (`APP&`/`MCU&`)
- [x] Device status, file listing, downloads
- [x] MP3 capture and playback
- [x] Local Whisper transcription
- [x] Speaker diarization (pyannote, with Claude and heuristic fallbacks)
- [x] Claude summarization, entity extraction, mind maps, chat
- [x] Web interface
- [x] Config file and setup wizard
- [x] Auto-connect and background sync (`watch`)
- [x] WiFi transfer client and endpoint discovery
- [ ] WiFi HTTP endpoint confirmed against hardware

## Contributing

Pull requests welcome. The most valuable contributions right now:

1. **Find the WiFi HTTP endpoint** — run `wifi-discover` on the device AP
2. **Test on other firmware** — run `pocket-libre explore` and share the output
3. **Report protocol differences** — capture with PacketLogger and open an issue

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and how to run the
tests.

## Legal

This project reverse-engineers a Bluetooth protocol for personal
interoperability, protected under DMCA Section 1201 exemptions. No DRM is
circumvented — the audio is unencrypted MP3. You own your device. You own your
recordings.

## License

MIT — see [LICENSE](LICENSE).
