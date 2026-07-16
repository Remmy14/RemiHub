import configparser
import json
import os
from pathlib import Path


DATABASE_CONFIG_ENV = "REMIHUB_DATABASE_CONFIG"


def resolve_database_config_path(default_path: str | os.PathLike[str]) -> Path:
    """Return the external database config path or the application default."""
    configured = os.environ.get(DATABASE_CONFIG_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(default_path)


def load_config(config_path: str) -> dict:
    """
    Load a configuration file and return the contents as a dictionary.
    Supports .ini and .json formats.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    ext = os.path.splitext(config_path)[1].lower()

    if ext == ".ini":
        return _load_ini(config_path)
    if ext == ".json":
        return _load_json(config_path)
    raise ValueError(f"Unsupported config file type: {ext}")


def _load_ini(path: str) -> dict:
    config = configparser.ConfigParser()
    config.read(path)
    return {section: dict(config[section]) for section in config.sections()}


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
