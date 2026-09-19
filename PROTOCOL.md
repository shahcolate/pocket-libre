# Pocket AI BLE Protocol

## Protocol Overview

The Pocket device uses a simple **ASCII text command protocol** over BLE GATT:
- **App writes** `APP&<COMMAND>` to characteristic E49A3002 (ATT handle 0x002b)
- **Device responds** `MCU&<RESPONSE>` via notifications on E49A3003 (ATT handle 0x0030)
- **Audio data** streams as MP3 on E49A28E1 (ATT handle 0x002d)

Decoded via PacketLogger HCI capture analysis.

## Audio Format

**MPEG-2 Layer 3 (MP3)**, 16kHz mono, ~32kbps.
Frame sync word: `0xFFF348C4`. No DRM, no encryption, no proprietary codec.

## Device Identity

- Device name: `PKT01_GREY_XXXXXXXX`
- Advertised BLE services: `5536`, `2222`
- Firmware: `1.3.3`

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
| `APP&SPACE` | `MCU&SPA&060846&061032` | Storage used & total (KB) |
| `APP&STE` | `MCU&STE&0` | Device state (0=idle) |
| `APP&T&YYYYMMDDHHmmss` | `MCU&T&OK` | Set device clock |
| `APP&REC&SECEN` | `MCU&REC&CON` | Recording config |

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

### BLE File Transfer

```
>> APP&U&2026-03-26&20260326014509
<< MCU&U&167798                        # File size in bytes
   [MP3 data arrives on handle 0x002d]
```

Throughput: ~3-4 KB/s. A 24MB file takes ~2 hours.

### WiFi Transfer (Fast) — PARTIALLY DECODED

> **Firmware matters here.** The sequence below was captured from the
> official app on **firmware 1.3.3**. On **firmware 1.8** it does not work:
> no SSID is ever broadcast, and the device tears down BLE a few seconds
> later, after which it only returns following a **physical power-cycle**.
> `APP&WIFIC` cannot recover it, because BLE is already gone.
>
> Use the firmware 1.8 order below instead. Reported with reproductions in
> [#4](https://github.com/shahcolate/pocket-libre/issues/4).

**Firmware 1.3.3 (as captured — do not use on 1.8):**

```
>> APP&U&WIFI
<< MCU&WIFIS&0                         # Initializing
>> APP&WIFI
<< MCU&WIFI&PKT01_GREY_XXXXXXXX&XXXXXXXX   # SSID & password
>> APP&WIFIO                           # <-- AP raised BEFORE staging
<< MCU&WIFIO
>> APP&WIFIS                           # Poll 3 -> 2 -> 1
>> APP&U&<date>&<timestamp>            # Stage file
<< MCU&U&24890732                      # File size in bytes
>> APP&U&WIFI                          # Begin transfer
>> APP&WIFIC                           # Cleanup
```

**Firmware 1.8 (confirmed working, 4/4 reproductions):**

Stage the file **before** raising the AP, and join the network
**concurrently** with the status poll — the AP window is only seconds wide.

```
>> APP&U&WIFI
   (no response on 1.8)
>> APP&WIFI
<< MCU&WIFI&<ssid>&<8 chars>           # Password = first 8 chars of session key
>> APP&U&2026-09-03&20260903145856     # <-- STAGE FIRST
<< MCU&U&6653128                       # File size in bytes
>> APP&WIFIO
<< MCU&WIFIO
<< MCU&OFF                             # Meaning unclear, see below
   t+0.8s  << MCU&WIFIS&3              # AP created, waiting for a client
   t+8.0s  << MCU&WIFIS&2              # Client connecting
   t+9.8s  << MCU&WIFIS&1              # Ready for transfer
```

BLE survives this order, and no power-cycle is needed afterwards.

Joined-state details (firmware 1.8):

| | |
|---|---|
| Client IP | `192.168.200.2/24` (DHCP from the device) |
| Gateway | `192.168.200.1` (the device) |
| Security | WPA2-PSK, 2.4 GHz |
| AP password | First 8 characters of the session key |

**What happens on the AP is NOT decoded.**

An earlier revision of this document asserted the device serves files over
HTTP at `192.168.4.1`. That was inference from a BLE-only capture, which
cannot observe a WiFi transfer, and it was wrong. Field data from firmware
1.8 found:

- All 65535 TCP ports swept on `192.168.200.1`: only **53** open (captive-portal
  DNS), identical before and after `APP&U&WIFI`.
- No mDNS/Bonjour advertisement, no UDP replies, no third host on the subnet.
- Nothing connects back to the client either: ~20k TCP listeners and ~4k UDP
  receivers on ports 1024-21024 saw no inbound connection after staging.

Strings from the vendor app describe a **framed socket protocol**, not HTTP:
`PocketWifiFileTransferClient`, `PocketWifiFilePacketParser`, and exceptions
for invalid frames, truncated frames and checksums. The transfer verb is
`RANGE` (`"RANGE request bytes="`, `"RANGE complete received="`,
`"RANGE cancel generation="`). The port is a compiled-in integer constant and
has not been recovered.

Still unknown:

- The socket port, and whether it ever opens on firmware 1.8.
- The `RANGE` frame format (header, length, checksum layout).
- Whether the `MCU&OFF` that arrives right after `APP&WIFIO` means the staged
  transfer is being torn down. It is documented below as transfer completion,
  but that came from the 1.3.3 capture and may be wrong.
- What, if anything, causes the device to raise the transfer listener.

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

## Legal Basis

- DMCA Section 1201 interoperability exemption
- Right to repair (your hardware, your data)
- No DRM circumvention (unencrypted MP3)
