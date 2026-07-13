"""Repository-local configuration for Rewind."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


CONFIG_FILE = ".rewind.toml"
DEFAULT_CONFIG = """# Rewind configuration. This file is local to this repository.\n\n[watch]\nburst_window_seconds = 3\nignore = []\n\n[guard]\n# command = \"pytest\"\ntimeout_seconds = 120\n\n[retention]\nmax_count = 200\nmax_age_days = 7\n"""


@dataclass(frozen=True)
class RewindConfig:
    burst_window_seconds: float = 3.0
    ignore: set[str] = field(default_factory=set)
    guard_command: str = ""
    guard_timeout_seconds: float = 120.0
    max_count: int = 200
    max_age_days: float = 7.0


def load_config(repo_path: Path) -> RewindConfig:
    path = repo_path / CONFIG_FILE
    if not path.exists():
        return RewindConfig()
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Invalid {CONFIG_FILE}: {exc}") from exc

    watch = data.get("watch", {})
    guard = data.get("guard", {})
    retention = data.get("retention", {})
    config = RewindConfig(
        burst_window_seconds=float(watch.get("burst_window_seconds", 3.0)),
        ignore={str(p) for p in watch.get("ignore", [])},
        guard_command=str(guard.get("command", "")),
        guard_timeout_seconds=float(guard.get("timeout_seconds", 120.0)),
        max_count=int(retention.get("max_count", 200)),
        max_age_days=float(retention.get("max_age_days", 7.0)),
    )
    if config.burst_window_seconds <= 0 or config.guard_timeout_seconds <= 0:
        raise ValueError("watch burst window and guard timeout must be positive")
    if config.max_count < 1 or config.max_age_days < 0:
        raise ValueError("retention max_count must be positive and max_age_days cannot be negative")
    return config
