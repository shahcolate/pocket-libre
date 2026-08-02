# Pocket AI BLE Protocol

## Protocol Overview

The Pocket device uses a simple **ASCII text command protocol** over BLE GATT:
- **App writes** `APP&<COMMAND>` to characteristic E49A3002 (ATT handle 0x002b)
- **Device responds** `MCU&<RESPONSE>` via notifications on E49A3003 (ATT handle 0x0030)
- **Audio data** streams as MP3 on E49A28E1 (ATT handle 0x002d)

Decoded via PacketLogger HCI capture analysis.

### Capture parsing tip

PacketLogger `.pklg` files interleave ASCII payloads with binary framing. After a
command like `APP&SK&XXXXXXXXXXXXXXXX`, the next bytes are often a little-endian
length field (e.g. `10 00 00 00` or `44 00 00 00`). The byte `0x44` is ASCII `D`,
so naive `strings` output can look like a 17-character session key — **it is not**.
Session keys are exactly **16** characters. Confirm with the following `MCU&SK&OK`.

## Audio Format

**MPEG-2 Layer 3 (MP3)**, 16kHz mono, ~32kbps.
Frame sync word: `0xFFF348C4`. No DRM, no encryption, no proprietary codec.

## Device Identity

- Device name: `PKT01_<COLOR>_<SUFFIX>` (examples: `PKT01_GREY_XXXXXXXX`, `PKT01_BLUE_260839da`)
- Advertised BLE services: `5536`, `2222`
- Firmware observed in the wild: `1.3.3`, `1.6` (WiFi FW `V6` / `V8`)

## Connectivity model (important)

Pocket does **not** currently upload to the internet on its own.

| Path | Role |
|------|------|
| BLE | Command/control + slow file download to phone/computer |
| Wi‑Fi SoftAP | Device creates an AP; **phone joins Pocket** for faster transfer |
| USB-C / Web Sync | Computer-mediated bulk transfer (official tooling) |
| Pocket Cloud | Upload happens from the **official phone app**, not from the device radio stack |

Wi‑Fi “Quick Transfer” is phone↔device local networking, not Pocket joining your home Wi‑Fi / cloud.

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

## Capturing the session key

You need the vendor app’s cleartext `APP&SK&` write. `pocket-libre sniff` cannot see it
(it only observes notifications on *its own* connection).

### iOS + Mac (PacketLogger)

1. Free Apple Developer login (paid membership not required).
2. On iPhone: install **Bluetooth for iOS/iPadOS** logging profile from
   [Apple Profiles and Logs](https://developer.apple.com/bug-reporting/profiles-and-logs/?platform=ios&name=bluetooth)
   (Safari on device, signed in). AirDrop from Mac often installs more reliably than a direct download.
3. On Mac: install **PacketLogger** from *Additional Tools for Xcode*.
4. USB-connect iPhone → PacketLogger → **File → New iOS Trace**.
5. Open the official Pocket app, connect/sync.
6. Search the trace for `APP&SK&`. Key = the **16 characters** after that prefix.
7. Confirm `MCU&SK&OK` appears.

### Android

Enable **Bluetooth HCI snoop log** in Developer options, reproduce connect/sync in the
official app, pull the btsnoop log, open in Wireshark, search ASCII for `APP&SK&`.

### Stability

The key behaves like a durable device credential (also used as Wi‑Fi AP password prefix),
not a one-shot nonce. It may change after factory reset / credential regeneration.
Treat it as a secret — never commit `.pklg` / snoop logs.

## Command Reference

### Session Authentication

```
>> APP&SK&<16-char-session-key>
<< MCU&SK&OK
```

The first 8 characters are reused as the device's WiFi AP password.

### Device Info

| Command | Response | Notes |
|---------|----------|-------|
| `APP&BAT` | `MCU&BAT&58` | Battery percentage |
| `APP&FW` | `MCU&FW&1.3.3` | Firmware version |
| `APP&WF` | `MCU&WF&V6` | WiFi firmware version |
| `APP&SPACE` | `MCU&SPA&060846&061032` | Storage used & total (KB) |
| `APP&STE` | `MCU&STE&0` | Device state (0=idle, 1=recording) |
| `APP&T&YYYYMMDDHHmmss` | `MCU&T&OK` | Set device clock |
| `APP&MAC` | `MCU&MAC&f44b260839da` | Device Bluetooth MAC (observed) |
| `APP&REC&SECEN` | `MCU&REC&CON` / `MCU&REC&CALL` | Recording config |
| `APP&GET&USBA` | `MCU&USB&0` | USB / accessory probe (observed) |

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
<< MCU&F&2026-03-28&PH260801230432&26
...
<< MCU&LIST&020
```

Format: `MCU&F&<date>&<timestamp>&<size_kb>`
Ends with: `MCU&LIST&<count>` (zero-padded)

**Timestamps** are usually `YYYYMMDDHHmmss`. Some files use a `PH…` prefix
(observed on phone-call style captures); pass them through unchanged to `U` / `D`.

### BLE File Transfer

```
>> APP&U&2026-03-26&20260326014509
<< MCU&U&167798                        # File size in bytes
   [MP3 data arrives on handle 0x002d]
```

Throughput: ~3-4 KB/s. A 24MB file takes ~2 hours.

### Delete Recording

```
>> APP&D&2026-08-02&PH260802164240
<< MCU&D
```

Same `<date>&<timestamp>` shape as download (`APP&U&...`). Response is bare `MCU&D`
(not `MCU&D&OK`). Decoded from PacketLogger capture of the official app’s
**Look up Device Files → Delete** flow. Official app often follows with `APP&SPACE`
to refresh storage.

Implemented in pocket-libre as `pocket-libre delete` and the web UI Delete button.

### WiFi Transfer (Fast)

Complete sequence observed from official app:

```
# 1. Trigger WiFi mode
>> APP&U&WIFI
<< MCU&WIFIS&0                         # Initializing

# 2. Get WiFi AP credentials
>> APP&WIFI
<< MCU&WIFI&PKT01_GREY_XXXXXXXX&XXXXXXXX   # SSID & password (first 8 chars of session key)

# 3. Turn on WiFi AP
>> APP&WIFIO
<< MCU&WIFIO

# 4. Poll until ready (status: 3→2→1)
>> APP&WIFIS
<< MCU&WIFIS&3                         # AP starting
>> APP&WIFIS
<< MCU&WIFIS&2                         # Almost ready
>> APP&WIFIS
<< MCU&WIFIS&1                         # Ready for transfer

# 5. Select file for transfer
>> APP&U&2026-03-28&20260328001919
<< MCU&U&24890732                       # File size in bytes

# 6. Begin WiFi transfer
>> APP&U&WIFI
<< MCU&U&WIFI
<< MCU&U&24890732                       # Confirmed

# 7. Transfer happens over WiFi HTTP (device is AP at 192.168.4.1)
# ... download completes ...

# 8. Device signals completion
<< MCU&OFF

# 9. Cleanup
>> APP&WIFIC
<< MCU&WIFIC
```

**WiFi Status Codes:**
- `0` = Initializing
- `3` = AP created, waiting for client connection
- `2` = Client connecting
- `1` = Ready for file transfer

**WiFi AP Details:**
- SSID: Device name (e.g., `PKT01_GREY_XXXXXXXX`)
- Password: First 8 chars of session key
- IP: Likely `192.168.4.1` (ESP32 SoftAP default)
- HTTP endpoint: TBD (needs probing once connected to AP)

## Still unknown / wanted

- Exact Wi‑Fi HTTP download URL/path once SoftAP is up
- Whether `MCU&D` is returned for missing files (may still ack)
- Remote start/stop record commands (if any beyond on-device controls)
- Factory wipe / format commands

## Legal Basis

- DMCA Section 1201 interoperability exemption
- Right to repair (your hardware, your data)
- No DRM circumvention (unencrypted MP3)
