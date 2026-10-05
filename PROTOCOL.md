# Pocket AI BLE Protocol

## Protocol Overview

The Pocket device uses a simple **ASCII text command protocol** over BLE GATT:
- **App writes** `APP&<COMMAND>` to characteristic E49A3002 (ATT handle 0x002b)
- **Device responds** `MCU&<RESPONSE>` via notifications on E49A3003 (ATT handle 0x0030)
- **Audio data** streams as MP3 on E49A28E1 (ATT handle 0x002d)

Decoded via PacketLogger HCI capture analysis.

## Audio Format

**MPEG-2 Layer 3 (MP3)**, 16 kHz mono, 32 kbps constant: 144-byte frames
starting `FF F3 48 C4`, so 4000 bytes per second of audio (confirmed on firmware
1.8: a 278 s recording is 1,114,450 bytes). No DRM, no encryption, no
proprietary codec. Over both BLE and WiFi the device sends the stored file as
is.

## Device Identity

- Device name: `PKT01_GREY_XXXXXXXX` (also the WiFi AP's SSID)
- Advertised BLE services: `5536`, `2222`
- Firmware seen: `1.3.3` (WiFi `V6`), `1.8` (WiFi `V9`). The vendor app notes
  that builds can differ while reporting the same version (a 1.8.5 re-cut added
  `APP&SCHED`).
- The device accepts one BLE connection at a time. While the vendor app (or
  anything else) is connected it stops advertising, so it can't be found.

## GATT Service Map

| Service | Characteristic | Handle | Properties | Purpose |
|---------|---------------|--------|------------|---------|
| E49A3001 | **E49A3002** | **0x002b** | Write | **Command channel** (APP& writes) |
| E49A3001 | **E49A3003** | **0x0030** | Notify | **Response channel** (MCU& responses) |
| E49A25F8 | **E49A28E1** | **0x002d** | Notify | **MP3 audio data stream** |
| E49A25F8 | E49A25E0 | 0x0024 | Write | Unknown |
| FFD0 | FFD1 | 0x0015 | Write | UART TX (unused by command protocol) |
| FFD0 | FFD2 | 0x0017 | Notify | UART RX (status beacons) |
| FFD0 | FFD3 | 0x001a | Write+Notify | UART BiDi (unused) |
| 001120A0 | 001120A1 | 0x002c | Notify | Secondary audio (legacy?) |
| 001120A0 | 001120A2 | 0x002a | Write | Unknown |
| 001120A0 | 001120A3 | 0x002f | Write+Notify | Metadata |

> **Firmware 1.8 notes.** pocket-libre writes commands to `001120A3` and
> receives `MCU&` replies there, and receives audio on `001120A1`; both work on
> 1.8. The vendor app on 1.8 (Android HCI log) writes commands to handle
> `0x002b`, gets replies on `0x0030` and audio on `0x002d`, after enabling
> notifications through the CCCDs at `0x002e` and `0x0031`. The UUID↔handle
> mapping in this table hasn't been re-verified on 1.8.
>
> Don't subscribe to `FFD2` on 1.8: the subscription never completed and the
> device dropped the connection.
>
> Under Linux/BlueZ, every notification tends to arrive about four times
> within a few milliseconds, and BLE audio occasionally contains a repeated
> 227-byte chunk. The phone's HCI log shows each notification once, so this is
> on the receiving side. Deduplicate command replies; for audio, compare the
> byte count with `MCU&U&<size>`.

## Command Reference

### Session Authentication

```
>> APP&SK&<16-char-session-key>
<< MCU&SK&OK
```
The 16-character session key authenticates the connection. Capture yours from an HCI capture of the vendor app's `APP&SK&` write (e.g. PacketLogger on macOS/iOS, or Android's Bluetooth HCI snoop log) — `pocket-libre sniff` cannot see it, since it only observes notifications on its own connection. The first 8 characters are reused as the device's WiFi AP password — treat the key as a secret.

### Device Info

| Command | Response | Notes |
|---------|----------|-------|
| `APP&BAT` | `MCU&BAT&58` | Battery percentage |
| `APP&FW` | `MCU&FW&1.3.3` | Firmware version |
| `APP&WF` | `MCU&WF&V6` | WiFi firmware version |
| `APP&SPACE` | `MCU&SPA&060846&061032` | Storage free & total (MB) |
| `APP&STE` | `MCU&STE&0` | Device state (0=idle) |
| `APP&T&YYYYMMDDHHmmss` | `MCU&T&OK` | Set device clock (the vendor app sends UTC) |
| `APP&REC&SECEN` | `MCU&REC&CON` | Recording config |
| `APP&MAC` | `MCU&MAC&<12 hex digits>` | Device MAC |
| `APP&GET&USB` | `MCU&USB&<0\|1>` | USB mass storage state; see [USB Mass Storage](#usb-mass-storage) |
| `APP&WPING` | `MCU&WPING` | Heartbeat while the WiFi AP is up |

On connect, the vendor app (1.8) sends `SK`, then `BAT`, `FW`, `GET&USB`, `MAC`,
`SPACE` and `WF` (several of them twice), `REC&SECEN`, `T&<UTC time>` and
`STE`. It lists recordings with `APP&LIST&<date>` for each of the last seven
days and the next two, not with `LIST_DIRS`, and then fetches new recordings
over BLE in the background.

Other commands named in the vendor app, not exercised here: `WIFID`, `WIFIE`,
`WIFIM&<n>` (switch the WiFi module between AP and client mode), `WIFIX`,
`WIFIJ`/`WIFIL`/`WSCAN`/`WIFIP`/`UPL`/`AUTOUP`/`SYNC`/`SCHED` (home-WiFi
upload), `OTA`/`WOTA` (firmware updates), `D&` (delete?), `PAU`, `RESU`, `STA`,
`STO`, `SHUT`, `BLE&OFF`, `LNP`, `LNS`, `LOG`. Replies seen only from the
device: `MCU&OFF` (end of a transfer), `MCU&SHUT`.

### File Listing

**List recording dates:**
```
>> APP&LIST_DIRS
<< MCU&DIRS&2026-03-26
<< MCU&DIRS&2026-03-27
<< MCU&DIRS&2026-03-28
<< MCU&DIRS_SUM&003
```

**List files for a date:**
```
>> APP&LIST&2026-03-28
<< MCU&F&2026-03-28&20260328001919&6222
<< MCU&F&2026-03-28&20260328191640&222
<< MCU&F&2026-03-28&20260328192028&3626
...
<< MCU&LIST&020
```

Format: `MCU&F&<date>&<timestamp>&<duration_seconds>`

> The trailing field is a **duration in seconds**, not a size in kilobytes.
> This was mislabelled here until firmware 1.8 field data corrected it
> ([#4](https://github.com/shahcolate/pocket-libre/issues/4)). Multiply by
> 4000 B/s (32 kbps) to estimate the size on disk.
Ends with: `MCU&LIST&<count>` (zero-padded)

### USB Mass Storage

The device can expose its storage as a USB drive. The vendor app switches this
on and off over BLE, then reads the state back:

```
>> APP&USB&1
<< MCU&USB&1
>> APP&GET&USB
<< MCU&USB&1
```

| Command | Response | Notes |
|---------|----------|-------|
| `APP&USB&1` | `MCU&USB&1` | Enable USB mass storage |
| `APP&USB&0` | `MCU&USB&0` | Disable USB mass storage |
| `APP&GET&USB` | `MCU&USB&<0\|1>` | Read the current state |

Captured from the vendor Android app (HCI snoop log) on 2026-10-03 and
verified on hardware with `pocket-libre usb on`: the device's storage shows
up as a USB drive. `GET&USB` is the only `GET&` command seen so far; others
may exist.

### BLE File Transfer

```
>> APP&U&2026-03-26&20260326014509
<< MCU&U&167798                        # File size in bytes
   [MP3 data arrives on handle 0x002d]
<< MCU&OFF                             # End of the transfer
```

The audio arrives as notifications of 244 bytes (some 192), the raw file with
no header. The last one is short, and then `MCU&OFF` follows. **The transfer
only runs while the audio characteristic is subscribed;** without a subscriber
the device sends `MCU&OFF` right away.

Throughput: about 3–4 KB/s in the original 1.3.3 capture. On firmware 1.8,
about 26 KB/s to a Linux laptop and about 65 KB/s to an Android phone. On
firmware 1.7, about 53–56 KB/s to a Mac. WiFi
transfer (below) runs at about 1 MB/s.

### WiFi Transfer (Fast) — decoded on firmware 1.8

**Works on firmware 1.8 with WiFi firmware V9**, about 0.7–1.1 MB/s, verified
byte for byte against BLE downloads of the same recordings. **Also works on
firmware 1.7 (WiFi firmware V9)**, with one difference: one transfer
connection per AP session instead of two (see step 5 below).
`pocket-libre wifi-transfer` implements it. Decoded on 2026-10-03 from the
vendor app's Android HCI snoop log during a real "Quick Transfer", then
reproduced from a Linux laptop acting as the WiFi client.

#### The vendor app's sequence

The app (Android, version 0.6.86) transferring a 44-minute recording, with
seconds since it opened the import sheet:

```
 0.0  >> APP&LIST_DIRS / APP&LIST&<date>         list the recordings
 0.7  >> APP&WIFIS           << MCU&WIFIS&0
 0.9  >> APP&WIFIO           << MCU&WIFIO         raise the AP; nothing staged before it
 1.0  >> APP&WPING           << MCU&WPING         heartbeat, then every 10 s
 3.5  >> APP&WIFI            << MCU&WIFI&<ssid>&<password>
 3.6  >> APP&WIFIS (1/s)     << MCU&WIFIS&3 ... MCU&WIFIS&2 (at 7.8)
 7.8  the phone asks Android for the network (WifiNetworkSpecifier: the "tap Accept" prompt)
52.1  the phone joins (2.4 GHz) and gets 192.168.200.2 by DHCP
55.1 >> APP&WIFIS           << MCU&WIFIS&1       a client has joined
55.3 >> APP&U&<date>&<ts>   << MCU&U&10705002    a normal BLE transfer starts (audio on 0x002d)
55.6 >> APP&U&WIFI                               0.35 s later: switch it to WiFi
56.9                        << MCU&U&WIFI, MCU&U&10705002   BLE audio stops
70.8                        << MCU&OFF           10.7 MB done
70.8 >> APP&WIFIC           << MCU&WIFIC
```

Most of the 52 s was Android finding the hidden network. On Android the phone
joins on a second, local-only interface and stays on its home WiFi, so users
never see it switch.

#### Protocol for a client

1. **BLE:** authenticate (`APP&SK&…`) and **subscribe to the audio
   characteristic** (pocket-libre: `001120A1`). Without a subscriber the device
   ends a BLE transfer at once with `MCU&OFF`, and there is nothing to switch.
2. **Raise the AP:** `APP&WIFIO` → `MCU&WIFIO`; `APP&WIFI` →
   `MCU&WIFI&<ssid>&<password>`. Poll `APP&WIFIS` about once a second, and send
   `APP&WPING` every few seconds while the AP is up.
3. **Join** the network: hidden SSID, WPA2-PSK. Wait for `MCU&WIFIS&1`.
4. **For each file:**
   1. Connect to **`192.168.200.1:8475`** (TCP) and send nothing.
   2. `APP&U&<date>&<timestamp>` → `MCU&U&<size>`.
   3. About 0.3 s later, `APP&U&WIFI` → `MCU&U&WIFI`, then `MCU&U&<size>` again
      (1.2–1.5 s after the switch).
   4. Read exactly `<size>` bytes from the socket: the **raw MP3 file from byte
      0**, no framing or headers. It does not resume after the bytes that already
      went over BLE. Then come **10 fixed bytes, `ba 5a 02 8f 04 ba 5a 02 8f
      04`**, identical for every file (not a checksum). `MCU&OFF` arrives over
      BLE at the same moment.
   5. Close the connection. The device closes its side at once, refuses new
      connections for about 1.5–3.5 s, then listens again.
5. **A limited number of transfer connections per AP session: two on 1.8, one
   on 1.7.** On 1.8, port 8475 stops listening for good after the second. On
   1.7 the device accepts a second connection, then resets it before sending
   any data. Restart the AP for more files: `APP&WIFIC`, leave the network,
   `APP&WIFIO`, rejoin, wait for `WIFIS=1` (about 14 s).
6. **Finish** with `APP&WIFIC` → `MCU&WIFIC`.

**Never send `APP&U&WIFI` without a transfer connection open.** The device
acknowledges the switch, then hangs without `MCU&OFF`, and about 14 s later
sends `MCU&SHUT`. BLE stayed up, and `APP&WIFIC` still worked, but don't rely
on it.

**WiFi Status Codes** (`MCU&WIFIS&<n>`), confirmed by timing them against the
client's join:

| Code | Meaning |
|---|---|
| `0` | AP off |
| `3` | AP coming up (right after `APP&WIFIO`) |
| `2` | AP up, waiting for a client |
| `1` | A client has joined: ready for transfer |

**WiFi AP Details (firmware 1.8; 1.7 uses the same address and port):**

| | |
|---|---|
| SSID | The device name (`PKT01_GREY_XXXXXXXX`), **hidden** |
| Security | WPA2-PSK, 2.4 GHz |
| Password | First 8 characters of the session key |
| Device | `192.168.200.1`, the DHCP server; leases `192.168.200.2/24` for 7200 s |
| Routing | No default route; DHCP names the device as DNS server, but port 53 answered on one device and was closed on another |
| Listening | Only TCP `8475`, and only while the AP is up and has transfer connections left (two per session on 1.8, one on 1.7) |

**What else was learned on the way:**

- **The vendor app sends small recordings over BLE**, even when the user
  picks Quick Transfer. Its strings say "Bluetooth is used for smaller
  recordings", with a threshold (`thresholdSeconds`) whose value isn't in the
  strings. Recordings of 23 s and 3 min went over BLE; one of 44 min went over
  WiFi.
- `APP&WIFI&SWITCH`, which appears in the app's strings, is **not implemented**
  on firmware 1.8 / V9: the device answers it like `APP&WIFI`, with the
  credentials, and the BLE transfer carries on. The app doesn't send it either.
- `RANGE` (`"RANGE request bytes="` in the app) is the **BLE** byte-range
  download, not the WiFi verb. Its neighbouring strings say "Byte-range
  downloads are Bluetooth-only".
- Earlier sweeps that found nothing on the AP (#4) most likely missed 8475
  because of timing. It only listens once the AP is up, and a client that
  re-issues its connect while the join is still in progress drops off and
  back on, after which it was gone too. NetworkManager's `nmcli connection up`
  does exactly that when retried during activation.
- The firmware 1.8 order this project documented before (`APP&U&WIFI`,
  `APP&WIFI`, stage with `APP&U&<file>`, `APP&WIFIO`) also raises the AP and
  works. The app's order shows that staging first is not required. What
  stranded the device in #4 was not reproduced in about 20 runs here: no BLE
  loss, no power-cycle needed.
- The app's strings also show a home-WiFi upload mode, where the device joins
  your router (`WIFIJ`, `WIFIL`, `WSCAN`, `WIFIP`, `UPL&…`, `AUTOUP`,
  `SYNC&…`). It is unrelated to this transfer and wasn't tested.

**Firmware 1.3.3 (historical capture, untested since):**

```
>> APP&U&WIFI
<< MCU&WIFIS&0
>> APP&WIFI
<< MCU&WIFI&PKT01_GREY_XXXXXXXX&XXXXXXXX
>> APP&WIFIO
<< MCU&WIFIO
>> APP&WIFIS                           # Poll 3 -> 2 -> 1
>> APP&U&<date>&<timestamp>
<< MCU&U&24890732
>> APP&U&WIFI                          # The switch, as on 1.8
>> APP&WIFIC
```

This is the same shape as the 1.8 protocol above. The capture was BLE-only,
which is why the socket went unnoticed. An earlier revision of this document
inferred an HTTP server at `192.168.4.1` from it, which was wrong.

**Still unknown:**

- Why the device serves only two connections per AP session (one on 1.7),
  and whether one connection can carry several files. The app has a "Multiple file download with same wifi
  socket" check. On 1.8 a second file on the same connection never arrived.
- What the 10-byte end marker encodes.
- The app's size threshold for using WiFi, and whether other firmware (newer,
  or the "T22+ / WiFi V10+" builds the app mentions) behaves the same.

## Legal Basis

- DMCA Section 1201 interoperability exemption
- Right to repair (your hardware, your data)
- No DRM circumvention (unencrypted MP3)
