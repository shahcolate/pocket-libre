# Pocket AI BLE Protocol

## Confirmed Audio Format

**MPEG-2 Layer 3 (MP3)**, 16kHz mono, ~32kbps.
Frame sync word: `0xFFF348C4`. No DRM, no encryption, no proprietary codec.

Standard MP3 that plays in any media player.

## Device Identity

- Device name: `PKT01_GREY_XXXXXXXX`
- Advertised BLE services: `5536`, `2222`
- Firmware identifier string on char 001120A3: `MCU&STA&YYYYMMDD`

## GATT Service Map

Discovered via nRF Connect and confirmed via PacketLogger HCI capture (11,935 records, 5MB).

### Service FFD0 (UART-like control)

Nordic UART Service clone. Periodic 219-byte status beacons (`ffd08eff...`) containing connection quality/RSSI data. Not used for audio control.

| Characteristic | Properties | Role |
|---------------|------------|------|
| FFD1 | Write | Send commands to device |
| FFD2 | Notify | Receive responses |
| FFD3 | Write + Notify | Bidirectional control |

### Service E49A3001-F69A-11E8-8EB2-F2801F1B9FD1

Custom service. Purpose TBD (possibly recording metadata or file listing).

| Characteristic | Properties | Role |
|---------------|------------|------|
| E49A3002 | Write | Command |
| E49A3003 | Notify | Response |

### Service E49A25F8-F69A-11E8-8EB2-F2801F1B9FD1

Custom service. Purpose TBD.

| Characteristic | Properties | Role |
|---------------|------------|------|
| E49A25E0 | Write | Command |
| E49A28E1 | Notify | Response |

### Service 001120A0-2233-4455-6677-889912345678 (AUDIO)

Primary audio data service. Confirmed streaming MP3 data.

| Characteristic | Properties | Role | Confirmed |
|---------------|------------|------|-----------|
| 001120A2 | Write | Request recordings / control | Suspected |
| **001120A1** | **Notify** | **MP3 audio stream** | **Yes** |
| 001120A3 | Write + Notify | Metadata (`MCU&STA&...`) | Yes |

## Packet Structure (from HCI capture)

### Audio Data Packets

ATT handle `0x0324`, packet prefix:

```
03 24 fb 00 f7 00 04 00 1b 2d 00 [MP3 data...]
```

- Total payload: 264 bytes (251 bytes MP3 + 13 bytes framing)
- `f7` = length field (247 = 0xf7)
- `0004001b2d00` = consistent header (possibly connection handle + ATT metadata)
- MP3 frames (`FFF348C4`) appear at various offsets within the payload due to frame boundaries not aligning with BLE packet boundaries

### Smaller Audio Packets

Occasional 208-byte packets with prefix `0324c300bf00` (length 0xc3 = 195).
These appear to be the last packet of each MP3 frame boundary cycle.

### Status Beacons (FFD0 service)

```
ffd0 8eff [timestamp] [rssi/quality data...]
```

219 bytes, appear every ~100-200 audio packets. Connection monitoring, not audio control.

## Transfer Behavior

Based on the 5MB PacketLogger capture of an official app sync:

1. **Audio starts flowing immediately** after BLE connection and GATT characteristic subscription. No obvious "request recording" write command was visible at the PKLG parsing level.

2. **Transfer is continuous.** Once started, MP3 packets arrive back-to-back with only status beacons interspersed.

3. **Multiple recordings may be concatenated.** The capture contains what appears to be a continuous MP3 stream. Recording boundaries would need to be detected by silence gaps or metadata markers.

4. **Effective throughput:** ~3-4 KB/s over BLE. A 30-minute recording at 32kbps (~7MB) would take ~30 minutes to transfer.

## What's Still Unknown

### Stored Recording Retrieval

How does the app request specific stored recordings?

Possibilities (ranked by likelihood):
1. **Auto-dump on connect:** Device sends all unsynced recordings upon connection
2. **Write to 001120A2:** A command byte triggers transfer
3. **Write to E49A3002 or E49A25E0:** Another service handles recording selection

### WiFi Transfer

The app mentions WiFi is faster. Likely:
- Device spins up a local WiFi AP or joins the same network
- Exposes HTTP endpoint for bulk download
- Uses BLE for initial handshake, hands off to WiFi

### Recording Boundaries

How to detect where one recording ends and the next begins:
- Silence detection in MP3 stream
- Metadata on 001120A3 characteristic
- Special marker packets

## Next Steps for Discovery

```bash
# Sniff all characteristics live
pocket-libre sniff --address YOUR_ADDRESS --duration 60

# Probe write characteristics
# In Python with bleak, try writing to 001120A2:
# await client.write_gatt_char("001120a2-...", b"\x01")

# WiFi AP scan after BLE connect
dns-sd -B _http._tcp local.
```

## Legal Basis

- DMCA Section 1201 interoperability exemption
- Right to repair (your hardware, your data)
- No DRM circumvention (unencrypted MP3)
