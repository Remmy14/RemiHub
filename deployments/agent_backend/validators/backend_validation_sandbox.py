#!/usr/bin/env python3
"""Isolated execution environment for trusted backend validation.

This launcher is intentionally separate from the existing Codex sandbox.
It does not mount Codex credentials, production configuration, or writable
implementation directories.

Stage 4B: development-only isolation testing.
"""

import os
import subprocess
import sys
from pathlib import Path

BWRAP = Path("/usr/bin/bwrap")
WORKTREE_ROOT = Path("/opt/remihub-agent/implementation/worktrees")
REPOSITORY = Path("/opt/remihub-agent/repositories/remihub-implementation.git")
PYTHON = Path("/opt/remihub/.venv/bin/python")


def sandbox_command(worktree: Path, command: list[str]) -> list[str]:
    worktree = worktree.resolve(strict=True)
    root = WORKTREE_ROOT.resolve(strict=True)

    if worktree == root or root not in worktree.parents:
        raise ValueError("Worktree is outside the approved implementation root")

    if not worktree.is_dir():
        raise ValueError("Worktree must be a directory")

    if not BWRAP.is_file():
        raise RuntimeError("Bubblewrap is unavailable")

    if not PYTHON.is_file():
        raise RuntimeError("Validation Python environment is unavailable")

    args = [
        str(BWRAP),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
        "--ro-bind", "/usr", "/usr",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/run",
        "--dir", "/etc",
        "--dir", "/home",
        "--dir", "/opt",
        "--dir", "/opt/remihub-agent",
        "--dir", "/opt/remihub",
        "--dir", "/opt/remihub-agent/implementation",
        "--dir", str(root),
        "--dir", "/opt/remihub-agent/repositories",
        "--ro-bind", str(PYTHON.parent.parent), str(PYTHON.parent.parent),
        "--ro-bind", str(REPOSITORY), str(REPOSITORY),
        "--ro-bind", str(worktree), str(worktree),
        "--setenv", "PATH", "/opt/remihub/.venv/bin:/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PYTHONPYCACHEPREFIX", "/tmp/python-cache",
        "--setenv", "PYTHONPATH", str(worktree),
        "--setenv", "REMIHUB_DATABASE_CONFIG", "/tmp/remihub-test-config.ini",
        "--setenv", "REMIHUB_CONFIG_FILE", "/tmp/remihub-test-config.ini",
        "--setenv", "REMIHUB_ENV_FILE", "/tmp/nonexistent.env",
        "--setenv", "REMIHUB_LOG_DIR", "/tmp/remihub-test-logs",
        "--setenv", "GIT_CONFIG_NOSYSTEM", "1",
        "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null",
        "--setenv", "GIT_OPTIONAL_LOCKS", "0",
        "--setenv", "GIT_TERMINAL_PROMPT", "0",
        "--chdir", str(worktree),
        "--",
    ]

    # Required dynamic linker paths vary by distribution.
    for path in ("/lib", "/lib64", "/bin", "/sbin"):
        p = Path(path)
        if p.is_symlink():
            args[args.index("--proc"):args.index("--proc")] = [
                "--symlink", os.readlink(path), path
            ]
        elif p.is_dir():
            args[args.index("--proc"):args.index("--proc")] = [
                "--ro-bind", path, path
            ]

    bootstrap = """
import os
import pathlib
import subprocess
import sys

# Synthetic namespace identity only; never expose the host account database.
pathlib.Path("/etc/passwd").write_text(
    f"remihub-validator:x:{os.getuid()}:{os.getgid()}:Isolated validation:/tmp:/usr/sbin/nologin\\n"
)
pathlib.Path("/etc/group").write_text(f"remihub-validator:x:{os.getgid()}:\\n")

config = pathlib.Path("/tmp/remihub-test-config.ini")
config.write_text(
    "[Database]\\n"
    "user=offline_test\\n"
    "password=offline_test\\n"
    "host=127.0.0.1\\n"
    "port=5432\\n"
    "database=offline_test\\n"
)

raise SystemExit(subprocess.call(sys.argv[1:]))
"""

    return args + [str(PYTHON), "-c", bootstrap, *command]


def main():
    if len(sys.argv) < 3:
        raise SystemExit(
            "Usage: backend_validation_sandbox.py WORKTREE COMMAND [ARGS...]"
        )

    command = sandbox_command(Path(sys.argv[1]), sys.argv[2:])
    result = subprocess.run(command, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
