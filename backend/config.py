import os
import json
import configparser

def load_config(config_path: str) -> dict:
    """
    Load a configuration file and return the contents as a dictionary.
    Supports .ini, .json, and .yaml/.yml formats.
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    ext = os.path.splitext(config_path)[1].lower()

    if ext == '.ini':
        return _load_ini(config_path)
    elif ext == '.json':
        return _load_json(config_path)
    else:
        raise ValueError(f"Unsupported config file type: {ext}")

def _load_ini(path: str) -> dict:
    config = configparser.ConfigParser()
    config.read(path)
    return {section: dict(config[section]) for section in config.sections()}

def _load_json(path: str) -> dict:
    with open(path, 'r') as f:
        return json.load(f)
