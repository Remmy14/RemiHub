import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core.runtime_paths import (
    ensure_log_directory,
    resolve_log_directory,
)


class RuntimeLogDirectoryTests(unittest.TestCase):
    def test_uses_configured_log_directory(self):
        with patch.dict(
            os.environ,
            {"REMIHUB_LOG_DIR": " /secure/runtime-logs "},
            clear=True,
        ):
            resolved = resolve_log_directory()

        self.assertEqual(resolved, Path("/secure/runtime-logs"))

    def test_blank_configuration_uses_default_directory(self):
        default_path = Path("/application/backend/logs")

        with patch.dict(
            os.environ,
            {"REMIHUB_LOG_DIR": "   "},
            clear=True,
        ):
            resolved = resolve_log_directory(default_path)

        self.assertEqual(resolved, default_path)

    def test_ensure_log_directory_creates_missing_parents(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_directory = (
                Path(temporary_directory)
                / "missing-parent"
                / "logs"
            )

            with patch.dict(os.environ, {}, clear=True):
                resolved = ensure_log_directory(log_directory)

            self.assertEqual(resolved, log_directory)
            self.assertTrue(log_directory.is_dir())


if __name__ == "__main__":
    unittest.main()
