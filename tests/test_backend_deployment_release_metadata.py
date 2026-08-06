from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-deployment-control"
)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_helper():
    loader = SourceFileLoader("backend_deployment_control", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise AssertionError("helper module spec is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    try:
        loader.exec_module(module)
    finally:
        sys.modules.pop(loader.name, None)
    return module


class BackendDeploymentRuntimeCleanlinessTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_helper()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.runtime = self.root / "runtime"
        self.target = self.root / "target.git"

        self.source.mkdir()
        _git(self.source, "init", "-b", "main")
        _git(self.source, "config", "user.name", "RemiHub Test")
        _git(self.source, "config", "user.email", "remihub-test@invalid.local")
        (self.source / "backend").mkdir()
        (self.source / "backend" / "example.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        release = self.source / "deployments" / "release_version.json"
        release.parent.mkdir(parents=True)
        release.write_text(
            '{\n  "version_code": 70,\n  "version_major": 0,\n'
            '  "version_minor": 8,\n  "version_patch": 16\n}\n',
            encoding="utf-8",
        )
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "Base")
        self.base = _git(self.source, "rev-parse", "HEAD")

        (self.source / "backend" / "example.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "Candidate")
        self.candidate = _git(self.source, "rev-parse", "HEAD")

        subprocess.run(
            ["git", "clone", "--bare", str(self.source), str(self.target)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clone", str(self.source), str(self.runtime)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(self.runtime, "checkout", "-B", "main", self.base)
        self.environment = self.module.Environment(
            service="production.service",
            target_repo=self.target,
            target_branch="production-main",
            runtime=self.runtime,
            runtime_branch="main",
            runtime_user="root",
            frontend_backup_root=self.root / "frontend-backups",
        )

    def test_clean_runtime_is_required(self):
        self.module.require_runtime_state(self.environment, self.base)

    def test_release_seed_modification_is_rejected(self):
        release = self.runtime / "deployments" / "release_version.json"
        release.write_text(
            '{"version_code": 71, "version_major": 0, '
            '"version_minor": 8, "version_patch": 17}\n',
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            self.module.require_runtime_state(self.environment, self.base)

    def test_unrelated_runtime_modification_is_rejected(self):
        (self.runtime / "backend" / "example.py").write_text(
            "VALUE = 99\n", encoding="utf-8"
        )
        with self.assertRaises(SystemExit):
            self.module.require_runtime_state(self.environment, self.base)

    def test_runtime_reset_no_longer_restores_tracked_operational_state(self):
        self.module.reset_runtime_preserving_operational_release(
            self.environment,
            expected_before=self.base,
            target=self.candidate,
        )
        self.assertEqual(_git(self.runtime, "rev-parse", "HEAD"), self.candidate)
        self.assertEqual(
            (self.runtime / "backend" / "example.py").read_text(encoding="utf-8"),
            "VALUE = 2\n",
        )
        self.assertEqual(_git(self.runtime, "status", "--porcelain=v1"), "")

    def test_helper_has_no_tracked_release_dirty_exception(self):
        source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn("OperationalReleaseSnapshot", source)
        self.assertNotIn(" M deployments/release_version.json", source)
        self.assertNotIn("ANDROID_RELEASE_LOCK", source)


if __name__ == "__main__":
    unittest.main()
