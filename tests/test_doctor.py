"""doctor: setup checks, with the outside world faked."""

import json
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from click.testing import CliRunner

from pocket_libre import cli as cli_module
from pocket_libre import config as config_module
from pocket_libre import doctor
from pocket_libre.doctor import FAIL, INFO, OK, WARN, Check


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "home" / "config.toml"
    for module in (config_module, doctor, cli_module):
        monkeypatch.setattr(module, "CONFIG_FILE", path)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HUGGINGFACE_TOKEN",
                "POCKET_LIBRE_LLM_PROVIDER", "POCKET_LIBRE_LLM_MODEL",
                "POCKET_LIBRE_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return path


def _statuses(checks):
    return [(c.area, c.status) for c in checks]


# ── Config and device ───────────────────────────


def test_config_missing_unreadable_and_open(_isolated):
    assert doctor.check_config().status == WARN
    assert doctor.check_config("bad toml").status == FAIL
    _isolated.parent.mkdir()
    _isolated.write_text("")
    _isolated.chmod(0o644)
    check = doctor.check_config()
    assert check.status == WARN and "chmod 600" in check.fix
    _isolated.chmod(0o600)
    assert doctor.check_config().status == OK


@pytest.mark.parametrize("device, status", [
    ({}, FAIL),
    ({"address": "AA"}, FAIL),
    ({"address": "AA", "session_key": "short"}, WARN),
    ({"address": "AA", "session_key": "A" * 16}, OK),
])
def test_device(device, status):
    assert doctor.check_device({"device": device}).status == status


# ── Recordings ──────────────────────────────────


def _duplicated(audio):
    out = bytearray()
    for start in range(0, len(audio), 4096):
        block = audio[start:start + 4096]
        out += block + (block[-192:] if len(block) == 4096 else b"")
    return bytes(out)


# Frames as the Pocket writes them (MPEG-2 layer III, 144 bytes each).
CLEAN = b"".join(b"\xff\xf3\x48\xc4" + bytes((i * 7 + j) % 255 for j in range(140))
                 for i in range(100))


def test_output_folder_reports_what_needs_attention(tmp_path):
    root = tmp_path / "rec"
    day = root / "2026-10-03"
    day.mkdir(parents=True)
    (day / "20261003090502.mp3").write_bytes(_duplicated(CLEAN))
    (day / "20261003100000.mp3").write_bytes(CLEAN)
    (day / "20261003100000_transcript.txt").write_text("[00:01] A: hi")
    (day / ".downloads.json").write_text(json.dumps({"20261003090502.mp3": {"verified": False}}))

    checks = doctor.check_output({"output": {"directory": str(root)}})
    assert _statuses(checks) == [("Recordings", OK), ("Recordings", WARN), ("Recordings", WARN)]
    assert "2 recording(s), 1 transcript(s)" in checks[0].message
    assert "couldn't be checked" in checks[1].message
    assert "1 recording(s) have the firmware 1.8.9 duplicates" in checks[2].message
    assert f'pocket-libre repair "{root}"' in checks[2].fix


def test_output_folder_not_there_yet(tmp_path):
    (check,) = doctor.check_output({"output": {"directory": str(tmp_path / "new")}})
    assert check.status == OK and "created on the first download" in check.message


# ── Transcription and speakers ──────────────────


@pytest.mark.parametrize("whisper, ffmpeg, statuses", [
    (True, "/usr/bin/ffmpeg", [OK, OK]),
    (True, None, [OK, FAIL]),            # Whisper can't read MP3s without it
    (False, None, [WARN, WARN]),
])
def test_transcription(monkeypatch, whisper, ffmpeg, statuses):
    monkeypatch.setattr(doctor, "_installed", lambda m: whisper)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: ffmpeg)
    assert [c.status for c in doctor.check_transcription()] == statuses


def test_diarization(monkeypatch):
    monkeypatch.setattr(doctor, "_installed", lambda m: True)
    assert doctor.check_diarization({"api": {"hf_token": "hf_x"}}).status == OK
    monkeypatch.setattr(doctor, "_installed", lambda m: False)
    assert doctor.check_diarization({"api": {"hf_token": "hf_x"}}).status == WARN
    assert doctor.check_diarization({}).status == INFO


# ── Language model ──────────────────────────────


def test_no_model_says_how_to_add_one():
    check = doctor.check_llm({})
    assert check.status == WARN
    assert "llm.provider=openai" in check.fix


def test_anthropic_key_shape(monkeypatch):
    monkeypatch.setattr(doctor, "_installed", lambda m: True)
    assert doctor.check_llm({"api": {"anthropic_key": "sk-ant-x"}}).status == OK
    assert doctor.check_llm({"api": {"anthropic_key": "oops"}}).status == WARN
    monkeypatch.setattr(doctor, "_installed", lambda m: False)
    assert doctor.check_llm({"api": {"anthropic_key": "sk-ant-x"}}).status == FAIL


@pytest.fixture
def models_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": [{"id": "llama3.2:latest"}, {"id": "qwen3:14b"}]}).encode()
            self.send_response(200 if self.path == "/v1/models" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("model, status", [
    ("qwen3:14b", OK),
    ("llama3.2", OK),          # Ollama lists it with :latest
    ("mistral", FAIL),
])
def test_local_server_is_asked_for_its_models(models_server, model, status):
    check = doctor.check_llm({"llm": {"provider": "ollama", "model": model,
                                      "base_url": models_server + "/v1"}})
    assert check.status == status
    if status == FAIL:
        assert "llama3.2:latest, qwen3:14b" in check.message
        assert "ollama pull mistral" in check.fix


def test_local_server_wrong_path_and_down(models_server):
    wrong = doctor.check_llm({"llm": {"provider": "openai", "model": "m",
                                      "base_url": models_server}})
    assert wrong.status == WARN and "/v1" in wrong.fix
    down = doctor.check_llm({"llm": {"provider": "openai", "model": "m",
                                     "base_url": "http://127.0.0.1:9/v1"}})
    assert down.status == FAIL and "ollama serve" in down.fix


# ── Bluetooth ───────────────────────────────────


def _device(address, name, rssi):
    return (types.SimpleNamespace(address=address, name=name), types.SimpleNamespace(rssi=rssi))


def _fake_scan(monkeypatch, *devices):
    async def scan(timeout):
        return {d[0].address: d for d in devices}
    monkeypatch.setattr(doctor, "_scan", scan)


def test_scan_finds_the_configured_device(monkeypatch):
    _fake_scan(monkeypatch, _device("AA:BB", "PKT01_GREY", -60), _device("CC", "Govee", -40))
    check = doctor.check_scan({"device": {"address": "aa:bb"}}, timeout=0)
    assert check.status == OK and "signal -60 dBm" in check.message


def test_scan_flags_a_weak_signal_and_other_pockets(monkeypatch):
    _fake_scan(monkeypatch, _device("AA:BB", "PKT01", -92))
    assert doctor.check_scan({"device": {"address": "AA:BB"}}, timeout=0).status == WARN
    _fake_scan(monkeypatch, _device("DD:EE", "PKT01_BLACK", -50))
    check = doctor.check_scan({"device": {"address": "AA:BB"}}, timeout=0)
    assert check.status == WARN and "PKT01_BLACK (DD:EE)" in check.message


def test_scan_without_a_pocket_and_scan_failure(monkeypatch):
    _fake_scan(monkeypatch)
    assert "No Pocket in range" in doctor.check_scan({}, timeout=0).message

    async def broken(timeout):
        raise OSError("Bluetooth is off")
    monkeypatch.setattr(doctor, "_scan", broken)
    assert doctor.check_scan({}, timeout=0).status == FAIL


def test_adapter_check_reads_bluetoothctl(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/bluetoothctl")
    outputs = {"Controller 00:11 x\n\tPowered: yes\n": OK, "Controller 00:11\n\tPowered: no\n": FAIL,
               "": FAIL}
    for out, status in outputs.items():
        monkeypatch.setattr(doctor.subprocess, "run",
                            lambda *a, _o=out, **k: types.SimpleNamespace(stdout=_o))
        assert doctor.check_bluetooth_adapter().status == status
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    assert doctor.check_bluetooth_adapter() is None


# ── Connecting ──────────────────────────────────


class _Commander:
    auth = True
    error = None

    def __init__(self, address):
        pass

    async def __aenter__(self):
        if _Commander.error:
            raise _Commander.error
        return self

    async def __aexit__(self, *args):
        pass

    async def authenticate(self, key):
        return _Commander.auth

    async def get_firmware(self):
        return "1.8.9"

    async def get_battery(self):
        return 10

    async def get_storage(self):
        return 100, 3800


CONFIGURED = {"device": {"address": "AA:BB", "session_key": "A" * 16}}


def test_connect(monkeypatch):
    from pocket_libre import commands
    monkeypatch.setattr(commands, "PocketCommander", _Commander)
    _Commander.auth, _Commander.error = True, None
    checks = doctor.check_connect(CONFIGURED)
    assert _statuses(checks) == [("Connection", OK), ("Firmware", OK), ("Connection", WARN)]
    assert "Firmware 1.8.9, battery 10%, 100 of 3,800 MB used" in checks[0].message

    _Commander.auth = False
    (refused,) = doctor.check_connect(CONFIGURED)
    assert refused.status == FAIL and "pocket-libre pair" in refused.fix

    _Commander.error = TimeoutError()
    (failed,) = doctor.check_connect(CONFIGURED)
    assert failed.status == FAIL and "TimeoutError" in failed.message

    assert doctor.check_connect({})[0].status == INFO


@pytest.mark.parametrize("firmware, status", [
    ("1.8", OK), ("v1.7.2", OK), ("1.3.3", INFO), ("2.0", WARN)])
def test_firmware_note(firmware, status):
    assert doctor.firmware_note(firmware).status == status


# ── The command ─────────────────────────────────


def _run(monkeypatch, checks, *args):
    seen = {}

    def run_checks(config, config_error=None, scan=True, connect=False):
        seen.update(config_error=config_error, scan=scan, connect=connect)
        return checks
    monkeypatch.setattr(doctor, "run_checks", run_checks)
    return CliRunner().invoke(cli_module.cli, ["doctor", *args]), seen


def test_command_exits_1_only_on_a_problem(monkeypatch):
    result, seen = _run(monkeypatch, [Check("Device", OK, "fine"),
                                      Check("Language model", WARN, "none", "set one")])
    assert result.exit_code == 0
    assert "→ set one" in result.output and "0 problem(s), 1 warning(s)" in result.output
    assert seen == {"config_error": None, "scan": True, "connect": False}

    result, seen = _run(monkeypatch, [Check("Device", FAIL, "no key", "run setup")],
                        "--no-scan", "--connect")
    assert result.exit_code == 1
    assert seen["scan"] is False and seen["connect"] is True


def test_a_config_that_doesnt_parse(monkeypatch, _isolated):
    _isolated.parent.mkdir()
    _isolated.write_text("[device\naddress = ")
    result, seen = _run(monkeypatch, [Check("Config", FAIL, "bad")])
    assert seen["config_error"]                # doctor runs and reports it
    other = CliRunner().invoke(cli_module.cli, ["search", "x"])
    assert other.exit_code == 1
    assert "doesn't parse" in other.output     # every other command says so
