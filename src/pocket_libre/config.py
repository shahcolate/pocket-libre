"""Configuration management for Pocket Libre.

Loads/saves config from ~/.pocket-libre/config.toml.
Resolution chain: CLI flag > env var > config file > default.
"""

import getpass
import os
import stat
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".pocket-libre"
CONFIG_FILE = CONFIG_DIR / "config.toml"

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
    },
    "analysis": {
        "enabled": "summary,entities",
        "custom_prompts": "",
    },
}


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
    """Write config dict to ~/.pocket-libre/config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines = []
    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, val in values.items():
            if isinstance(val, bool):
                lines.append(f'{key} = {"true" if val else "false"}')
            elif isinstance(val, int):
                lines.append(f"{key} = {val}")
            else:
                lines.append(f'{key} = "{_escape_toml(str(val))}"')
        lines.append("")

    CONFIG_FILE.write_text("\n".join(lines), encoding="utf-8")
    # Config holds API keys and the device session key — keep it owner-only.
    restrict_permissions(CONFIG_FILE)


def get(config: dict, section: str, key: str,
        cli_value=None, env_var: str | None = None, default=None):
    """Resolve a config value. CLI flag > env var > config file > default."""
    if cli_value not in (None, ""):
        return cli_value
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val
    config_val = config.get(section, {}).get(key)
    if config_val not in (None, ""):
        return config_val
    if default is not None:
        return default
    return DEFAULTS.get(section, {}).get(key)


def get_output_dir(config: dict, cli_value: str | None = None) -> str:
    """Resolve output directory from config chain."""
    raw = get(config, "output", "directory", cli_value=cli_value,
              default="~/Pocket Libre")
    return os.path.expanduser(raw)


def resolve_address(config: dict, cli_value: str | None = None) -> str | None:
    """Resolve device address from config chain."""
    return get(config, "device", "address", cli_value=cli_value)


def resolve_session_key(config: dict, cli_value: str | None = None) -> str:
    """Resolve session key from config chain. Returns "" when unset."""
    return get(config, "device", "session_key", cli_value=cli_value)


def resolve_anthropic_key(config: dict, cli_value: str | None = None) -> str | None:
    """Resolve Anthropic API key from config chain."""
    return get(config, "api", "anthropic_key", cli_value=cli_value,
               env_var="ANTHROPIC_API_KEY")


def resolve_hf_token(config: dict, cli_value: str | None = None) -> str | None:
    """Resolve HuggingFace token from config chain."""
    return get(config, "api", "hf_token", cli_value=cli_value,
               env_var="HUGGINGFACE_TOKEN")
