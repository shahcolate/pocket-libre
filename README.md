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

| | 1.3.3 | 1.7 | 1.8 |
|---|---|---|---|
| Scan, connect, authenticate | Works | Works | Works |
| Status, storage, file listing | Works | Works | Works |
| BLE download (`download`, `sync`, `watch`) | Works | Works | Works |
| Transcribe, diarize, summarize, web UI | Works | Works | Works |
| WiFi AP handshake | Works | Works | Works |
| WiFi file transfer (`wifi-transfer`) | Untested | **Works** (WiFi firmware V9) | **Works** (WiFi firmware V9) |

The 1.7 results come from a field report
([#9](https://github.com/shahcolate/pocket-libre/pull/9)) on WiFi firmware V9.

**Everything works on firmware 1.7 and 1.8.** The BLE path is slower (tens of KB/s on
1.8, as little as 3–4 KB/s on 1.3.3) but it is what `download`, `sync` and
`watch` use. `wifi-transfer` pulls recordings over the device's WiFi access
point at about 1 MB/s; see [WiFi transfer](#wifi-transfer).

WiFi transfer works on firmware 1.7 and 1.8, and `wifi-transfer` refuses
other firmware unless you pass `--force`; it then restarts the access point
for every file, which works on both known versions. On 1.3.3 the BLE side
looks the same, but nobody has tried it.

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

### Pairing without the vendor app

A device takes the first session key it is sent after a hardware reset and
refuses every other one from then on. `pocket-libre pair` uses that to pair a
device with a new random key of its own, so you need neither the vendor app
nor a packet capture:

1. Hardware-reset the Pocket: triple-click the side button (the LED blinks
   red), then press and hold it until the red blinking stops. The LED then
   pulses blue.
2. Make sure no phone is connected to it, then run:

```bash
pocket-libre pair
```

It finds the Pocket, pairs it, and saves its address and the new key to the
config. The vendor app can't connect to a device paired this way until it is
reset and paired in the app again (which in turn locks out this key).

### Getting your session key

To keep using the vendor app alongside pocket-libre, use the key the app
pairs with instead. The 16-character session key authenticates the BLE
connection. The vendor app sends it as the first key after pairing, so you
need to capture it once from an HCI trace.

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
| `pair` | Pair a reset Pocket with a key of its own, without the vendor app |
| `web` | Launch the web interface |
| `watch` | Auto-sync whenever the device is in range |
| `config` | View or edit configuration |
| `scan` | Find nearby BLE devices |
| `status` | Device battery, firmware, storage |
| `list` | List recordings on device |
| `usb` | Show or set USB mass storage mode (`on`, `off`, `status`) |
| `download` | Download one recording |
| `download-all` | Download every recording |
| `delete` | Delete recordings from the device: one, or every one already downloaded (`--downloaded`) |
| `sync` | Download, transcribe, and summarize what's new |
| `process` | Process an existing audio file |
| `transcribe` | Transcribe locally with Whisper |
| `convert` | Convert raw audio to WAV |
| `wifi-transfer` | Download over WiFi instead of BLE, about 1 MB/s ([details](#wifi-transfer)) |
| `wifi-discover` | Sweep the device's WiFi AP for listening sockets (diagnostic) |
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

Both are optional. Without an Anthropic key you still get local transcription.
Without a HuggingFace token, speaker labeling falls back to Claude, and then to
a single unnamed speaker.

That cost estimate assumes Claude Haiku 4.5 at $1 and $5 per million input and
output tokens, on a half-hour recording with summary, entities, and mind map all
enabled. Every command prints what it actually spent.

## Protocol

The Pocket speaks a plain ASCII command protocol over BLE GATT, and the audio is
ordinary MP3 at 16 kHz mono, around 32 kbps. No encryption, no DRM, no
proprietary codec. [PROTOCOL.md](PROTOCOL.md) has the full command reference.

## WiFi transfer

On firmware 1.7 and 1.8 the device can raise a WiFi access point and serve
recordings over it at about 1 MB/s, instead of the tens of KB/s that BLE manages. An hour
of audio (about 14 MB) takes 15–20 s instead of many minutes.

```bash
# Every recording not downloaded yet, into <output dir>/<date>/<timestamp>.mp3
pocket-libre wifi-transfer

# Only recent ones
pocket-libre wifi-transfer --since 2026-10-01

# One recording
pocket-libre wifi-transfer --date 2026-10-03 --timestamp 20261003143216
```

What it does:

1. Connects over BLE and authenticates. It checks the firmware (1.7 or 1.8)
   and the battery (above 10%).
2. Raises the device's access point and moves **this machine's WiFi** onto it.
   The network is hidden and WPA2-protected; its password is the first 8
   characters of your session key.
3. Requests each recording as a BLE transfer and switches it to WiFi, which is
   how the vendor app does it, then saves the file. The device serves two files
   per access-point session on 1.8 and one on 1.7, so the AP is restarted in
   between (about 15 s each time).
4. Lowers the access point and puts this machine back on its usual network. It
   does this after errors and Ctrl-C too.

Requirements and caveats:

- **Joining the network automatically** needs NetworkManager on Linux, or
  netsh on Windows. On macOS, or with `--wifi manual`, the command prints the
  network name and password, and you join it yourself while it waits. The
  network is hidden, so on macOS that's **Other Network…** in the WiFi menu,
  once per access-point session: before every file on firmware 1.7.
- **While it runs, this machine has no internet over WiFi.** With Ethernet
  connected you keep internet over the cable. On Linux, the temporary profile
  doesn't take over the default route.
- **The temporary profile holds the AP password while it exists** (NetworkManager
  or Windows profile store). It's deleted when the command ends.
- **Close the vendor app first.** The device takes one BLE connection at a
  time.

How the transfer works on the wire is in
[PROTOCOL.md](PROTOCOL.md#wifi-transfer-fast--decoded-on-firmware-18).

## Troubleshooting

**The device stopped responding over BLE after a WiFi command.** This was
reported on firmware 1.8 when raising the access point by hand
([#4](https://github.com/shahcolate/pocket-libre/issues/4)), and `APP&WIFIC`
can't recover it because BLE is already gone. **Power-cycle the device
physically**: hold the power button until it switches off, then turn it back
on. It will start advertising again. Nothing on the device is lost; recordings
are on its internal storage.

**The device isn't found right after a WiFi transfer.** Give it up to a minute
before the next run: right after a session it sometimes drops new BLE
connections. Also check that the vendor app hasn't reconnected in the
background.

**`wifi-transfer` can't join the network.** The access point is hidden, so it
won't show up in a WiFi scan; that is expected. On Linux, check that
NetworkManager manages your WiFi interface (`nmcli device`), or pick one with
`--iface`. As a fallback, `--wifi manual` lets you join it yourself.

**`pocket-libre scan` finds nothing.** Make sure the device is on and not
connected to the vendor app — it accepts one BLE connection at a time, so
force-quit the phone app first. On Linux, BLE scanning usually needs
`bluetoothd` running and your user in the `bluetooth` group.

**Authentication fails.** The session key is per *account*, not per device, so
a key captured from one phone works for every device on that account — but it
changes if you sign in as someone else. Re-capture it with the
[logcat method](#getting-your-session-key). Keys are 16 characters.

**Downloads are slow.** That is expected over BLE: tens of KB/s on firmware
1.7 and 1.8, and 3–4 KB/s on 1.3.3. On 1.7 and 1.8, `pocket-libre wifi-transfer`
runs at about 1 MB/s. Otherwise, `pocket-libre watch` runs in the background
and syncs new recordings as they appear.

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
- [x] WiFi transfer socket (TCP 8475) and stream format decoded (firmware 1.8)
- [x] WiFi transfer confirmed on firmware 1.7 (one file per access-point session)
- [x] `wifi-transfer` over WiFi, with automatic join on Linux and Windows
- [ ] Several files over one WiFi connection
- [ ] WiFi transfer on other firmware

## Contributing

Pull requests welcome. The things that would help most right now:

1. **Try `wifi-transfer` on other firmware** (anything but 1.8, with
   `--force`) and report what happens: `pocket-libre status` output, and
   whether the AP came up and the files arrived.
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
