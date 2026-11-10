"""Local settings and the API key, kept outside the project folder.

Where they live:

- macOS and Linux: $XDG_CONFIG_HOME/authz-pilot, or ~/.config/authz-pilot
- Windows: %APPDATA%\\authz-pilot
- AUTHZ_PILOT_HOME overrides both.

Each provider has its own API key (Anthropic for Claude, OpenAI, Google for
Gemini), found in this order:

1. the environment variable: ANTHROPIC_API_KEY, OPENAI_API_KEY, or
   GEMINI_API_KEY (GOOGLE_API_KEY also works);
2. the key saved with `python -m authz_pilot key set` or on the app's Settings
   page, in a file that only your user can read;
3. on macOS, the Keychain item with the service name "anthropic-api-key",
   "openai-api-key" or "gemini-api-key" (AUTHZ_PILOT_KEYCHAIN=0 turns this off).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import DEFAULT_AGENT_MODEL, DEFAULT_MONITOR_MODEL, EFFORTS, MODEL_INFO, MODELS


@dataclass(frozen=True)
class Provider:
    name: str  # as people know it
    env: tuple[str, ...]  # environment variables, first match wins
    keychain: str  # macOS Keychain service name
    file: str  # file name of the saved key
    console: str  # where people get a key


PROVIDERS = {
    "anthropic": Provider("Anthropic", ("ANTHROPIC_API_KEY",), "anthropic-api-key", "api-key", "the Claude Console"),
    "openai": Provider("OpenAI", ("OPENAI_API_KEY",), "openai-api-key", "openai-api-key", "the OpenAI API dashboard"),
    "google": Provider(
        "Google", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini-api-key", "gemini-api-key", "Google AI Studio"
    ),
}
KEYCHAIN_SERVICE = PROVIDERS["anthropic"].keychain
LIMITS = {"max_cost": (0.01, 1000.0), "max_steps": (1, 20), "workers": (1, 16)}


def home() -> Path:
    override = os.environ.get("AUTHZ_PILOT_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "authz-pilot"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        return (Path(base) if base else Path.home() / ".config") / "authz-pilot"


def key_path(provider: str = "anthropic") -> Path:
    return home() / PROVIDERS[provider].file


def settings_path() -> Path:
    return home() / "settings.json"


# --------------------------------------------------------------------------- settings


@dataclass
class Settings:
    model: str = DEFAULT_AGENT_MODEL  # the assistant under test
    effort: str = "medium"
    monitor_model: str = DEFAULT_MONITOR_MODEL  # condition C
    monitor_effort: str = "medium"
    max_cost: float = 20.0  # USD; no new episode starts after the estimate passes it
    max_steps: int = 8  # model calls per message
    workers: int = 4  # episodes in parallel

    def to_dict(self) -> dict:
        return asdict(self)


def parse_settings(raw: dict, base: Settings | None = None) -> tuple[Settings, dict[str, str]]:
    """Apply the valid fields of `raw` on top of `base`. Returns (settings, problems by field)."""
    values = asdict(base or Settings())
    problems: dict[str, str] = {}
    for name, value in raw.items():
        if name not in values:
            continue
        if name in ("model", "monitor_model"):
            if value not in MODELS:
                problems[name] = "Pick one of: " + ", ".join(MODELS) + "."
                continue
        elif name in ("effort", "monitor_effort"):
            if value not in EFFORTS:
                problems[name] = "Pick one of: " + ", ".join(EFFORTS) + "."
                continue
        else:
            low, high = LIMITS[name]
            kind = float if name == "max_cost" else int
            try:
                number = kind(value)
                exact = float(value) == number
            except (TypeError, ValueError):
                problems[name] = "Enter a number."
                continue
            if isinstance(value, bool) or not exact or not (low <= number <= high):
                problems[name] = f"Enter a {'number' if kind is float else 'whole number'} from {low:g} to {high:g}."
                continue
            value = number
        values[name] = value
    # Each model takes only some effort levels.
    for model_field, effort_field in (("model", "effort"), ("monitor_model", "monitor_effort")):
        info = MODEL_INFO.get(values[model_field])
        if info and values[effort_field] not in info.effort_levels and effort_field not in problems:
            levels = list(info.effort_levels)
            problems[effort_field] = f"{info.name} takes effort {', '.join(levels[:-1])} or {levels[-1]}."
    return Settings(**values), problems


def load_settings() -> Settings:
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    settings, _ = parse_settings(raw if isinstance(raw, dict) else {})
    return settings


def save_settings(settings: Settings) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- API key


def key_problem(key: str, provider: str = "anthropic") -> str | None:
    if not key:
        return "Paste a key first."
    if not (20 <= len(key) <= 400) or not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
        return f"That does not look like an API key. Copy it again from {PROVIDERS[provider].console}."
    return None


def save_key(key: str, provider: str = "anthropic") -> Path:
    """Write the key to a file only this user can read (0600 from the start)."""
    key = key.strip()
    problem = key_problem(key, provider)
    if problem:
        raise ValueError(problem)
    path = key_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # also when an older file had wider permissions
        os.write(fd, (key + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return path


def saved_key(provider: str = "anthropic") -> str | None:
    try:
        key = key_path(provider).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key or None


def remove_key(provider: str = "anthropic") -> bool:
    try:
        key_path(provider).unlink()
    except FileNotFoundError:
        return False
    return True


def keychain_key(provider: str = "anthropic") -> str | None:
    # A name, not an inline test: mypy would treat the rest as unreachable when it checks for another platform.
    on_macos = sys.platform == "darwin"
    if not on_macos or os.environ.get("AUTHZ_PILOT_KEYCHAIN", "1") == "0":
        return None
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", PROVIDERS[provider].keychain, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    key = out.stdout.strip()
    return key if out.returncode == 0 and not key_problem(key, provider) else None


def env_key(provider: str = "anthropic") -> tuple[str | None, str | None]:
    for name in PROVIDERS[provider].env:
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return None, None


def resolve_key(provider: str = "anthropic") -> tuple[str | None, str]:
    """(key, source). Source is "environment", "saved", "keychain" or "none"."""
    env, _ = env_key(provider)
    if env:
        return env, "environment"
    for source, find in (("saved", saved_key), ("keychain", keychain_key)):
        key = find(provider)
        if key:
            return key, source
    return None, "none"


def describe_source(source: str, provider: str = "anthropic") -> str:
    p = PROVIDERS[provider]
    _, env_name = env_key(provider)
    return {
        "environment": f"the {env_name or p.env[0]} environment variable",
        "saved": f"the saved key in {key_path(provider)}",
        "keychain": f'the macOS Keychain (service "{p.keychain}")',
        "none": "none found",
    }[source]


def mask(key: str | None) -> str:
    """Enough to recognise a key, never enough to use it."""
    if not key:
        return ""
    prefix = next((p for p in ("sk-ant-", "sk-proj-", "sk-", "AIza") if key.startswith(p)), "")
    return prefix + "…" + key[-4:]
