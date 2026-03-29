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
>> APP&SK&xJiEbRKnKrhCqvoZ
<< MCU&SK&OK
```
Session key authenticates the connection. First 8 chars (`xJiEbRKn`) are reused as the WiFi AP password.

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

Format: `MCU&F&<date>&<timestamp>&<size_kb>`
Ends with: `MCU&LIST&<count>` (zero-padded)

### BLE File Transfer

```
>> APP&U&2026-03-26&20260326014509
<< MCU&U&167798                        # File size in bytes
   [MP3 data arrives on handle 0x002d]
```

Throughput: ~3-4 KB/s. A 24MB file takes ~2 hours.

### WiFi Transfer (Fast)

Complete sequence observed from official app:

```
# 1. Trigger WiFi mode
>> APP&U&WIFI
<< MCU&WIFIS&0                         # Initializing

# 2. Get WiFi AP credentials
>> APP&WIFI
<< MCU&WIFI&PKT01_GREY_XXXXXXXX&xJiEbRKn   # SSID & password

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

## Legal Basis

- DMCA Section 1201 interoperability exemption
- Right to repair (your hardware, your data)
- No DRM circumvention (unencrypted MP3)
