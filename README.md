# Pocket Libre

**Liberate your Pocket AI recorder from the cloud.**

[![CI](https://github.com/shahcolate/pocket-libre/actions/workflows/ci.yml/badge.svg)](https://github.com/shahcolate/pocket-libre/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Pocket Libre replaces the vendor app for your [Pocket](https://heypocket.com) AI
voice recorder. It pulls recordings off the device over Bluetooth, transcribes
them locally with Whisper, works out who was speaking, and summarizes with your
own API key.

Your conversations never touch anyone else's servers.

```bash
pip install -e . && pocket-libre setup && pocket-libre web
```

<!-- Screenshot: the web UI dashboard showing device battery, storage, and the
     recording library. Add as docs/screenshot-dashboard.png and link here. -->

---

## Why

The Pocket is good hardware wrapped in a subscription. The vendor app uploads
your audio, transcribes it on their servers, and charges $79–179 a year for the
privilege.

The device itself does none of that. It stores MP3 files and hands them over
when asked. So this asks it directly.

| | Pocket Pro | Pocket Libre |
|--|-----------|--------------|
| Transcription | Their servers | Local Whisper, your CPU |
| Summarization | Cloud, mandatory | Claude Haiku, ~$0.02/recording |
| Where your audio lives | Their infrastructure | Your disk |
| Annual cost | $79–179 | ~$5 in API credits |
| Works offline | No | Yes (except summaries) |

Transcription and speaker identification run entirely on your machine.
Summarization is the only step that touches the network, and you can skip it.
You still get timestamped, speaker-labeled transcripts.

## Requirements

- Python 3.10 or newer
- A Pocket recorder (`PKT01`; see [firmware compatibility](#firmware-compatibility))
- Bluetooth LE support
- The device's 16-character session key (see [below](#getting-your-session-key))

## Firmware compatibility

Firmware matters more than you would expect — the device's WiFi behaviour
differs enough between versions that a sequence confirmed on one can strand
another. Check yours with `pocket-libre status`.

| | 1.3.3 | 1.8 |
|---|---|---|
| Scan, connect, authenticate | Works | Works |
| Status, storage, file listing | Works | Works |
| BLE download (`download`, `sync`, `watch`) | Works | Works |
| Transcribe, diarize, summarize, web UI | Works | Works |
| WiFi AP handshake | Works | Works, but [needs a different order](PROTOCOL.md#wifi-transfer-fast--partially-decoded) |
| WiFi file transfer | Never confirmed | **No endpoint found** |

**Everything except WiFi transfer works on both.** The BLE path is slower
(3–4 KB/s, so a long recording takes hours) but it is reliable, and it is what
`download`, `sync` and `watch` use.

**WiFi transfer does not currently work on any firmware.** The BLE handshake
that raises the access point is decoded, but nothing has been found listening
on the AP once it is up — see [#4](https://github.com/shahcolate/pocket-libre/issues/4)
and the [WiFi transfer](#wifi-transfer) section. On firmware 1.8, raising the
AP the wrong way can leave the device needing a physical power-cycle.

Other firmware versions are untested. If you have one, `pocket-libre status`
and `pocket-libre explore` output would be genuinely useful in an issue.

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

The wizard scans for your device, asks for your session key and API keys, and
writes everything to `~/.pocket-libre/config.toml` with mode `600`. After that
you never pass `--address` or keys on the command line again.

### Getting your session key

The 16-character session key authenticates the BLE connection. The vendor app
receives it during pairing, so you need to capture it once from an HCI trace.

**macOS/iOS.** Use [PacketLogger](https://developer.apple.com/bluetooth/), which
ships with Additional Tools for Xcode. Start a capture, open the vendor app, let
it connect, then search the trace for `APP&SK&`.

**Android (easiest).** The vendor app writes an analytics event containing
`distinct_id`, a 28-character account ID whose **first 16 characters are the
session key**. No packet capture needed:

```bash
adb logcat | grep -i distinct_id
```

**Android (packet capture).** Enable *Bluetooth HCI snoop log* in Developer
Options, connect with the vendor app, then pull `btsnoop_hci.log` and open it
in Wireshark. Note that the snoop log inside an ordinary bug report is **not**
sufficient — it truncates every ACL packet to 15 bytes, leaving 3 bytes of ATT
payload, so `APP&`/`MCU&` frames are unreadable. Set *Bluetooth HCI snoop log*
to **Full**, restart the Bluetooth stack, and take the capture from there.

`pocket-libre sniff` won't find it for you. It only sees notifications on its own
connection, and the key is something the app *writes*.

> **The session key is per-account, not per-device**, and its first 8 characters
> are your device's WiFi AP password. See [SECURITY.md](SECURITY.md) for what
> that means for you. Both findings come from
> [#4](https://github.com/shahcolate/pocket-libre/issues/4).

Full protocol details are in [PROTOCOL.md](PROTOCOL.md).

## Two recorders, or two people

One config can describe several recorders. Each gets a profile, and a profile
owns its own library, web port, credentials and transcription settings.

```bash
pocket-libre setup --profile mine
pocket-libre setup --profile hers
pocket-libre profiles
```

```toml
default_profile = "mine"

[device]
session_key = "SHARED-ACCOUNT-KEY"   # issued per vendor account, not per device

[profiles.mine]
label = "Mine"
address = "AA:BB:CC:DD:EE:01"

[profiles.hers]
label = "Hers"
address = "AA:BB:CC:DD:EE:02"
accent = "#C0888D"
```

Then `-p`, or `POCKET_LIBRE_PROFILE`, picks one:

```bash
pocket-libre -p hers status
pocket-libre -p hers sync
pocket-libre watch --all        # every profile, one device at a time
pocket-libre web --all          # one server per profile, on its own port
```

A few things follow from the profiles being separate on purpose:

- **Each has its own library.** A profile that does not set
  `output_directory` gets a subdirectory of the global one rather than sharing
  it, so two people's recordings cannot end up in one folder by default.
- **A command never guesses whose device it means.** With several profiles and
  no `default_profile`, every device command asks for `--profile` instead of
  picking one. `pocket-libre profiles` also warns when two profiles would share
  a library, a port or a device address.
- **A web server serves exactly one profile.** The profile is resolved once,
  before anything else runs, and `web --all` puts each server in its own
  process. The header shows whose library it is, in that profile's accent
  colour, because two identical tabs on adjacent ports is how someone ends up
  reading the wrong person's transcript.
- **The session key can be shared.** It is issued per vendor account, so a
  profile with no key of its own falls back to `[device].session_key`. Two
  recorders on one account need one key; on two accounts, one each.

A config with no profiles at all behaves exactly as it always did.

## Use

### Web interface

```bash
pocket-libre web
```

Opens `http://127.0.0.1:8265`. You get device status, one-click download and
processing, an audio player, transcripts, summaries, mind maps, and a chat box
for asking questions about any recording.

> The web interface has **no authentication**. It binds to localhost by default.
> Keep it there. Passing `--host 0.0.0.0` hands your transcripts and your device
> to everyone on the network.

### Command line

```bash
pocket-libre status                 # Battery, firmware, storage
pocket-libre list                   # What's on the device
pocket-libre sync                   # Download + transcribe + summarize everything new
pocket-libre watch                  # Same, but automatically whenever the device appears
pocket-libre process --input x.mp3  # Process an audio file you already have
```

Leave `watch` running and recordings sync themselves whenever the Pocket comes
in range. It backs off to five-minute checks while the device is away, so it
isn't scanning flat out all day.

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
| `usb` | Show or set USB mass storage mode (`on`, `off`, `status`) |
| `download` | Download one recording |
| `download-all` | Download every recording |
| `sync` | Download, transcribe, and summarize what's new |
| `process` | Process an existing audio file |
| `transcribe` | Transcribe locally with Whisper |
| `convert` | Convert raw audio to WAV |
| `wifi-transfer` | Download over WiFi instead of BLE ([see caveat](#wifi-transfer)) |
| `wifi-discover` | Sweep the device's WiFi AP for listening sockets |
| `explore` | Dump GATT services and characteristics |
| `sniff` | Subscribe to all BLE notifications |
| `probe` | Probe write characteristics |
| `profiles` | List configured recorders, their libraries and ports |
| `speakers` | List, enroll, test or forget voices in this library |
| `search` | Search inside transcripts, summaries and action items |
| `reindex` | Rebuild this library's search index |
| `tasks` | Print action items as checkboxes |
| `export` | Write recordings out as Markdown notes |

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
transcribe_backend = "openai-whisper"   # or faster-whisper-xxl, or none
language = "auto"                       # or it, de, en, ...
faster_whisper_path = ""                # only if it is not auto-detected
device = "cuda"
max_speakers = 0                        # 0 = let diarization decide

[analysis]
enabled = "summary,entities"
```

Set values directly with `pocket-libre config --set device.address=...`, or
`--set profiles.hers.address=...` for one profile.

Resolution order is CLI flag → environment variable → **profile** → config
file → default, so `ANTHROPIC_API_KEY` in your shell overrides the file, and a
profile overrides the global sections.

The config holds your API key and your session key, so it is written
owner-only: mode `600`, or a rewritten ACL on Windows where `chmod` cannot
restrict a file.

## API keys

| Key | What it does | Cost | Where |
|-----|--------------|------|-------|
| **Anthropic** | Summaries, entity extraction, mind maps, chat | ~$0.02/recording | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| **HuggingFace** | Speaker identification via pyannote | Free | [huggingface.co](https://huggingface.co/settings/tokens) |

Both are optional. Without an Anthropic key you still get local transcription.
Without a HuggingFace token, speaker labeling falls back to Claude, and then to
a single unnamed speaker.

That cost estimate assumes Claude Haiku 4.5 at $1 and $5 per million input and
output tokens, on a half-hour recording with summary, entities, and mind map all
enabled. Every command prints what it actually spent.

## Transcription

Two backends. `defaults.transcribe_backend` picks one, per profile if you like.

| | `openai-whisper` | `faster-whisper-xxl` |
|--|------------------|----------------------|
| Runs | in this process | a local standalone build |
| Install | included | [download it separately](https://github.com/Purfview/whisper-standalone-win) |
| Languages | English models by default | all of Whisper's, auto-detected |
| Speakers | no; falls back to pyannote, Claude, or one label | included, no HuggingFace token |
| Needs torch | yes | no |
| Speed | CPU, realtime-ish | GPU, an order of magnitude faster |

`openai-whisper` is the default and is unchanged. To use the other one:

```bash
pocket-libre config --set defaults.transcribe_backend=faster-whisper-xxl
pocket-libre config --set defaults.faster_whisper_path=/path/to/Faster-Whisper-XXL
pocket-libre config --set defaults.language=auto
```

It is found automatically if it sits in `~/Tools/Faster-Whisper-XXL` or on
`PATH`, or via `FASTER_WHISPER_XXL`.

### Language

`defaults.language` takes `auto` or a code like `it`, `de`, `en`. On `auto` the
backend is asked to sample several windows rather than just the first, because
detection from one window is a coin flip on a short recording - and when it
loses, Whisper transcribes the audio phonetically in the wrong language instead
of failing in a way you would notice. If you always speak the same language,
set it.

## Who is speaking

Diarization labels are per recording: `SPEAKER_01` in one file is not the same
person as `SPEAKER_01` in the next. So names do not come from a label map. They
come from the speaker embedding the local backend dumps - one vector per
speaker, which *is* comparable across recordings.

```bash
pocket-libre speakers                                  # enrolled voices
pocket-libre speakers enroll Ada     --recording 2026-10-04/20261004120000 --label SPEAKER_01
pocket-libre speakers test --recording 2026-10-04/20261004120000
pocket-libre speakers threshold 0.7
```

Pick the recording by ear once, enroll the voice, and later recordings name it
for you. `speakers test` prints the actual similarity scores, which is how you
should choose the threshold: the right cut-off depends on the microphone and on
the voices, not on this project's default. Enroll a second sample from another
room to make the match hold up.

Matching is one name per speaker and one speaker per name, and anything below
the threshold stays `SPEAKER_01`. That is the right answer when it is someone
you have not enrolled.

## Searching, tasks, and notes

```bash
pocket-libre search mortgage rate            # inside transcripts and summaries
pocket-libre search budget --kind summary
pocket-libre tasks                           # commitments, as checkboxes
pocket-libre export --to ~/Notes             # one Markdown note per recording
```

Search is an SQLite FTS5 index kept inside the library, so it covers one
profile and never reaches another. It is accent-folded, so `perche` finds
`perché`, and it is refreshed before each search.

`tasks` prints the action items the `entities` analysis extracts as `- [ ]`
lines, ready to paste into a task list. A task carries a date only when the
transcript stated one: "next Tuesday" is quoted as said rather than turned into
a deadline nobody agreed to.

`export` writes one self-contained note per recording - frontmatter, summary,
action items, full transcript. It needs a destination, from `--to` or from the
profile's own `vault_path` with `vault_export = true`, and it will not
overwrite a note that already exists unless you pass `--overwrite`.

## Protocol

The Pocket speaks a plain ASCII command protocol over BLE GATT, and the audio is
ordinary MP3 at 16 kHz mono, around 32 kbps. No encryption, no DRM, no
proprietary codec. [PROTOCOL.md](PROTOCOL.md) has the full command reference.

## WiFi transfer

> **Experimental, and it can strand your device.** On firmware 1.8, raising
> the WiFi access point in the order this project previously used left the
> device unreachable over BLE until it was **physically power-cycled** —
> `APP&WIFIC` cannot recover it, because BLE is already gone by then. Use
> `pocket-libre download` unless you are actively helping decode this.

BLE transfers run at 3–4 KB/s, so a long recording can take hours. The device
can raise a WiFi access point to move files faster, and the BLE side of that
handshake is decoded.

**What runs on that access point is not decoded.** This project used to claim
the device served files over HTTP at `192.168.4.1`. That was inference from a
BLE-only packet capture, and firmware 1.8 field data
([#4](https://github.com/shahcolate/pocket-libre/issues/4)) showed it was
wrong: with a file staged and the device reporting ready, an exhaustive sweep
of all 65535 TCP ports found only DNS open. Strings in the vendor app describe
a framed socket protocol using a `RANGE` verb, whose port and frame format are
both unknown.

So `wifi-transfer` requires `--url` and refuses to raise the access point
without one — there is no point risking the device for a download with nowhere
to download from.

If you own a Pocket, the most useful thing you can contribute is a port sweep
of the AP, run twice — once before staging a file, once while the device
reports `WIFIS=1`:

```bash
# Join the device's WiFi network first, then:
pocket-libre wifi-discover
```

Share both outputs in [an issue](https://github.com/shahcolate/pocket-libre/issues).
A confirmed "nothing listens" is as valuable as a hit: it would mean fast
transfer is unreachable on this firmware by any client we could write.

## Troubleshooting

**The device stopped responding over BLE after a WiFi command.** This is the
firmware 1.8 trap: `APP&WIFIO` sent in the wrong order tears down BLE, and
`APP&WIFIC` cannot recover it because BLE is already gone. **Power-cycle the
device physically** — hold the power button until it switches off, then back
on. It will start advertising again. Nothing on the device is lost;
recordings are on its internal storage.

**`pocket-libre scan` finds nothing.** Make sure the device is on and not
connected to the vendor app — it accepts one BLE connection at a time, so
force-quit the phone app first. On Linux, BLE scanning usually needs
`bluetoothd` running and your user in the `bluetooth` group.

**Authentication fails.** The session key is per *account*, not per device, so
a key captured from one phone works for every device on that account — but it
changes if you sign in as someone else. Re-capture it with the
[logcat method](#getting-your-session-key). Keys are 16 characters.

**Downloads are slow.** That is expected: BLE runs at 3–4 KB/s, so an hour of
audio takes roughly two hours to pull. `pocket-libre watch` runs in the
background and syncs new recordings as they appear, which is usually a better
fit than waiting on a single download.

**A download came back short.** The device occasionally drops a transfer.
`download` retries on its own; if it keeps failing, move the device closer and
close the vendor app. Partial files are never left behind under the final
name.

**Transcription is slow or runs out of memory.** Whisper model size is the
lever: `pocket-libre config --set transcribe.model=base` is much lighter than
`large`. Diarization needs PyTorch and a HuggingFace token; without them the
pipeline falls back to a heuristic speaker split.

**Something else.** Open an issue with the output of `pocket-libre status` and
your firmware version — and scrub your session key, which is also your
device's WiFi password.

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
- [x] WiFi AP handshake (BLE side), firmware 1.8 sequence
- [x] Several recorders in one config, with separate libraries
- [x] Pluggable transcription backends, multilingual, speakers included
- [x] Named speakers, matched by voice embedding across recordings
- [x] Full-text search, action items, Markdown export
- [ ] WiFi transfer socket port identified
- [ ] WiFi `RANGE` frame format decoded

## Contributing

Pull requests welcome. The things that would help most right now:

1. **Find the WiFi transfer socket.** Run `wifi-discover` on the device AP,
   before and after staging a file, and share both sweeps.
2. **Test on other firmware.** Run `pocket-libre explore` and share the output.
3. **Report protocol differences.** Capture with PacketLogger and open an issue.

[CONTRIBUTING.md](CONTRIBUTING.md) covers development setup and how to run the
tests.

## Legal

This project reverse-engineers a Bluetooth protocol for personal
interoperability, protected under DMCA Section 1201 exemptions. Nothing here
circumvents DRM, because there is none; the audio is unencrypted MP3. You own
your device. You own your recordings.

## License

MIT. See [LICENSE](LICENSE).
