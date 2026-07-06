from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from backend.tasks.media_conversion import convert_mp4_to_mkv, result_to_dict


DEFAULT_ROOT = Path("/mnt/plex-pool/Movies")


def iter_mp4_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        if path.suffix.lower() != ".mp4":
            continue

        # Skip temp/remux artifacts just in case.
        if ".remuxing." in path.name:
            continue

        yield path

def format_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"

    seconds = int(seconds)

    if seconds < 60:
        return f"{seconds}s"

    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {sec}s"


def print_progress_report(
    *,
    index: int,
    total: int,
    current_file: Path | None,
    previous_result: dict | None,
    converted: int,
    skipped_existing: int,
    skipped: int,
    failed: int,
    started_at: float,
) -> None:
    remaining = max(total - index, 0)
    elapsed = time.monotonic() - started_at

    completed = index
    avg_per_file = elapsed / completed if completed > 0 else None
    estimated_remaining = avg_per_file * remaining if avg_per_file else None

    print("", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print(f"MP4 → MKV batch progress: {completed}/{total} complete", file=sys.stderr)
    print("-" * 80, file=sys.stderr)

    if previous_result:
        print("Just finished:", file=sys.stderr)
        print(f"  Source: {previous_result.get('source')}", file=sys.stderr)
        print(f"  Target: {previous_result.get('target')}", file=sys.stderr)
        print(f"  Status: {previous_result.get('status')}", file=sys.stderr)

        elapsed_seconds = previous_result.get("elapsed_seconds")
        if elapsed_seconds:
            print(f"  Time:   {format_seconds(elapsed_seconds)}", file=sys.stderr)

        message = previous_result.get("message")
        if message:
            print(f"  Note:   {message}", file=sys.stderr)

    print("", file=sys.stderr)

    if current_file:
        print("Doing now:", file=sys.stderr)
        print(f"  {current_file}", file=sys.stderr)
    else:
        print("Doing now:", file=sys.stderr)
        print("  Nothing — batch complete.", file=sys.stderr)

    print("", file=sys.stderr)
    print("Remaining:", file=sys.stderr)
    print(f"  Files left: {remaining}", file=sys.stderr)
    print(f"  Elapsed:    {format_seconds(elapsed)}", file=sys.stderr)
    print(f"  ETA:        {format_seconds(estimated_remaining)}", file=sys.stderr)

    print("", file=sys.stderr)
    print("Totals so far:", file=sys.stderr)
    print(f"  Converted:        {converted}", file=sys.stderr)
    print(f"  Skipped existing: {skipped_existing}", file=sys.stderr)
    print(f"  Skipped:          {skipped}", file=sys.stderr)
    print(f"  Failed:           {failed}", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print("", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remux .mp4 movie files to .mkv containers."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Root movie directory. Default: {DEFAULT_ROOT}",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually run ffmpeg. Without this, the script only prints what it would do.",
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Delete the original .mp4 after successful conversion.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .mkv files.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N files. Useful for testing.",
    )

    args = parser.parse_args()

    root = args.root

    if not root.exists():
        print(
            json.dumps(
                {
                    "status": "failed",
                    "message": f"Root path does not exist: {root}",
                }
            )
        )
        return 1

    files = list(iter_mp4_files(root))

    if args.limit is not None:
        files = files[: args.limit]

    summary = {
        "root": str(root),
        "dry_run": not args.apply,
        "delete_source": args.delete_source,
        "overwrite": args.overwrite,
        "found_mp4_count": len(files),
        "converted": 0,
        "skipped_existing": 0,
        "skipped": 0,
        "failed": 0,
        "would_convert": 0,
        "results": [],
    }

    batch_started = time.monotonic()

    if files:
        print_progress_report(
            index=0,
            total=len(files),
            current_file=files[0],
            previous_result=None,
            converted=summary["converted"],
            skipped_existing=summary["skipped_existing"],
            skipped=summary["skipped"],
            failed=summary["failed"],
            started_at=batch_started,
        )

    for index, source in enumerate(files, start=1):
        target = source.with_suffix(".mkv")

        if not args.apply:
            item = {
                "source": str(source),
                "target": str(target),
                "status": "would_convert",
            }
            summary["would_convert"] += 1
            summary["results"].append(item)

            next_file = files[index] if index < len(files) else None

            print_progress_report(
                index=index,
                total=len(files),
                current_file=next_file,
                previous_result=item,
                converted=summary["converted"],
                skipped_existing=summary["skipped_existing"],
                skipped=summary["skipped"],
                failed=summary["failed"],
                started_at=batch_started,
            )

            continue

        result = convert_mp4_to_mkv(
            source,
            delete_source=args.delete_source,
            overwrite=args.overwrite,
        )

        result_dict = result_to_dict(result)
        summary["results"].append(result_dict)

        if result.status in summary:
            summary[result.status] += 1
        else:
            summary["skipped"] += 1

        next_file = files[index] if index < len(files) else None

        print_progress_report(
            index=index,
            total=len(files),
            current_file=next_file,
            previous_result=result_dict,
            converted=summary["converted"],
            skipped_existing=summary["skipped_existing"],
            skipped=summary["skipped"],
            failed=summary["failed"],
            started_at=batch_started,
        )

    print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
