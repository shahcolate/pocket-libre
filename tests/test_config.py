"""Config resolution chain, profiles, TOML escaping, and file permissions."""

import getpass
import os
import stat
import subprocess

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


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not the ACL on Windows")
def test_config_file_is_owner_only(config_home):
    """Config holds API keys and the device session key."""
    cfg.save_config({"api": {"anthropic_key": "sk-ant-secret"}})
    mode = stat.S_IMODE(cfg.CONFIG_FILE.stat().st_mode)
    assert mode & 0o077 == 0, f"config is group/world accessible: {mode:o}"


@pytest.mark.skipif(os.name != "nt", reason="ACL hardening is Windows-only")
def test_config_file_acl_is_owner_only_on_windows(config_home):
    """`chmod` cannot restrict a file on Windows, so the ACL must be rewritten.

    `stat().st_mode` always reports 0o666 on NTFS regardless of the real
    permissions, so asserting on mode bits here tested nothing: the config kept
    the ACL it inherited and stayed readable by every account on the machine.
    """
    cfg.save_config({"api": {"anthropic_key": "sk-ant-secret"}})
    listing = subprocess.run(
        ["icacls", str(cfg.CONFIG_FILE)], capture_output=True, text=True, check=False,
    ).stdout
    user = os.environ.get("USERNAME") or getpass.getuser()

    # One ACE per `:(`, and the trustee is whatever precedes it.
    entries = listing.split(":(")
    assert len(entries) == 2, f"expected exactly one ACL entry, got {listing!r}"
    assert entries[0].rstrip().lower().endswith(user.lower()), (
        f"config is readable by someone other than its owner: {listing!r}"
    )
    assert "(I)" not in listing, "inherited entries survived; /inheritance:r did not apply"


# ── Profiles ────────────────────────────────────


def test_config_without_profiles_resolves_as_before():
    """The single-device shape must keep working untouched."""
    conf = {"device": {"address": "AA:BB", "session_key": "key"}}
    assert cfg.resolve_profile_name(conf) is None
    assert cfg.resolve_address(conf) == "AA:BB"
    assert cfg.resolve_session_key(conf) == "key"


def test_asking_for_a_profile_without_any_is_an_error():
    with pytest.raises(cfg.ProfileError, match="has no profiles"):
        cfg.resolve_profile_name({"device": {"address": "AA:BB"}}, "erika")


def test_profile_overrides_global_section():
    conf = {
        "device": {"address": "GLOBAL", "session_key": "shared"},
        "profiles": {"erika": {"address": "ERIKA"}},
    }
    assert cfg.resolve_address(conf, profile="erika") == "ERIKA"


def test_profile_without_session_key_falls_back_to_shared_one():
    """The key is issued per vendor account, so two devices may share it."""
    conf = {
        "device": {"session_key": "shared-account-key"},
        "profiles": {"erika": {"address": "ERIKA"}},
    }
    assert cfg.resolve_session_key(conf, profile="erika") == "shared-account-key"


def test_cli_value_beats_profile():
    conf = {"profiles": {"erika": {"address": "ERIKA"}}}
    assert cfg.resolve_address(conf, "FROM-CLI", profile="erika") == "FROM-CLI"


def test_env_var_beats_profile(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    conf = {"profiles": {"erika": {"anthropic_key": "from-profile"}}}
    assert cfg.resolve_anthropic_key(conf, profile="erika") == "from-env"


def test_profile_selection_order(monkeypatch):
    conf = {
        "default_profile": "oleksandr",
        "profiles": {"oleksandr": {"address": "A"}, "erika": {"address": "B"}},
    }
    assert cfg.resolve_profile_name(conf) == "oleksandr"
    monkeypatch.setenv(cfg.PROFILE_ENV_VAR, "erika")
    assert cfg.resolve_profile_name(conf) == "erika"
    assert cfg.resolve_profile_name(conf, "oleksandr") == "oleksandr"


def test_sole_profile_needs_no_default():
    conf = {"profiles": {"erika": {"address": "B"}}}
    assert cfg.resolve_profile_name(conf) == "erika"


def test_several_profiles_without_default_refuses_to_guess():
    """Guessing wrong here means touching someone else's recordings."""
    conf = {"profiles": {"oleksandr": {"address": "A"}, "erika": {"address": "B"}}}
    with pytest.raises(cfg.ProfileError, match="no default"):
        cfg.resolve_profile_name(conf)


def test_unknown_profile_name_is_rejected():
    conf = {"profiles": {"erika": {"address": "B"}}}
    with pytest.raises(cfg.ProfileError, match="No profile named 'nobody'"):
        cfg.resolve_profile_name(conf, "nobody")


def test_invalid_profile_names_are_ignored_and_reported():
    conf = {"profiles": {"Erika Taranto": {"address": "B"}}}
    assert cfg.list_profiles(conf) == {}
    assert any("ignored" in w for w in cfg.profile_warnings(conf))


def test_profile_gets_its_own_library_by_default(tmp_path):
    conf = {
        "output": {"directory": str(tmp_path)},
        "profiles": {"erika": {"address": "B"}, "oleksandr": {"address": "A"}},
    }
    assert cfg.get_output_dir(conf, profile="erika") == str(tmp_path / "erika")
    assert cfg.get_output_dir(conf, profile="oleksandr") == str(tmp_path / "oleksandr")
    assert cfg.get_output_dir(conf) == str(tmp_path)


def test_explicit_profile_directory_wins(tmp_path):
    conf = {
        "output": {"directory": str(tmp_path)},
        "profiles": {"erika": {"output_directory": str(tmp_path / "elsewhere")}},
    }
    assert cfg.get_output_dir(conf, profile="erika") == str(tmp_path / "elsewhere")


def test_cli_output_dir_is_used_verbatim(tmp_path):
    conf = {"profiles": {"erika": {"address": "B"}}}
    assert cfg.get_output_dir(conf, str(tmp_path), profile="erika") == str(tmp_path)


def test_web_ports_do_not_collide_by_default():
    conf = {"profiles": {"erika": {}, "oleksandr": {}}}
    assert cfg.resolve_web_port(conf, profile="erika") == cfg.BASE_WEB_PORT
    assert cfg.resolve_web_port(conf, profile="oleksandr") == cfg.BASE_WEB_PORT + 1
    assert cfg.resolve_web_port(conf) == cfg.BASE_WEB_PORT


def test_explicit_web_port_wins():
    conf = {"profiles": {"erika": {"web_port": 9000}}}
    assert cfg.resolve_web_port(conf, profile="erika") == 9000
    assert cfg.resolve_web_port(conf, 9100, profile="erika") == 9100


def test_label_and_accent_differ_per_profile():
    conf = {"profiles": {"erika": {"label": "Erika"}, "oleksandr": {}}}
    assert cfg.profile_label(conf, "erika") == "Erika"
    assert cfg.profile_label(conf, "oleksandr") == "Oleksandr"
    assert cfg.profile_label(conf, None) == "Pocket Libre"
    assert cfg.profile_accent(conf, "erika") != cfg.profile_accent(conf, "oleksandr")


def test_profiles_round_trip_through_the_toml_writer(config_home):
    cfg.save_config({
        "default_profile": "erika",
        "output": {"directory": "~/Pocket Libre"},
        "profiles": {
            "erika": {"address": "AA:BB", "web_port": 8266, "vault_export": False},
            "oleksandr": {"address": "CC:DD"},
        },
    })
    loaded = cfg.load_config()
    assert loaded["default_profile"] == "erika"
    assert loaded["profiles"]["erika"]["web_port"] == 8266
    assert loaded["profiles"]["erika"]["vault_export"] is False
    assert loaded["profiles"]["oleksandr"]["address"] == "CC:DD"
    assert loaded["output"]["directory"] == "~/Pocket Libre"
    assert cfg.resolve_profile_name(loaded) == "erika"


def test_warnings_catch_a_shared_library(tmp_path):
    shared = str(tmp_path / "shared")
    conf = {"profiles": {
        "erika": {"output_directory": shared},
        "oleksandr": {"output_directory": shared},
    }}
    assert any("same library" in w or "share the output directory" in w
               for w in cfg.profile_warnings(conf))


def test_warnings_catch_a_shared_port_and_address(tmp_path):
    conf = {
        "default_profile": "ghost",
        "output": {"directory": str(tmp_path)},
        "profiles": {
            "erika": {"web_port": 8265, "address": "AA:BB"},
            "oleksandr": {"web_port": 8265, "address": "aa:bb"},
        },
    }
    warnings = cfg.profile_warnings(conf)
    assert any("web port 8265" in w for w in warnings)
    assert any("same device" in w for w in warnings)
    assert any("default_profile" in w for w in warnings)
