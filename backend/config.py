import configparser
import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APPLICATION_CONFIG = PROJECT_ROOT / "config" / "config.ini"
DEFAULT_ENVIRONMENT_FILE = PROJECT_ROOT / "config" / "remihub.env"

APPLICATION_CONFIG_ENV = "REMIHUB_CONFIG_FILE"
DATABASE_CONFIG_ENV = "REMIHUB_DATABASE_CONFIG"
ENVIRONMENT_FILE_ENV = "REMIHUB_ENV_FILE"


def _resolve_path(
    environment_variable: str,
    default_path: str | os.PathLike[str],
) -> Path:
    configured = os.environ.get(environment_variable, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(default_path)


def resolve_application_config_path(
    default_path: str | os.PathLike[str] = DEFAULT_APPLICATION_CONFIG,
) -> Path:
    """Return the external application config path or the project default."""
    return _resolve_path(APPLICATION_CONFIG_ENV, default_path)


def resolve_database_config_path(default_path: str | os.PathLike[str]) -> Path:
    """Return the external database config path or the application default."""
    return _resolve_path(DATABASE_CONFIG_ENV, default_path)


def resolve_environment_file_path(
    default_path: str | os.PathLike[str] = DEFAULT_ENVIRONMENT_FILE,
) -> Path:
    """Return the external dotenv path or the project default."""
    return _resolve_path(ENVIRONMENT_FILE_ENV, default_path)


def load_config(config_path: str | os.PathLike[str]) -> dict:
    """
    Load a configuration file and return the contents as a dictionary.
    Supports .ini and .json formats.
    """
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    ext = path.suffix.lower()

    if ext == ".ini":
        return _load_ini(path)
    if ext == ".json":
        return _load_json(path)
    raise ValueError(f"Unsupported config file type: {ext}")


def load_application_config() -> dict:
    """Load the non-database application configuration."""
    return load_config(resolve_application_config_path())


def _load_ini(path: str | os.PathLike[str]) -> dict:
    config = configparser.ConfigParser()
    config.read(path)
    return {section: dict(config[section]) for section in config.sections()}


def _load_json(path: str | os.PathLike[str]) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
