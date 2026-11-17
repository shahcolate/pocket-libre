"""Remove the duplicated BLE notifications from recordings saved before #42.

Firmware 1.8.9 sends the short notification that closes every 4096-byte
block twice. Downloads made before the fix kept both copies, so the MP3 has
a repeated 192-byte fragment about every 4 KB, which breaks the frame chain
(ffmpeg: "Header missing") and ticks about once a second.

The repair walks the MP3 frame chain. Where it breaks, it looks just before
the break for 192 bytes that repeat the 192 before them and whose removal
lets the chain run on. Only then is anything cut, so a quiet passage, whose
bytes repeat too, is left alone unless the chain is broken right there.
"""

from __future__ import annotations

from dataclasses import dataclass

DUPLICATE_SIZE = 192

# Frames that must follow a header for it to count as the real thing.
_CONFIRM_FRAMES = 3

_BITRATES = {
    # (MPEG-1?, layer): kbit/s for indexes 1..14
    (True, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def frame_length(header: bytes) -> int | None:
    """Length of the MPEG audio frame starting with `header` (4 bytes), or
    None if it isn't a valid frame header."""
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version = (header[1] >> 3) & 3          # 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5
    layer = 4 - ((header[1] >> 1) & 3)      # bits 11/10/01 = layer I/II/III
    bitrate_index = header[2] >> 4
    rate_index = (header[2] >> 2) & 3
    if version == 1 or layer == 4 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    mpeg1 = version == 3
    bitrate = _BITRATES[(mpeg1, layer)][bitrate_index - 1] * 1000
    sample_rate = _SAMPLE_RATES[version][rate_index]
    padding = (header[2] >> 1) & 1
    if layer == 1:
        return (12 * bitrate // sample_rate + padding) * 4
    if layer == 3 and not mpeg1:
        return 72 * bitrate // sample_rate + padding
    return 144 * bitrate // sample_rate + padding


@dataclass
class RepairResult:
    data: bytes
    removed: int = 0           # duplicates cut out
    unrepaired: int = 0        # chain breaks left as they were

    @property
    def changed(self) -> bool:
        return self.removed > 0


class _Stream:
    """Frame-chain checks that stay with the stream's own format, so a stray
    0xFFF that happens to parse as some other kind of frame doesn't count."""

    def __init__(self, data: bytes, first: int):
        self.data = data
        # Version, layer, CRC flag (byte 1) and sample rate (byte 2).
        self.key = (data[first + 1], data[first + 2] & 0x0C)

    def length(self, view: bytes, pos: int) -> int | None:
        header = view[pos:pos + 4]
        if len(header) < 4 or (header[1], header[2] & 0x0C) != self.key:
            return None
        return frame_length(header)

    def chain_ok(self, view: bytes, pos: int, frames: int = _CONFIRM_FRAMES) -> bool:
        """`frames` valid frames from `pos`, or valid ones up to the end."""
        for _ in range(frames):
            if len(view) - pos < 4:
                return True
            n = self.length(view, pos)
            if n is None:
                return False
            pos += n
        return True


def _first_frame(data: bytes) -> int | None:
    pos = data.find(b"\xff")
    while pos != -1:
        if frame_length(data[pos:pos + 4]):
            stream = _Stream(data, pos)
            if stream.chain_ok(data, pos):
                return pos
        pos = data.find(b"\xff", pos + 1)
    return None


def remove_duplicates(data: bytes, size: int = DUPLICATE_SIZE) -> RepairResult:
    """`data` with the duplicated notifications cut out (see module doc).

    Bytes before the first frame and after the last are kept as they are, and
    a file without duplicates comes back unchanged.
    """
    first = _first_frame(data)
    if first is None:
        return RepairResult(data)
    stream = _Stream(data, first)
    end = len(data)

    cuts: list[int] = []        # start of each removed duplicate
    starts: list[int] = []      # frame starts since the last cut
    unrepaired = 0
    pos = first
    floor = 0                   # nothing before this may be cut again
    while end - pos >= 4:
        n = stream.length(data, pos)
        if n is not None:
            starts.append(pos)
            pos += n
            continue

        y = _find_duplicate(stream, data, pos, starts, floor, size)
        if y is not None:
            cuts.append(y)
            floor = y + size
            starts = [s for s in starts if s < y]
            pos += size
            continue

        # A break that isn't a duplicate: leave it, pick the chain up again.
        unrepaired += 1
        nxt = _resync(stream, data, pos + 1)
        if nxt is None:
            break
        starts = []
        pos = nxt

    if not cuts:
        return RepairResult(data, 0, unrepaired)
    parts, last = [], 0
    for y in cuts:
        parts.append(data[last:y])
        last = y + size
    parts.append(data[last:])
    return RepairResult(b"".join(parts), len(cuts), unrepaired)


def _find_duplicate(stream: _Stream, data: bytes, pos: int, starts: list[int],
                    floor: int, size: int) -> int | None:
    """Where the duplicate that broke the chain at `pos` starts, or None.

    The duplicate went in somewhere inside the last frame before the break
    (or the last few, if its copy happened to hold frame-shaped bytes), or
    inside the 4-byte header at the break itself. The
    nearest candidate that repeats the bytes before it and lets the chain run
    from a frame before it through the cut wins. Neighbouring candidates
    that both repeat leave the same bytes behind, so which of them is cut
    doesn't matter.
    """
    if not starts:
        return None
    lowest = max(floor + size, size, starts[max(0, len(starts) - 4)] + 1)
    for y in range(pos + 3, lowest - 1, -1):
        if data[y:y + size] != data[y - size:y]:
            continue
        anchor = max((s for s in starts if s < y), default=None)
        if anchor is None:
            continue
        view = data[anchor:y] + data[y + size:y + size + 8 * 1441]
        if _runs_past(stream, view, y - anchor):
            return y
    return None


def _runs_past(stream: _Stream, view: bytes, cut: int) -> bool:
    """True if the chain from the start of `view` crosses `cut` and keeps
    going for a few frames."""
    pos = 0
    while pos <= cut:
        n = stream.length(view, pos)
        if n is None:
            return False
        pos += n
    return stream.chain_ok(view, pos)


def _resync(stream: _Stream, data: bytes, pos: int) -> int | None:
    pos = data.find(b"\xff", pos)
    while pos != -1:
        if stream.length(data, pos) and stream.chain_ok(data, pos):
            return pos
        pos = data.find(b"\xff", pos + 1)
    return None


BACKUP_SUFFIX = ".orig"


def repair_file(path, write: bool = True, backup: bool = True) -> RepairResult:
    """Remove the duplicates from the recording at `path`.

    With `write`, a changed file is replaced through a .part file, and the
    original kept next to it as <name>.orig (unless `backup` is off, or an
    .orig from an earlier repair is already there: that one is the real
    original). A verified download's recorded size moves to the new size, so
    the device copy can still be deleted once it's safely downloaded.
    """
    from pathlib import Path

    from pocket_libre.commands import _downloads, _set_entry

    path = Path(path)
    data = path.read_bytes()
    result = remove_duplicates(data)
    if not (write and result.changed):
        return result

    if backup:
        original = path.with_name(path.name + BACKUP_SUFFIX)
        if not original.exists():
            original.write_bytes(data)
    partial = path.with_name(path.name + ".part")
    try:
        partial.write_bytes(result.data)
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    entry = _downloads(path.parent).get(path.name)
    if isinstance(entry, dict) and entry.get("size") == len(data):
        _set_entry(path, {**entry, "size": len(result.data)})
    return result
