"""repair: recordings saved with the duplicated notifications of #42."""

import json
import random

import pytest
from click.testing import CliRunner

from pocket_libre.cli import cli
from pocket_libre.commands import DOWNLOADS_FILE
from pocket_libre.mp3repair import frame_length, remove_duplicates, repair_file

HEADER = b"\xff\xf3\x48\xc4"     # MPEG-2 layer III, 32 kbit/s, 16 kHz: 144 bytes


def _mp3(frames: int, silent_from: int | None = None) -> bytes:
    """Frames as the Pocket writes them. Payload bytes stay below 0xFF so no
    header turns up by chance; frames from `silent_from` on are identical,
    as they are in a quiet passage."""
    rng = random.Random(0)
    out = bytearray()
    for i in range(frames):
        if silent_from is not None and i >= silent_from:
            out += HEADER + b"\x00" * 140
        else:
            out += HEADER + bytes(rng.randrange(255) for _ in range(140))
    return bytes(out)


def _duplicated(audio: bytes) -> bytes:
    """What a download on firmware 1.8.9 saved before the fix: the last 192
    bytes of every full 4096-byte block twice."""
    out = bytearray()
    for start in range(0, len(audio), 4096):
        block = audio[start:start + 4096]
        out += block
        if len(block) == 4096:
            out += block[-192:]
    return bytes(out)


CLEAN = _mp3(300, silent_from=150)     # ~43 KB, half of it silence


def test_frame_length():
    assert frame_length(HEADER) == 144
    assert frame_length(b"\xff\xfb\x90\xc4") == 417       # MPEG-1, 128k, 44.1k
    assert frame_length(b"\xff\xfb\x92\xc4") == 418       # same, padded
    assert frame_length(b"\xff\xf3\xf8\xc4") is None      # bad bitrate index
    assert frame_length(b"\x00\xf3\x48\xc4") is None


def test_duplicates_are_removed_through_silence_too():
    broken = _duplicated(CLEAN)
    result = remove_duplicates(broken)
    assert result.data == CLEAN
    assert result.removed == len(CLEAN) // 4096
    assert result.unrepaired == 0


@pytest.mark.parametrize("audio", [CLEAN, _mp3(200, silent_from=0)], ids=["mixed", "silence"])
def test_a_healthy_file_is_left_alone(audio):
    result = remove_duplicates(audio)
    assert result.data == audio
    assert not result.changed


def test_bytes_before_the_first_frame_are_kept():
    lead = b"\x01\x02\x03"
    assert remove_duplicates(_duplicated(lead + CLEAN)).data == lead + CLEAN


def test_not_mp3_is_left_alone():
    junk = bytes(range(250)) * 40
    assert remove_duplicates(junk).data == junk


def test_a_break_that_isnt_a_duplicate_is_left():
    damaged = CLEAN[:1000] + b"\x00" * 50 + CLEAN[1000:]
    result = remove_duplicates(damaged)
    assert result.data == damaged
    assert result.unrepaired == 1


def _saved(tmp_path, data: bytes, verified: bool = True):
    path = tmp_path / "20261003090502.mp3"
    path.write_bytes(data)
    entry = {"size": len(data), "duration_s": 60} if verified else {"verified": False}
    (tmp_path / DOWNLOADS_FILE).write_text(json.dumps({path.name: entry}))
    return path


def _entry(path):
    return json.loads((path.parent / DOWNLOADS_FILE).read_text())[path.name]


def test_repair_file_keeps_the_original_and_moves_the_recorded_size(tmp_path):
    broken = _duplicated(CLEAN)
    path = _saved(tmp_path, broken)
    assert repair_file(path).changed
    assert path.read_bytes() == CLEAN
    assert (tmp_path / (path.name + ".orig")).read_bytes() == broken
    assert _entry(path) == {"size": len(CLEAN), "duration_s": 60}

    # A second run finds nothing, and keeps the real original.
    assert not repair_file(path).changed
    assert (tmp_path / (path.name + ".orig")).read_bytes() == broken


def test_repair_file_leaves_an_unverified_record_alone(tmp_path):
    path = _saved(tmp_path, _duplicated(CLEAN), verified=False)
    repair_file(path, backup=False)
    assert _entry(path) == {"verified": False}
    assert not (tmp_path / (path.name + ".orig")).exists()


def test_dry_run_writes_nothing(tmp_path):
    broken = _duplicated(CLEAN)
    path = _saved(tmp_path, broken)
    assert repair_file(path, write=False).changed
    assert path.read_bytes() == broken
    assert _entry(path)["size"] == len(broken)


def test_repair_command_walks_folders(tmp_path):
    day = tmp_path / "2026-10-03"
    day.mkdir()
    (day / "a.mp3").write_bytes(_duplicated(CLEAN))
    (day / "b.mp3").write_bytes(CLEAN)
    result = CliRunner().invoke(cli, ["repair", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (day / "a.mp3").read_bytes() == CLEAN
    assert "Fixed 1 file(s); 1 had nothing to fix." in result.output
