import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from backend.database.migration_runner import (
    MIGRATIONS_DIR,
    _acquire_lock,
    _validate_applied_checksums,
    discover_migrations,
    sha256_file,
)


class MigrationDiscoveryTests(unittest.TestCase):
    def test_repository_migration_is_discoverable(self):
        migrations = discover_migrations(MIGRATIONS_DIR)

        self.assertEqual(
            [(migration.version, migration.name) for migration in migrations],
            [("0001", "auth_foundation")],
        )

    def test_discovers_up_and_down_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            migrations_dir = Path(temp_dir)
            up = migrations_dir / "0001_auth_foundation.up.sql"
            down = migrations_dir / "0001_auth_foundation.down.sql"
            up.write_text("SELECT 1;\n", encoding="utf-8")
            down.write_text("SELECT 2;\n", encoding="utf-8")

            migrations = discover_migrations(migrations_dir)

            self.assertEqual(len(migrations), 1)
            self.assertEqual(migrations[0].version, "0001")
            self.assertEqual(migrations[0].name, "auth_foundation")
            self.assertEqual(migrations[0].up_path, up)
            self.assertEqual(migrations[0].down_path, down)
            self.assertEqual(
                migrations[0].checksum,
                hashlib.sha256(b"SELECT 1;\n").hexdigest(),
            )

    def test_rejects_duplicate_versions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            migrations_dir = Path(temp_dir)
            for name in ("auth", "other"):
                (migrations_dir / f"0001_{name}.up.sql").write_text(
                    "SELECT 1;\n",
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(RuntimeError, "Duplicate migration version"):
                discover_migrations(migrations_dir)

    def test_rejects_down_only_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            migrations_dir = Path(temp_dir)
            (migrations_dir / "0001_auth.down.sql").write_text(
                "SELECT 1;\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "missing its up SQL"):
                discover_migrations(migrations_dir)

    def test_sha256_file_streams_file_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "migration.sql"
            content = b"SELECT gen_random_uuid();\n"
            path.write_bytes(content)

            self.assertEqual(sha256_file(path), hashlib.sha256(content).hexdigest())

    def test_rejects_changed_applied_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            migrations_dir = Path(temp_dir)
            (migrations_dir / "0001_auth.up.sql").write_text(
                "SELECT 1;\n",
                encoding="utf-8",
            )
            migrations = discover_migrations(migrations_dir)

            with self.assertRaisesRegex(RuntimeError, "has been modified"):
                _validate_applied_checksums(
                    migrations,
                    {
                        "0001": {
                            "name": "auth",
                            "checksum": "wrong-checksum",
                            "applied_at": "2026-01-01T00:00:00+00:00",
                        }
                    },
                )

    def test_migration_lock_fails_fast_when_another_runner_holds_it(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (False,)

        with self.assertRaisesRegex(RuntimeError, "already running"):
            _acquire_lock(conn)


if __name__ == "__main__":
    unittest.main()
