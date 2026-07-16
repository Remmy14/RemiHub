import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config import (
    load_application_config,
    resolve_application_config_path,
    resolve_database_config_path,
    resolve_environment_file_path,
)


class ApplicationConfigPathTests(unittest.TestCase):
    def test_uses_default_path_when_environment_is_missing(self):
        default_path = Path("/application/config/config.ini")

        with patch.dict(os.environ, {}, clear=True):
            resolved = resolve_application_config_path(default_path)

        self.assertEqual(resolved, default_path)

    def test_uses_external_application_config(self):
        with patch.dict(
            os.environ,
            {"REMIHUB_CONFIG_FILE": " /secure/application.ini "},
            clear=True,
        ):
            resolved = resolve_application_config_path()

        self.assertEqual(resolved, Path("/secure/application.ini"))

    def test_loads_external_application_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "application.ini"
            config_path.write_text(
                "[Weather]\napi_key = test-key\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"REMIHUB_CONFIG_FILE": str(config_path)},
                clear=True,
            ):
                loaded = load_application_config()

        self.assertEqual(loaded["Weather"]["api_key"], "test-key")


class EnvironmentFilePathTests(unittest.TestCase):
    def test_uses_external_environment_file(self):
        with patch.dict(
            os.environ,
            {"REMIHUB_ENV_FILE": " /secure/remihub.env "},
            clear=True,
        ):
            resolved = resolve_environment_file_path()

        self.assertEqual(resolved, Path("/secure/remihub.env"))

    def test_blank_environment_value_uses_default_path(self):
        default_path = Path("/application/config/remihub.env")

        with patch.dict(
            os.environ,
            {"REMIHUB_ENV_FILE": "   "},
            clear=True,
        ):
            resolved = resolve_environment_file_path(default_path)

        self.assertEqual(resolved, default_path)


class DatabaseConfigIsolationTests(unittest.TestCase):
    def test_application_config_does_not_replace_database_config(self):
        default_path = Path("/application/config/database.ini")

        with patch.dict(
            os.environ,
            {"REMIHUB_CONFIG_FILE": "/secure/application.ini"},
            clear=True,
        ):
            resolved = resolve_database_config_path(default_path)

        self.assertEqual(resolved, default_path)


if __name__ == "__main__":
    unittest.main()
