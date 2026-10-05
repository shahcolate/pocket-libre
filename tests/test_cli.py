"""CLI behaviour for the usb command, with the BLE commander faked out."""

import pytest
from click.testing import CliRunner

from pocket_libre import cli as cli_module


class FakeCommander:
    """Stands in for PocketCommander; records which USB calls were made."""

    def __init__(self, auth_ok=True, set_state=None, get_state=None):
        self.auth_ok = auth_ok
        self.set_state = set_state
        self.get_state = get_state
        self.calls = []

    def __call__(self, address):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def authenticate(self, session_key):
        return self.auth_ok

    async def set_usb(self, enabled):
        self.calls.append(("set", enabled))
        return self.set_state

    async def get_usb(self):
        self.calls.append(("get",))
        return self.get_state


@pytest.fixture
def run_usb(monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: {})

    def _run(fake, *args):
        monkeypatch.setattr(cli_module, "PocketCommander", fake)
        return CliRunner().invoke(
            cli_module.cli,
            ["usb", *args, "--address", "AA:BB:CC:DD:EE:FF", "--key", "00" * 16],
        )

    return _run


def test_usb_auth_failure_exits_nonzero(run_usb):
    fake = FakeCommander(auth_ok=False)
    result = run_usb(fake, "on")
    assert result.exit_code == 1
    assert "Authentication failed" in result.output
    assert fake.calls == []


def test_usb_on_uses_state_from_set_reply(run_usb):
    fake = FakeCommander(set_state=True)
    result = run_usb(fake, "on")
    assert result.exit_code == 0
    assert fake.calls == [("set", True)]
    assert "USB mass storage: on" in result.output


def test_usb_set_falls_back_to_get_when_reply_has_no_state(run_usb):
    fake = FakeCommander(set_state=None, get_state=False)
    result = run_usb(fake, "off")
    assert result.exit_code == 0
    assert fake.calls == [("set", False), ("get",)]
    assert "USB mass storage: off" in result.output


def test_usb_set_reports_mismatch(run_usb):
    fake = FakeCommander(set_state=False)
    result = run_usb(fake, "on")
    assert result.exit_code == 1
    assert "did not switch USB on" in result.output


def test_usb_status_only_queries(run_usb):
    fake = FakeCommander(get_state=True)
    result = run_usb(fake)
    assert result.exit_code == 0
    assert fake.calls == [("get",)]


def test_usb_status_unknown_exits_nonzero(run_usb):
    fake = FakeCommander(get_state=None)
    result = run_usb(fake, "status")
    assert result.exit_code == 1
    assert "did not report a USB state" in result.output


# ── Profile selection ───────────────────────────


TWO_PROFILES = {
    "default_profile": "oleksandr",
    "device": {"session_key": "SHAREDACCOUNT123"},
    "output": {"directory": "/library"},
    "profiles": {
        "oleksandr": {"label": "Oleksandr", "address": "AA:BB:CC:DD:EE:01"},
        "erika": {"label": "Erika", "address": "AA:BB:CC:DD:EE:02"},
    },
}


@pytest.fixture
def with_config(monkeypatch):
    """Serve a fixed config to the CLI, and capture what any command would save."""
    saved = {}

    def _install(config):
        monkeypatch.setattr(cli_module, "load_config", lambda: config)
        monkeypatch.setattr(cli_module, "save_config", lambda c: saved.update({"config": c}))
        return saved

    return _install


def test_profiles_lists_each_recorder(with_config):
    with_config(TWO_PROFILES)
    result = CliRunner().invoke(cli_module.cli, ["profiles"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "oleksandr" in result.output
    assert "erika" in result.output
    # Separate libraries are the whole point: they must not share a directory.
    assert "oleksandr" in result.output and "erika" in result.output
    assert "shared" in result.output  # session key falls back to [device]


class StatusRecorder:
    """A commander that answers `status` and records which address it was given.

    Deliberately not a FakeCommander subclass: that one keeps a `get_state`
    attribute, which would shadow the method `status` calls.
    """

    def __init__(self):
        self.address = None

    def __call__(self, address):
        self.address = address
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def authenticate(self, session_key):
        return True

    async def get_battery(self):
        return 80

    async def get_firmware(self):
        return "1.8"

    async def get_storage(self):
        return (15, 59634)

    async def get_state(self):
        return 0

    async def set_time(self):
        return True


def test_status_uses_the_selected_profiles_device(with_config, monkeypatch):
    with_config(TWO_PROFILES)
    recorder = StatusRecorder()
    monkeypatch.setattr(cli_module, "PocketCommander", recorder)
    result = CliRunner().invoke(cli_module.cli, ["--profile", "erika", "status"])
    assert result.exit_code == 0
    assert recorder.address == "AA:BB:CC:DD:EE:02"


def test_default_profile_is_used_when_none_given(with_config, monkeypatch):
    with_config(TWO_PROFILES)
    recorder = StatusRecorder()
    monkeypatch.setattr(cli_module, "PocketCommander", recorder)
    result = CliRunner().invoke(cli_module.cli, ["status"])
    assert result.exit_code == 0
    assert recorder.address == "AA:BB:CC:DD:EE:01"


def test_unknown_profile_is_refused(with_config):
    with_config(TWO_PROFILES)
    result = CliRunner().invoke(cli_module.cli, ["--profile", "nobody", "status"])
    assert result.exit_code != 0
    assert "No profile named 'nobody'" in result.output


def test_device_command_refuses_to_guess_between_profiles(with_config):
    ambiguous = {k: v for k, v in TWO_PROFILES.items() if k != "default_profile"}
    with_config(ambiguous)
    result = CliRunner().invoke(cli_module.cli, ["status"])
    assert result.exit_code != 0
    assert "no default" in result.output


def test_profiles_command_still_works_when_the_choice_is_ambiguous(with_config):
    """'profiles' is what you run to fix the ambiguity, so it must not need it resolved."""
    ambiguous = {k: v for k, v in TWO_PROFILES.items() if k != "default_profile"}
    with_config(ambiguous)
    result = CliRunner().invoke(cli_module.cli, ["profiles"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "erika" in result.output


def test_setup_insists_on_a_profile_when_the_choice_is_ambiguous(with_config):
    ambiguous = {k: v for k, v in TWO_PROFILES.items() if k != "default_profile"}
    with_config(ambiguous)
    result = CliRunner().invoke(cli_module.cli, ["setup"])
    assert result.exit_code != 0
    assert "--profile" in result.output


def test_config_set_writes_into_a_profile_table(with_config):
    saved = with_config(dict(TWO_PROFILES))
    result = CliRunner().invoke(
        cli_module.cli, ["config", "--set", "profiles.erika.address=NEW"],
    )
    assert result.exit_code == 0
    assert saved["config"]["profiles"]["erika"]["address"] == "NEW"
    # The other profile is untouched.
    assert saved["config"]["profiles"]["oleksandr"]["address"] == "AA:BB:CC:DD:EE:01"


def test_config_set_rejects_an_unplaceable_path(with_config):
    with_config(dict(TWO_PROFILES))
    result = CliRunner().invoke(cli_module.cli, ["config", "--set", "a.b.c.d=1"])
    assert result.exit_code != 0


def test_config_never_saves_a_profile_folded_config(with_config):
    """Saving the folded view would flatten one profile over the globals."""
    from pocket_libre import config as cfg

    saved = with_config(dict(TWO_PROFILES))
    result = CliRunner().invoke(
        cli_module.cli, ["--profile", "erika", "config", "--set", "api.hf_token=hf_x"],
    )
    assert result.exit_code == 0
    assert cfg.EFFECTIVE_MARKER not in saved["config"]
    assert saved["config"]["profiles"]["oleksandr"]["address"] == "AA:BB:CC:DD:EE:01"


def test_library_commands_report_a_missing_library_instead_of_crashing(
    with_config, tmp_path,
):
    """A fresh profile has no library yet; that is not a stack trace."""
    config = {
        "default_profile": "erika",
        "output": {"directory": str(tmp_path / "nothing-here")},
        "profiles": {"erika": {"address": "AA:01"}},
    }
    with_config(config)

    for args in (
        ["export", "--to", str(tmp_path / "out")],
        ["search", "anything"],
        ["tasks"],
    ):
        result = CliRunner().invoke(cli_module.cli, args)
        assert result.exit_code != 0, args
        assert "No library at" in result.output, args
        assert not isinstance(result.exception, FileNotFoundError), args
