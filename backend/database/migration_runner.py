from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from backend.config import load_config


logger = logging.getLogger("remihub.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_PATTERN = re.compile(
    r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.(?P<direction>up|down)\.sql$"
)
ADVISORY_LOCK_NAME = "remihub_schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_path: Path
    down_path: Path | None
    checksum: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    migration_files: dict[tuple[str, str], dict[str, Path]] = {}

    if not migrations_dir.is_dir():
        raise RuntimeError(f"Migrations directory does not exist: {migrations_dir}")

    for path in migrations_dir.iterdir():
        if not path.is_file():
            continue

        match = MIGRATION_PATTERN.fullmatch(path.name)
        if not match:
            continue

        key = (match.group("version"), match.group("name"))
        direction = match.group("direction")
        migration_files.setdefault(key, {})[direction] = path

    migrations: list[Migration] = []
    seen_versions: set[str] = set()

    for (version, name), paths in sorted(migration_files.items()):
        if version in seen_versions:
            raise RuntimeError(f"Duplicate migration version: {version}")
        seen_versions.add(version)

        up_path = paths.get("up")
        if up_path is None:
            raise RuntimeError(f"Migration {version}_{name} is missing its up SQL file")

        migrations.append(
            Migration(
                version=version,
                name=name,
                up_path=up_path,
                down_path=paths.get("down"),
                checksum=sha256_file(up_path),
            )
        )

    return migrations


def _connect(config_path: Path):
    import psycopg2

    database_url = os.environ.get("REMIHUB_DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)

    config = load_config(str(config_path))["Database"]
    return psycopg2.connect(
        user=config["user"],
        password=config["password"],
        host=config["host"],
        port=config["port"],
        database=config["database"],
    )


def _ensure_migration_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                version text PRIMARY KEY,
                name text NOT NULL,
                checksum text NOT NULL,
                execution_ms integer NOT NULL,
                applied_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
    conn.commit()


def _acquire_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0));",
            (ADVISORY_LOCK_NAME,),
        )
        acquired = cur.fetchone()

    if not acquired or acquired[0] is not True:
        raise RuntimeError("Another RemiHub migration process is already running")


def _release_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0));",
            (ADVISORY_LOCK_NAME,),
        )
        released = cur.fetchone()

    if not released or released[0] is not True:
        logger.warning("Migration advisory lock was not held at release time")


def _applied_migrations(conn) -> dict[str, dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT version, name, checksum, applied_at
            FROM public.schema_migrations
            ORDER BY version;
            """
        )
        rows = cur.fetchall()

    return {
        row[0]: {
            "name": row[1],
            "checksum": row[2],
            "applied_at": row[3].isoformat(),
        }
        for row in rows
    }


def _validate_applied_checksums(
    migrations: list[Migration],
    applied: dict[str, dict[str, str]],
) -> None:
    known = {migration.version: migration for migration in migrations}

    for version, record in applied.items():
        migration = known.get(version)
        if migration is None:
            raise RuntimeError(
                f"Applied migration {version}_{record['name']} is missing from the source tree"
            )
        if migration.name != record["name"]:
            raise RuntimeError(
                f"Applied migration {version} name changed from "
                f"{record['name']} to {migration.name}"
            )
        if migration.checksum != record["checksum"]:
            raise RuntimeError(
                f"Applied migration {version}_{migration.name} has been modified"
            )


def upgrade(conn, migrations: list[Migration]) -> int:
    applied = _applied_migrations(conn)
    _validate_applied_checksums(migrations, applied)
    applied_count = 0

    for migration in migrations:
        if migration.version in applied:
            continue

        logger.info("Applying migration %s_%s", migration.version, migration.name)
        started = time.monotonic()

        try:
            with conn.cursor() as cur:
                cur.execute(migration.up_path.read_text(encoding="utf-8"))
                elapsed_ms = round((time.monotonic() - started) * 1000)
                cur.execute(
                    """
                    INSERT INTO public.schema_migrations (
                        version,
                        name,
                        checksum,
                        execution_ms
                    )
                    VALUES (%s, %s, %s, %s);
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        elapsed_ms,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        applied_count += 1

    return applied_count


def downgrade(conn, migrations: list[Migration], steps: int) -> int:
    if steps < 1:
        raise ValueError("steps must be at least 1")

    applied = _applied_migrations(conn)
    _validate_applied_checksums(migrations, applied)
    by_version = {migration.version: migration for migration in migrations}
    targets = sorted(applied, reverse=True)[:steps]

    if len(targets) < steps:
        raise RuntimeError(
            f"Cannot roll back {steps} migration(s); only {len(targets)} are applied"
        )

    for version in targets:
        migration = by_version[version]
        if migration.down_path is None:
            raise RuntimeError(
                f"Migration {migration.version}_{migration.name} has no down SQL file"
            )

        logger.warning("Rolling back migration %s_%s", migration.version, migration.name)

        try:
            with conn.cursor() as cur:
                cur.execute(migration.down_path.read_text(encoding="utf-8"))
                cur.execute(
                    "DELETE FROM public.schema_migrations WHERE version = %s;",
                    (migration.version,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return len(targets)


def status(conn, migrations: list[Migration]) -> list[dict[str, str | bool | None]]:
    applied = _applied_migrations(conn)
    _validate_applied_checksums(migrations, applied)

    return [
        {
            "version": migration.version,
            "name": migration.name,
            "applied": migration.version in applied,
            "applied_at": applied.get(migration.version, {}).get("applied_at"),
        }
        for migration in migrations
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply RemiHub database migrations")
    parser.add_argument(
        "command",
        choices=("upgrade", "downgrade", "status"),
        help="Migration operation to perform",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=1,
        help="Number of migrations to roll back (downgrade only)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/config.ini"),
        help="Database config file when REMIHUB_DATABASE_URL is not set",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=MIGRATIONS_DIR,
        help="Directory containing migration SQL files",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args()
    migrations = discover_migrations(args.migrations_dir)
    conn = _connect(args.config)

    try:
        _ensure_migration_table(conn)
        _acquire_lock(conn)
        try:
            if args.command == "upgrade":
                count = upgrade(conn, migrations)
                logger.info("Applied %s migration(s)", count)
            elif args.command == "downgrade":
                count = downgrade(conn, migrations, args.steps)
                logger.info("Rolled back %s migration(s)", count)
            else:
                for item in status(conn, migrations):
                    state = "applied" if item["applied"] else "pending"
                    applied_at = f" at {item['applied_at']}" if item["applied_at"] else ""
                    print(f"{item['version']}_{item['name']}: {state}{applied_at}")
        finally:
            _release_lock(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
