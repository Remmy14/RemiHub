from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIRECTORY = PROJECT_ROOT / "backend" / "logs"
LOG_DIRECTORY_ENV = "REMIHUB_LOG_DIR"


def resolve_log_directory(
    default_path: str | os.PathLike[str] = DEFAULT_LOG_DIRECTORY,
) -> Path:
    """Return the configured runtime log directory or the project default."""
    configured = os.environ.get(LOG_DIRECTORY_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(default_path)


def ensure_log_directory(
    default_path: str | os.PathLike[str] = DEFAULT_LOG_DIRECTORY,
) -> Path:
    """Create and return the runtime log directory."""
    directory = resolve_log_directory(default_path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
