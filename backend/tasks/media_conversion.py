from __future__ import annotations

import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ConversionResult:
    source: str
    target: str
    status: str
    message: str = ""
    elapsed_seconds: float = 0.0


def convert_mp4_to_mkv(
    source_path: str | Path,
    *,
    delete_source: bool = False,
    overwrite: bool = False,
) -> ConversionResult:
    """
    Remux an .mp4 file into an .mkv container without transcoding video/audio.

    This uses ffmpeg stream copy, so it should be fast and preserve quality.
    The source file is deleted only after ffmpeg succeeds and the final .mkv
    has been atomically moved into place.

    Args:
        source_path: Path to the .mp4 file.
        delete_source: Delete the original .mp4 after successful conversion.
        overwrite: Replace an existing .mkv if one already exists.

    Returns:
        ConversionResult describing what happened.
    """
    started = time.monotonic()

    source = Path(source_path)

    if not source.exists():
        return ConversionResult(
            source=str(source),
            target="",
            status="failed",
            message="Source file does not exist",
        )

    if not source.is_file():
        return ConversionResult(
            source=str(source),
            target="",
            status="failed",
            message="Source path is not a file",
        )

    if source.suffix.lower() != ".mp4":
        return ConversionResult(
            source=str(source),
            target="",
            status="skipped",
            message="Source is not an .mp4 file",
        )

    target = source.with_suffix(".mkv")

    if target.exists() and not overwrite:
        return ConversionResult(
            source=str(source),
            target=str(target),
            status="skipped_existing",
            message="Target .mkv already exists",
        )

    # Keep temp file in the same directory so the final rename is atomic.
    temp_target = source.with_name(f".{source.stem}.remuxing.mkv")

    if temp_target.exists():
        temp_target.unlink()

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c",
        "copy",
        "-c:s",
        "srt",
        "-max_muxing_queue_size",
        "4096",
        str(temp_target),
    ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    elapsed = round(time.monotonic() - started, 2)

    if proc.returncode != 0:
        if temp_target.exists():
            temp_target.unlink()

        return ConversionResult(
            source=str(source),
            target=str(target),
            status="failed",
            message=proc.stderr.strip()[-4000:],
            elapsed_seconds=elapsed,
        )

    if not temp_target.exists() or temp_target.stat().st_size == 0:
        if temp_target.exists():
            temp_target.unlink()

        return ConversionResult(
            source=str(source),
            target=str(target),
            status="failed",
            message="ffmpeg completed but output file was missing or empty",
            elapsed_seconds=elapsed,
        )

    temp_target.replace(target)

    if delete_source:
        source.unlink()

    return ConversionResult(
        source=str(source),
        target=str(target),
        status="converted",
        message="Converted successfully",
        elapsed_seconds=elapsed,
    )


def result_to_dict(result: ConversionResult) -> dict:
    return asdict(result)
