"""Configuration management for Pocket Libre.

Loads/saves config from ~/.pocket-libre/config.toml.
Resolution chain: CLI flag > env var > profile > config file > default.

A config with no ``[profiles.*]`` tables is the single-device shape this project
started with, and every resolver below behaves exactly as it did then. Profiles
are additive: declare one per recorder and each gets its own library, its own
web port, and its own credentials.
"""

import getpass
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".pocket-libre"
CONFIG_FILE = CONFIG_DIR / "config.toml"

PROFILES_SECTION = "profiles"
DEFAULT_PROFILE_KEY = "default_profile"
PROFILE_ENV_VAR = "POCKET_LIBRE_PROFILE"
BASE_WEB_PORT = 8265

# Profile names end up in filesystem paths, so keep them to a plain slug.
VALID_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Handed out in name order when a profile does not pick its own accent.
ACCENT_PALETTE = ("#5eead4", "#f0abfc", "#fcd34d", "#93c5fd", "#fca5a5")

# Defaults
DEFAULTS = {
    "device": {
        "address": "",
        "session_key": "",
    },
    "api": {
        "anthropic_key": "",
        "hf_token": "",
    },
    "output": {
        "directory": "~/Pocket Libre",
    },
    "defaults": {
        "whisper_model": "base.en",
        "summary_style": "meeting",
        "language": "auto",
        "transcribe_backend": "openai-whisper",
        "faster_whisper_path": "",
        "device": "cuda",
        "max_speakers": 0,
    },
    "analysis": {
        "enabled": "summary,entities",
        "custom_prompts": "",
    },
}

# A [profiles.<name>] table is flat and meant to be edited by hand, so this maps
# each of its keys onto the (section, key) pair it overrides in the global config.
# Single source of truth: the reverse lookup is derived from it below.
PROFILE_KEY_TARGETS = {
    "address": ("device", "address"),
    "session_key": ("device", "session_key"),
    "output_directory": ("output", "directory"),
    "anthropic_key": ("api", "anthropic_key"),
    "hf_token": ("api", "hf_token"),
    "whisper_model": ("defaults", "whisper_model"),
    "summary_style": ("defaults", "summary_style"),
    "language": ("defaults", "language"),
    "transcribe_backend": ("defaults", "transcribe_backend"),
    "faster_whisper_path": ("defaults", "faster_whisper_path"),
    "device": ("defaults", "device"),
    "max_speakers": ("defaults", "max_speakers"),
    "analysis_enabled": ("analysis", "enabled"),
    "custom_prompts": ("analysis", "custom_prompts"),
    "web_port": ("web", "port"),
    "label": ("web", "label"),
    "accent": ("web", "accent"),
    "vault_export": ("export", "vault"),
    "vault_path": ("export", "vault_path"),
}

_SECTION_KEY_TO_PROFILE = {pair: name for name, pair in PROFILE_KEY_TARGETS.items()}

# Marks a config that already had a profile folded into it. Writing one back
# would drop every other profile, so `save_config` refuses.
EFFECTIVE_MARKER = "_active_profile"


class ProfileError(ValueError):
    """The requested profile does not exist, or which one to use is ambiguous."""


def load_config() -> dict:
    """Read config from ~/.pocket-libre/config.toml. Returns empty sections if missing."""
    if not CONFIG_FILE.exists():
        return {}

    text = CONFIG_FILE.read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(text)
    else:
        import tomli
        return tomli.loads(text)


def _escape_toml(value: str) -> str:
    """Escape a string for a TOML basic string.

    Control characters must be escaped, not emitted raw: a value containing
    a newline previously produced a config file that failed to parse on the
    next load, silently wiping every setting.
    """
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _format_value(val) -> str:
    """Render one scalar as TOML. Bool before int: bool is an int subclass."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    return f'"{_escape_toml(str(val))}"'


def _restrict_windows_acl(path: Path) -> bool:
    """Rewrite the file's ACL so only the current user can read it."""
    user = os.environ.get("USERNAME") or getpass.getuser()
    if not user:
        return False
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def restrict_permissions(path: Path) -> bool:
    """Make `path` readable only by its owner. Returns True when enforced.

    `chmod` is a no-op on Windows: the file keeps whatever ACL it inherited, so
    a config holding the session key (which is also the device's WiFi password)
    and the Anthropic key stayed readable by every account on the machine. On
    Windows the ACL has to be rewritten instead, which is what `icacls` does.
    """
    try:
        path.chmod(0o600)
    except OSError:
        pass

    if os.name == "nt":
        return _restrict_windows_acl(path)

    try:
        return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    except OSError:
        return False


def save_config(config: dict):
    """Write config dict to ~/.pocket-libre/config.toml.

    Handles one level of nested tables so `[profiles.<name>]` round-trips.
    """
    if EFFECTIVE_MARKER in config:
        raise ValueError(
            "Refusing to save a config with a profile already folded into it: "
            "that would overwrite the global settings and drop every other "
            "profile. Save the config as loaded instead."
        )

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines = []

    # Top-level scalars have to precede every table header: after one, TOML
    # reads a bare key as belonging to that table, not to the document.
    for key, val in config.items():
        if not isinstance(val, dict):
            lines.append(f"{key} = {_format_value(val)}")
    if lines:
        lines.append("")

    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        nested = {k: v for k, v in values.items() if isinstance(v, dict)}
        scalars = {k: v for k, v in values.items() if not isinstance(v, dict)}

        if scalars or not nested:
            lines.append(f"[{section}]")
            for key, val in scalars.items():
                lines.append(f"{key} = {_format_value(val)}")
            lines.append("")

        for name, subvalues in nested.items():
            lines.append(f"[{section}.{name}]")
            for key, val in subvalues.items():
                if isinstance(val, dict):
                    continue  # one level of nesting is all the schema needs
                lines.append(f"{key} = {_format_value(val)}")
            lines.append("")

    CONFIG_FILE.write_text("\n".join(lines), encoding="utf-8")
    # Config holds API keys and the device session key — keep it owner-only.
    restrict_permissions(CONFIG_FILE)


# ── Profiles ────────────────────────────────────


def list_profiles(config: dict) -> dict:
    """Every valid `[profiles.<name>]` table, keyed by name."""
    raw = config.get(PROFILES_SECTION) or {}
    if not isinstance(raw, dict):
        return {}
    return {
        name: dict(values)
        for name, values in raw.items()
        if isinstance(values, dict) and VALID_PROFILE_NAME.match(str(name))
    }


def resolve_profile_name(config: dict, cli_value: str | None = None) -> str | None:
    """Which profile is active: CLI flag > env var > `default_profile` > the only one.

    Returns None when the config declares no profiles, so a single-device config
    keeps working untouched. Raises `ProfileError` rather than guessing when
    several profiles exist and none was chosen: picking the wrong one would mean
    touching someone else's recordings.
    """
    profiles = list_profiles(config)

    if not profiles:
        if cli_value:
            raise ProfileError(
                f"No profile named {cli_value!r}: this config has no profiles. "
                "Run 'pocket-libre setup --profile <name>' to create one."
            )
        return None

    for candidate in (cli_value,
                      os.environ.get(PROFILE_ENV_VAR),
                      config.get(DEFAULT_PROFILE_KEY)):
        if not candidate:
            continue
        name = str(candidate).strip().lower()
        if name not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise ProfileError(f"No profile named {name!r}. Known profiles: {known}")
        return name

    if len(profiles) == 1:
        return next(iter(profiles))

    known = ", ".join(sorted(profiles))
    raise ProfileError(
        f"Several profiles configured ({known}) and no default. "
        "Pass --profile <name>, set POCKET_LIBRE_PROFILE, "
        "or add default_profile to the config."
    )


def profile_config(config: dict, profile: str | dict | None) -> dict:
    """The active profile's table. Accepts a name or an already-resolved table."""
    if isinstance(profile, dict):
        return profile
    if not profile:
        return {}
    return list_profiles(config).get(str(profile).strip().lower(), {})


def profile_key_for(section: str, key: str) -> str:
    """The name a global `(section, key)` setting takes inside a profile table."""
    return _SECTION_KEY_TO_PROFILE.get((section, key), key)


_profile_key = profile_key_for


def active_profile(config: dict) -> str | None:
    """The profile already folded into this config, if any."""
    return config.get(EFFECTIVE_MARKER)


def effective_config(config: dict, profile: str | None) -> dict:
    """Flatten the active profile down into the global sections.

    Resolving the profile once, at the entry point, is what keeps two people's
    recordings apart: everything downstream receives a config describing exactly
    one device and one library, so there is no code path left that could read
    the other profile's by mistake.

    The result is for reading only. `save_config` rejects it.
    """
    if not profile:
        return config

    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in config.items()}
    merged.pop(PROFILES_SECTION, None)
    merged.pop(DEFAULT_PROFILE_KEY, None)

    for key, value in profile_config(config, profile).items():
        if value in (None, ""):
            continue
        target = PROFILE_KEY_TARGETS.get(key)
        if target is None:
            continue
        section, section_key = target
        merged.setdefault(section, {})[section_key] = value

    # These four are resolved rather than copied: each has a per-profile default
    # that the raw table may not spell out, and the library in particular must
    # never silently fall back to a root shared with another profile.
    merged.setdefault("output", {})["directory"] = get_output_dir(config, profile=profile)
    web = merged.setdefault("web", {})
    web["port"] = resolve_web_port(config, profile=profile)
    web["label"] = profile_label(config, profile)
    web["accent"] = profile_accent(config, profile)

    merged[EFFECTIVE_MARKER] = profile
    return merged


def profile_label(config: dict, profile: str | None) -> str:
    """Human name for the active profile, for the web header and window title."""
    if not profile:
        return "Pocket Libre"
    label = profile_config(config, profile).get("label")
    if label:
        return str(label)
    return str(profile).replace("-", " ").replace("_", " ").title()


def profile_accent(config: dict, profile: str | None) -> str:
    """Accent color for the active profile.

    Two identical UIs on adjacent ports is a mix-up waiting to happen, so every
    profile gets a visibly different one even when the config does not say.
    """
    accent = profile_config(config, profile).get("accent")
    if accent:
        return str(accent)
    names = sorted(list_profiles(config))
    if profile in names:
        return ACCENT_PALETTE[names.index(profile) % len(ACCENT_PALETTE)]
    return ACCENT_PALETTE[0]


def profile_warnings(config: dict) -> list[str]:
    """Configuration mistakes worth printing before they cost someone privacy."""
    warnings = []
    raw = config.get(PROFILES_SECTION) or {}
    profiles = list_profiles(config)

    if isinstance(raw, dict):
        for name in raw:
            if str(name) not in profiles:
                warnings.append(
                    f"Profile {name!r} is ignored: names must be lowercase letters, "
                    "digits, '-' or '_', starting with a letter or digit."
                )

    default = config.get(DEFAULT_PROFILE_KEY)
    if default and str(default).strip().lower() not in profiles:
        warnings.append(f"default_profile = {default!r} does not match any profile.")

    if len(profiles) > 1 and not default:
        warnings.append(
            "No default_profile set, so every command needs --profile. "
            "That is deliberate when the profiles belong to different people."
        )

    seen_dirs: dict[str, str] = {}
    seen_ports: dict[int, str] = {}
    seen_addrs: dict[str, str] = {}
    for name in sorted(profiles):
        out = get_output_dir(config, profile=name)
        if out in seen_dirs:
            warnings.append(
                f"Profiles {seen_dirs[out]!r} and {name!r} share the output directory "
                f"{out}: their recordings would land in the same library."
            )
        seen_dirs[out] = name

        port = resolve_web_port(config, profile=name)
        if port in seen_ports:
            warnings.append(
                f"Profiles {seen_ports[port]!r} and {name!r} both use web port {port}: "
                "only one of them can run at a time."
            )
        seen_ports[port] = name

        addr = str(profiles[name].get("address") or "").strip().upper()
        if addr and addr in seen_addrs:
            warnings.append(
                f"Profiles {seen_addrs[addr]!r} and {name!r} point at the same device "
                f"address {addr}."
            )
        if addr:
            seen_addrs[addr] = name

    return warnings


# ── Resolution ──────────────────────────────────


def get(config: dict, section: str, key: str,
        cli_value=None, env_var: str | None = None, default=None,
        profile: str | dict | None = None):
    """Resolve a config value. CLI flag > env var > profile > config file > default."""
    if cli_value not in (None, ""):
        return cli_value
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val

    table = profile_config(config, profile)
    if table:
        profile_val = table.get(_profile_key(section, key))
        if profile_val not in (None, ""):
            return profile_val

    section_values = config.get(section)
    config_val = section_values.get(key) if isinstance(section_values, dict) else None
    if config_val not in (None, ""):
        return config_val
    if default is not None:
        return default
    return DEFAULTS.get(section, {}).get(key)


def get_output_dir(config: dict, cli_value: str | None = None,
                   profile: str | None = None) -> str:
    """Resolve output directory from config chain.

    A profile that does not name its own directory gets a subdirectory under the
    global one. Two recorders sharing a library by default would quietly mix two
    people's recordings together.
    """
    table = profile_config(config, profile)
    raw = get(config, "output", "directory", cli_value=cli_value,
              profile=table, default="~/Pocket Libre")
    path = Path(os.path.expanduser(str(raw)))
    if profile and not cli_value and not table.get("output_directory"):
        path = path / str(profile)
    return str(path)


def resolve_address(config: dict, cli_value: str | None = None,
                    profile: str | None = None) -> str | None:
    """Resolve device address from config chain."""
    return get(config, "device", "address", cli_value=cli_value, profile=profile)


def resolve_session_key(config: dict, cli_value: str | None = None,
                        profile: str | None = None) -> str:
    """Resolve session key from config chain. Returns "" when unset.

    A profile with no key of its own falls back to the global one: the key is
    issued per vendor account, not per device, so two recorders on one account
    legitimately share it.
    """
    return get(config, "device", "session_key", cli_value=cli_value, profile=profile)


def resolve_anthropic_key(config: dict, cli_value: str | None = None,
                          profile: str | None = None) -> str | None:
    """Resolve Anthropic API key from config chain."""
    return get(config, "api", "anthropic_key", cli_value=cli_value,
               env_var="ANTHROPIC_API_KEY", profile=profile)


def resolve_hf_token(config: dict, cli_value: str | None = None,
                     profile: str | None = None) -> str | None:
    """Resolve HuggingFace token from config chain."""
    return get(config, "api", "hf_token", cli_value=cli_value,
               env_var="HUGGINGFACE_TOKEN", profile=profile)


def resolve_web_port(config: dict, cli_value: int | None = None,
                     profile: str | None = None) -> int:
    """Resolve the web UI port.

    An explicit port wins. Otherwise profiles are handed consecutive ports from
    `BASE_WEB_PORT` in name order, so `web --all` can raise one server per
    profile without them colliding.
    """
    explicit = get(config, "web", "port", cli_value=cli_value, profile=profile)
    if explicit not in (None, ""):
        return int(explicit)
    names = sorted(list_profiles(config))
    if profile and profile in names:
        return BASE_WEB_PORT + names.index(profile)
    return BASE_WEB_PORT
