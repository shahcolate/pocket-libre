"""Config resolution chain, TOML escaping, and file permissions."""

import stat

import pytest

from pocket_libre import config as cfg


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Redirect the module-level config paths into a temp directory."""
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / ".pocket-libre")
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / ".pocket-libre" / "config.toml")
    return tmp_path


# ── Resolution chain ────────────────────────────


def test_cli_value_wins_over_everything(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    conf = {"api": {"anthropic_key": "from-file"}}
    assert cfg.resolve_anthropic_key(conf, "from-cli") == "from-cli"


def test_env_wins_over_config_file(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    conf = {"api": {"anthropic_key": "from-file"}}
    assert cfg.resolve_anthropic_key(conf) == "from-env"


def test_config_file_wins_over_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conf = {"api": {"anthropic_key": "from-file"}}
    assert cfg.resolve_anthropic_key(conf) == "from-file"


def test_empty_string_is_treated_as_unset(monkeypatch):
    """An empty CLI flag must not shadow a real configured value."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conf = {"api": {"anthropic_key": "from-file"}}
    assert cfg.resolve_anthropic_key(conf, "") == "from-file"


def test_falls_back_to_defaults_when_absent(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cfg.get({}, "defaults", "whisper_model") == "base.en"


def test_missing_session_key_returns_empty(monkeypatch):
    assert cfg.resolve_session_key({}) == ""


def test_output_dir_expands_user(monkeypatch):
    resolved = cfg.get_output_dir({"output": {"directory": "~/somewhere"}})
    assert not resolved.startswith("~")
    assert resolved.endswith("somewhere")


# ── Round-tripping ──────────────────────────────


def test_save_load_roundtrip(config_home):
    original = {
        "device": {"address": "AA:BB:CC", "session_key": "abcd1234abcd1234"},
        "defaults": {"whisper_model": "small.en"},
    }
    cfg.save_config(original)
    assert cfg.load_config() == original


def test_load_missing_file_returns_empty(config_home):
    assert cfg.load_config() == {}


def test_windows_path_survives_roundtrip(config_home):
    """Backslashes must not be re-interpreted as TOML escapes."""
    cfg.save_config({"output": {"directory": r"C:\Users\me\Pocket"}})
    assert cfg.load_config()["output"]["directory"] == r"C:\Users\me\Pocket"


@pytest.mark.parametrize(
    "value",
    [
        'has "quotes"',
        "line\nbreak",
        "carriage\rreturn",
        "tab\there",
        'mixed "\\" \n chaos',
        "null\x00byte",
    ],
)
def test_control_characters_survive_roundtrip(config_home, value):
    """Regression: an unescaped newline produced an unparseable config,
    silently wiping every setting on the next load."""
    cfg.save_config({"output": {"directory": value}})
    assert cfg.load_config()["output"]["directory"] == value


def test_value_cannot_inject_toml_sections(config_home):
    """A crafted value must not be able to forge a new config section."""
    cfg.save_config({"output": {"directory": 'x"\n[api]\nanthropic_key = "stolen'}})
    loaded = cfg.load_config()
    assert loaded.get("api", {}).get("anthropic_key") != "stolen"
    assert list(loaded) == ["output"]


def test_bool_and_int_types_preserved(config_home):
    cfg.save_config({"defaults": {"enabled": True, "count": 7}})
    loaded = cfg.load_config()
    assert loaded["defaults"]["enabled"] is True
    assert loaded["defaults"]["count"] == 7


def test_config_file_is_owner_only(config_home):
    """Config holds API keys and the device session key."""
    cfg.save_config({"api": {"anthropic_key": "sk-ant-secret"}})
    mode = stat.S_IMODE(cfg.CONFIG_FILE.stat().st_mode)
    assert mode & 0o077 == 0, f"config is group/world accessible: {mode:o}"
