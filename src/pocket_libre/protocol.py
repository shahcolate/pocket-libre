"""Known BLE protocol constants for the Pocket AI recorder.

Discovered via nRF Connect and PacketLogger HCI capture analysis
on a PKT01 device.

The device uses a simple ASCII command protocol:
  - App writes "APP&<CMD>" to the command characteristic
  - Device responds "MCU&<CMD>&<DATA>" via notification
"""

# ──────────────────────────────────────────────
# Device identification
# ──────────────────────────────────────────────
DEVICE_NAME_PREFIX = "PKT01"
ADVERTISED_SERVICES = ["5536", "2222"]


# ──────────────────────────────────────────────
# Standard services
# ──────────────────────────────────────────────
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"


# ──────────────────────────────────────────────
# FFD0: UART-like control service
# ──────────────────────────────────────────────
UART_SERVICE = "0000ffd0-0000-1000-8000-00805f9b34fb"
UART_TX_CHAR = "0000ffd1-0000-1000-8000-00805f9b34fb"   # Write, Write Without Response
UART_RX_CHAR = "0000ffd2-0000-1000-8000-00805f9b34fb"   # Notify
UART_BIDI_CHAR = "0000ffd3-0000-1000-8000-00805f9b34fb"  # Write, Write Without Response, Notify


# ──────────────────────────────────────────────
# E49A3001: Custom service #1 (command channel)
# Confirmed via PacketLogger: ATT handle 0x002b (write)
# and 0x0030 (notify) carry the APP&/MCU& protocol.
# ──────────────────────────────────────────────
CUSTOM1_SERVICE = "e49a3001-f69a-11e8-8eb2-f2801f1b9fd1"
CUSTOM1_WRITE_CHAR = "e49a3002-f69a-11e8-8eb2-f2801f1b9fd1"  # ATT handle 0x002b — APP& commands
CUSTOM1_NOTIFY_CHAR = "e49a3003-f69a-11e8-8eb2-f2801f1b9fd1"  # ATT handle 0x0030 — MCU& responses


# ──────────────────────────────────────────────
# E49A25F8: Custom service #2 (audio data)
# ATT handle 0x002d carries MP3 audio notifications.
# ──────────────────────────────────────────────
CUSTOM2_SERVICE = "e49a25f8-f69a-11e8-8eb2-f2801f1b9fd1"
CUSTOM2_WRITE_CHAR = "e49a25e0-f69a-11e8-8eb2-f2801f1b9fd1"
CUSTOM2_NOTIFY_CHAR = "e49a28e1-f69a-11e8-8eb2-f2801f1b9fd1"  # ATT handle 0x002d


# ──────────────────────────────────────────────
# 001120A0: Primary service (confirmed via live testing)
# Commands go to 001120A3, audio arrives on 001120A1.
# ──────────────────────────────────────────────
AUDIO_SERVICE = "001120a0-2233-4455-6677-889912345678"
AUDIO_WRITE_CHAR = "001120a2-2233-4455-6677-889912345678"
AUDIO_DATA_CHAR = "001120a1-2233-4455-6677-889912345678"
AUDIO_META_CHAR = "001120a3-2233-4455-6677-889912345678"

# ──────────────────────────────────────────────
# Command + audio channel aliases (confirmed on firmware 1.3.3)
# ──────────────────────────────────────────────
CMD_WRITE_CHAR = AUDIO_META_CHAR      # Write APP& commands here
CMD_NOTIFY_CHAR = AUDIO_META_CHAR     # MCU& responses arrive here
AUDIO_NOTIFY_CHAR = AUDIO_DATA_CHAR   # MP3 audio data arrives here (001120A1)


# ──────────────────────────────────────────────
# Audio format (CONFIRMED via capture 2026-03-28)
# ──────────────────────────────────────────────
AUDIO_FORMAT = "mp3"
MP3_SYNC_WORD = b"\xff\xf3"
MP3_FRAME_HEADER = b"\xff\xf3\x48\xc4"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_BITRATE_KBPS = 32


# ──────────────────────────────────────────────
# APP&/MCU& Command Protocol
# (Decoded from PacketLogger HCI capture 2026-03-29)
#
# All commands are ASCII strings written to CMD_WRITE_CHAR.
# Responses arrive as notifications on CMD_NOTIFY_CHAR.
# Audio data arrives on AUDIO_NOTIFY_CHAR.
# ──────────────────────────────────────────────
CMD_PREFIX = "APP&"
RSP_PREFIX = "MCU&"

# WiFi status codes (from MCU&WIFIS&N)
WIFI_STATUS_INIT = 0       # WiFi mode initializing
WIFI_STATUS_READY = 1      # Ready for transfer
WIFI_STATUS_STARTING = 2   # AP starting up
WIFI_STATUS_CONNECTING = 3 # AP created, waiting for client


# ──────────────────────────────────────────────
# All notify characteristics (for auto-detection)
# ──────────────────────────────────────────────
ALL_NOTIFY_CHARS = [
    UART_RX_CHAR,
    UART_BIDI_CHAR,
    CMD_NOTIFY_CHAR,
    AUDIO_NOTIFY_CHAR,
    AUDIO_DATA_CHAR,
    AUDIO_META_CHAR,
]

# Priority order for audio capture auto-detection
AUDIO_CHAR_PRIORITY = [
    AUDIO_NOTIFY_CHAR,   # Primary: confirmed MP3 data on handle 0x002d
    AUDIO_DATA_CHAR,     # Secondary: 001120a1
    UART_RX_CHAR,
]

# All write characteristics (for command probing)
ALL_WRITE_CHARS = [
    CMD_WRITE_CHAR,      # Primary: APP& commands on handle 0x002b
    UART_TX_CHAR,
    UART_BIDI_CHAR,
    CUSTOM2_WRITE_CHAR,
    AUDIO_WRITE_CHAR,
    AUDIO_META_CHAR,
]
