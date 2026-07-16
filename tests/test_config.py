import os
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config import resolve_database_config_path


class DatabaseConfigPathTests(unittest.TestCase):
    def test_uses_default_path_when_environment_is_missing(self):
        default_path = Path("/application/config/config.ini")

        with patch.dict(os.environ, {}, clear=True):
            resolved = resolve_database_config_path(default_path)

        self.assertEqual(resolved, default_path)

    def test_uses_external_database_config(self):
        with patch.dict(
            os.environ,
            {"REMIHUB_DATABASE_CONFIG": " /secure/prod-app.ini "},
            clear=True,
        ):
            resolved = resolve_database_config_path("config/config.ini")

        self.assertEqual(resolved, Path("/secure/prod-app.ini"))

    def test_blank_environment_value_uses_default_path(self):
        default_path = Path("config/config.ini")

        with patch.dict(
            os.environ,
            {"REMIHUB_DATABASE_CONFIG": "   "},
            clear=True,
        ):
            resolved = resolve_database_config_path(default_path)

        self.assertEqual(resolved, default_path)


if __name__ == "__main__":
    unittest.main()
