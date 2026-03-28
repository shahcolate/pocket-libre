"""Known BLE protocol constants for the Pocket AI recorder.

Discovered via nRF Connect on device PKT01_GREY_XXXXXXXX.
These UUIDs map the device's GATT service tree.
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
# Pattern matches Nordic UART Service clones.
# Likely used for general command/control between
# the app and device (settings, sync triggers, etc.)
# ──────────────────────────────────────────────
UART_SERVICE = "0000ffd0-0000-1000-8000-00805f9b34fb"

# Write commands to device
UART_TX_CHAR = "0000ffd1-0000-1000-8000-00805f9b34fb"  # Write, Write Without Response

# Receive responses from device
UART_RX_CHAR = "0000ffd2-0000-1000-8000-00805f9b34fb"  # Notify

# Bidirectional (command + response on same char)
UART_BIDI_CHAR = "0000ffd3-0000-1000-8000-00805f9b34fb"  # Write, Write Without Response, Notify


# ──────────────────────────────────────────────
# E49A3001: Custom service #1
# Write/Notify pair. Purpose TBD.
# Could be file listing, recording metadata,
# or a secondary control channel.
# ──────────────────────────────────────────────
CUSTOM1_SERVICE = "e49a3001-f69a-11e8-8eb2-f2801f1b9fd1"
CUSTOM1_WRITE_CHAR = "e49a3002-f69a-11e8-8eb2-f2801f1b9fd1"  # Write, Write Without Response
CUSTOM1_NOTIFY_CHAR = "e49a3003-f69a-11e8-8eb2-f2801f1b9fd1"  # Notify


# ──────────────────────────────────────────────
# E49A25F8: Custom service #2
# Write/Notify pair. Purpose TBD.
# ──────────────────────────────────────────────
CUSTOM2_SERVICE = "e49a25f8-f69a-11e8-8eb2-f2801f1b9fd1"
CUSTOM2_WRITE_CHAR = "e49a25e0-f69a-11e8-8eb2-f2801f1b9fd1"  # Write, Write Without Response
CUSTOM2_NOTIFY_CHAR = "e49a28e1-f69a-11e8-8eb2-f2801f1b9fd1"  # Notify


# ──────────────────────────────────────────────
# 001120A0: Audio data service (HIGH CONFIDENCE)
#
# This service was observed actively streaming
# large binary payloads via 001120A1 without any
# explicit trigger command. This is the most likely
# candidate for audio data transfer.
# ──────────────────────────────────────────────
AUDIO_SERVICE = "001120a0-2233-4455-6677-889912345678"

# Command channel: write to request recordings, control transfer
AUDIO_WRITE_CHAR = "001120a2-2233-4455-6677-889912345678"  # Write Without Response

# PRIMARY AUDIO DATA STREAM
# Observed streaming large hex payloads unprompted.
# Subscribe to this characteristic to receive audio data.
AUDIO_DATA_CHAR = "001120a1-2233-4455-6677-889912345678"  # Notify

# Metadata/status channel
# Observed value: 4D43 5526 5526 3732 3030 3131 3938
# Decodes partially to ASCII "MCU&..." - likely firmware/device info
AUDIO_META_CHAR = "001120a3-2233-4455-6677-889912345678"  # Write Without Response, Notify


# ──────────────────────────────────────────────
# Audio format (CONFIRMED via capture 2026-03-28)
# ──────────────────────────────────────────────
# The device streams standard MP3 frames over BLE.
# Frame sync word: 0xFFF3
# Header: 0xFFF348C4 = MPEG-2, Layer 3, mono, ~16kHz, ~32kbps
# No proprietary codec. No encryption. Just MP3.
#
# Captured data can be saved directly as .mp3 and played.
# The first frame may contain silence padding (0x55 bytes).

AUDIO_FORMAT = "mp3"
MP3_SYNC_WORD = b"\xff\xf3"
MP3_FRAME_HEADER = b"\xff\xf3\x48\xc4"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_BITRATE_KBPS = 32


# ──────────────────────────────────────────────
# All notify characteristics (for auto-detection)
# ──────────────────────────────────────────────
ALL_NOTIFY_CHARS = [
    UART_RX_CHAR,
    UART_BIDI_CHAR,
    CUSTOM1_NOTIFY_CHAR,
    CUSTOM2_NOTIFY_CHAR,
    AUDIO_DATA_CHAR,
    AUDIO_META_CHAR,
]

# Priority order for audio capture auto-detection
AUDIO_CHAR_PRIORITY = [
    AUDIO_DATA_CHAR,      # Most likely: was actively streaming
    CUSTOM1_NOTIFY_CHAR,  # Could be audio on a different channel
    CUSTOM2_NOTIFY_CHAR,  # Could be audio on a different channel
    UART_RX_CHAR,         # Less likely but possible
]
