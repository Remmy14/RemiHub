import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.tasks import plex_dl_monitor as monitor


class PlexMonitorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.staging = self.root / "Temp" / "Movies"
        self.library = self.root / "Library"
        self.staging.mkdir(parents=True)
        (self.library / "Movies").mkdir(parents=True)
        self.states = []
        self.record_patch = patch.object(monitor.plex_service, "record_file_state",
                                         side_effect=self.record)
        self.record_patch.start()

    def tearDown(self):
        self.record_patch.stop()
        self.temp.cleanup()

    def record(self, source, state, **kwargs):
        self.states.append((source, state, kwargs))
        return {"completed": False}

    def test_nested_media_moves_and_verifies_each_destination(self):
        nested = self.staging / "Package" / "More"
        nested.mkdir(parents=True)
        for name in ("FIRST.2024.mkv", "SECOND.2025.mkv"):
            (nested / name).write_bytes(b"media")
        self.assertTrue(monitor.get_new_downloads(str(self.staging), str(self.library), "Movies"))
        self.assertEqual((self.library / "Movies" / "First (2024).mkv").read_bytes(), b"media")
        self.assertEqual((self.library / "Movies" / "Second (2025).mkv").read_bytes(), b"media")
        completed = [entry for entry in self.states if entry[1] == "completed"]
        self.assertEqual(len(completed), 2)
        self.assertTrue(all(Path(entry[2]["destination"]).is_file() for entry in completed))

    def test_move_failure_is_recorded_and_source_remains_for_retry(self):
        source = self.staging / "FILM.2024.mkv"
        source.write_bytes(b"media")
        with patch.object(monitor.shutil, "move", side_effect=OSError("storage unavailable")):
            self.assertFalse(monitor.get_new_downloads(str(self.staging), str(self.library), "Movies"))
        self.assertTrue(source.is_file())
        self.assertIn("failed", [state for _, state, _ in self.states])
        self.assertNotIn("completed", [state for _, state, _ in self.states])

    def test_existing_destination_is_not_overwritten(self):
        source = self.staging / "FILM.2024.mkv"
        source.write_bytes(b"new")
        destination = self.library / "Movies" / "Film (2024).mkv"
        destination.write_bytes(b"existing")
        self.assertFalse(monitor.get_new_downloads(str(self.staging), str(self.library), "Movies"))
        self.assertEqual(destination.read_bytes(), b"existing")
        self.assertTrue(source.exists())
        self.assertNotIn("completed", [state for _, state, _ in self.states])

    def test_destination_verification_failure_does_not_complete(self):
        source = self.staging / "FILM.2024.mkv"
        source.write_bytes(b"media")

        def broken_move(src, dst):
            Path(src).unlink()

        with patch.object(monitor.shutil, "move", side_effect=broken_move):
            self.assertFalse(monitor.get_new_downloads(str(self.staging), str(self.library), "Movies"))
        self.assertIn("failed", [state for _, state, _ in self.states])
        self.assertNotIn("completed", [state for _, state, _ in self.states])

    def test_tracking_failure_does_not_move_or_quarantine(self):
        source = self.staging / "FILM.2024.mkv"
        source.write_bytes(b"media")
        with patch.object(monitor.plex_service, "record_file_state", side_effect=RuntimeError("db down")):
            with patch.object(monitor, "quarantine_item") as quarantine:
                self.assertFalse(monitor.get_new_downloads(str(self.staging), str(self.library), "Movies"))
        self.assertTrue(source.exists())
        quarantine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
