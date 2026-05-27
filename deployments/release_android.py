
import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path

from backend.database.database import get_db_conn, put_db_conn


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("release_android")

DEPLOYMENT_DIR = Path("/mnt/secure-pool/Q_Drive/Projects/RemiHub/deployments")
VERSION_FILE = DEPLOYMENT_DIR / "release_version.json"

ANDROID_APP_DIR = Path("/home/alex/StudioProjects/RemiHub-App/")
GRADLE_FILE = ANDROID_APP_DIR / "app" / "build.gradle.kts"
APK_OUTPUT_DIR = ANDROID_APP_DIR / "app" / "build" / "outputs" / "apk" / "release"
SERVER_RELEASE_DIR = Path("/opt/remihub/releases/android")
PLATFORM = "android"

REMOTE_SERVER = "alex@remillard-serv"
REMOTE_RELEASE_DIR = "/opt/remihub/releases/android"
APK_PUBLIC_RELATIVE_DIR = "releases/android"


def get_version_info(version_file: Path, release_type: str) -> tuple[int, str, int, int, int]:
    with open(version_file, "r") as f:
        version_info = json.load(f)

    version_code = version_info["version_code"]
    version_major = version_info["version_major"]
    version_minor = version_info["version_minor"]
    version_patch = version_info["version_patch"]

    # Iterate the version code
    version_code += 1

    # Iterate the build number based on release_type
    if release_type == "major":
        version_major += 1
        version_minor = version_patch = 0
    elif release_type == "minor":
        version_minor += 1
        version_patch = 0
    elif release_type == "patch":
        version_patch += 1

    version_name = f"{version_major}.{version_minor}.{version_patch}"

    return version_code, version_name, version_major, version_minor, version_patch


def build_release_apk(android_app_dir: Path, version_code: int, version_major: int, version_minor: int, version_patch: int):
    gradlew_cmd = [
        "./gradlew",
        "assembleRelease",
        f"-PRELEASE_VERSION_CODE={version_code}",
        f"-PRELEASE_VERSION_MAJOR={version_major}",
        f"-PRELEASE_VERSION_MINOR={version_minor}",
        f"-PRELEASE_VERSION_PATCH={version_patch}",
    ]

    logger.info("Building release APK")
    subprocess.run(
        gradlew_cmd,
        cwd=android_app_dir,
        check=True,
    )


def find_release_apk(apk_output_dir: Path) -> Path:
    apks = sorted(
        apk_output_dir.glob("*.apk"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not apks:
        raise RuntimeError(f"No APK found in {apk_output_dir}")

    for apk in apks:
        if "unsigned" not in apk.name.lower():
            return apk

    return apks[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_apk(source_apk: Path, version_code: int, version_name: str):
    # SERVER_RELEASE_DIR.mkdir(parents=True, exist_ok=True)

    filename = f"remihub-v{version_code}-{version_name}.apk"
    remote_path = f"{REMOTE_SERVER}:{REMOTE_RELEASE_DIR}/{filename}"

    file_size = source_apk.stat().st_size
    file_hash = sha256_file(source_apk)
    relative_path = f"releases/android/{filename}"

    subprocess.run(
        ["scp", str(source_apk), remote_path],
        check=True,
    )

    logger.info("Published APK to %s", remote_path)
    return filename, relative_path, file_size, file_hash


def upsert_release_row(
    platform: str,
    version_code: int,
    version_name: str,
    apk_filename: str,
    apk_relative_path: str,
    apk_sha256: str,
    file_size_bytes: int,
):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app_release
                SET is_active = FALSE
                WHERE platform = %s;
                """,
                (platform,),
            )

            cur.execute(
                """
                INSERT INTO app_release (
                    platform,
                    version_code,
                    version_name,
                    apk_filename,
                    apk_relative_path,
                    apk_sha256,
                    file_size_bytes,
                    is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id;
                """,
                (
                    platform,
                    version_code,
                    version_name,
                    apk_filename,
                    apk_relative_path,
                    apk_sha256,
                    file_size_bytes,
                ),
            )
            release_id = cur.fetchone()[0]

        conn.commit()
        logger.info("Inserted app_release row id=%s", release_id)
        return release_id
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def save_version_info(
    version_file: Path,
    version_code: int,
    version_major: int,
    version_minor: int,
    version_patch: int,
):
    version_info = {
        "version_code": version_code,
        "version_major": version_major,
        "version_minor": version_minor,
        "version_patch": version_patch,
    }

    with open(version_file, "w") as f:
        json.dump(version_info, f, indent=2)
        f.write("\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Build and publish an Android release")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--patch", action="store_true", help="Create a patch release")
    group.add_argument("--minor", action="store_true", help="Create a minor release")
    group.add_argument("--major", action="store_true", help="Create a major release")

    return parser.parse_args()


def main():
    args = parse_args()

    if args.patch:
        release_type = "patch"
    elif args.minor:
        release_type = "minor"
    elif args.major:
        release_type = "major"
    else:
        raise RuntimeError("No release type selected")

    logger.info("Selected release type: %s", release_type)

    version_code, version_name, version_major, version_minor, version_patch = get_version_info(VERSION_FILE, release_type)

    logger.info(
        "Preparing release versionCode=%s versionName=%s",
        version_code,
        version_name,
    )

    build_release_apk(
        ANDROID_APP_DIR,
        version_code=version_code,
        version_major=version_major,
        version_minor=version_minor,
        version_patch=version_patch,
    )
    source_apk = find_release_apk(APK_OUTPUT_DIR)

    apk_filename, apk_relative_path, file_size_bytes, apk_sha256 = publish_apk(
        source_apk=source_apk,
        version_code=version_code,
        version_name=version_name,
    )

    release_id = upsert_release_row(
        platform=PLATFORM,
        version_code=version_code,
        version_name=version_name,
        apk_filename=apk_filename,
        apk_relative_path=apk_relative_path,
        apk_sha256=apk_sha256,
        file_size_bytes=file_size_bytes,
    )

    save_version_info(
        VERSION_FILE,
        version_code=version_code,
        version_major=version_major,
        version_minor=version_minor,
        version_patch=version_patch,
    )

    logger.info("Release complete")
    logger.info("release_id=%s", release_id)
    logger.info("apk=%s", apk_filename)
    logger.info("sha256=%s", apk_sha256)


if __name__ == "__main__":
    main()
