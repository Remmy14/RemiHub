import grp
import importlib.machinery
import importlib.util
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_helper():
    helper_path = (
        Path(__file__).resolve().parents[1]
        / "deployments"
        / "agent_backend"
        / "libexec"
        / "remihub-backend-deployment-control"
    )
    loader = importlib.machinery.SourceFileLoader(
        "remihub_backend_deployment_release_metadata_test",
        str(helper_path),
    )
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


def _write_counter(path: Path, *, code: int, patch_number: int) -> bytes:
    content = (
        "{\n"
        f'  "version_code": {code},\n'
        '  "version_major": 0,\n'
        '  "version_minor": 8,\n'
        f'  "version_patch": {patch_number}\n'
        "}\n"
    ).encode("utf-8")
    path.write_bytes(content)
    return content


class BackendDeploymentReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_helper()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.target = self.root / "target.git"
        self.runtime = self.root / "runtime"
        self.lock = self.root / "android-release.lock"

        self.source.mkdir()
        _git(self.source, "init", "-b", "main")
        _git(self.source, "config", "user.name", "RemiHub Test")
        _git(self.source, "config", "user.email", "remihub-test@invalid.local")
        (self.source / "backend").mkdir()
        (self.source / "backend" / "example.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        release_path = (
            self.source
            / self.module.RELEASE_VERSION_RELATIVE
        )
        release_path.parent.mkdir(parents=True)
        _write_counter(release_path, code=63, patch_number=9)
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "Base")
        self.base = _git(self.source, "rev-parse", "HEAD")

        subprocess.run(
            ["git", "clone", "--bare", str(self.source), str(self.target)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(
            self.target,
            "update-ref",
            "refs/heads/production-main",
            self.base,
        )
        subprocess.run(
            [
                "git",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(self.target),
                str(self.runtime),
            ],
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
        )
        self.owner = SimpleNamespace(pw_uid=os.getuid())
        self.group = SimpleNamespace(gr_gid=os.getgid())
        self.release_path = (
            self.runtime
            / self.module.RELEASE_VERSION_RELATIVE
        )
        self.live_content = _write_counter(
            self.release_path,
            code=68,
            patch_number=14,
        )
        self.release_path.chmod(self.module.RELEASE_VERSION_MODE)

    def _identity_patches(self):
        return (
            patch.object(
                self.module.pwd,
                "getpwnam",
                return_value=self.owner,
            ),
            patch.object(
                self.module.grp,
                "getgrnam",
                return_value=self.group,
            ),
            patch.object(
                self.module,
                "ANDROID_RELEASE_LOCK",
                self.lock,
            ),
        )

    def _candidate(self):
        _git(self.source, "checkout", "-b", "candidate")
        (self.source / "backend" / "example.py").write_text(
            "VALUE = 2\n",
            encoding="utf-8",
        )
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "Candidate")
        candidate = _git(self.source, "rev-parse", "HEAD")
        branch = (
            "deployment/card-"
            "11111111-1111-4111-8111-111111111111/r1"
        )
        _git(
            self.source,
            "push",
            str(self.target),
            f"HEAD:refs/heads/{branch}",
        )
        return candidate, branch

    def test_valid_operational_release_is_allowed(self):
        with self._identity_patches()[0], self._identity_patches()[1]:
            self.module.require_runtime_state(
                self.environment,
                self.base,
            )

        self.assertEqual(
            _git(
                self.runtime,
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
            ),
            "M deployments/release_version.json",
        )

    def test_unrelated_runtime_modification_is_rejected(self):
        (self.runtime / "backend" / "example.py").write_text(
            "VALUE = 99\n",
            encoding="utf-8",
        )
        with (
            self._identity_patches()[0],
            self._identity_patches()[1],
            self.assertRaises(SystemExit),
        ):
            self.module.require_runtime_state(
                self.environment,
                self.base,
            )

    def test_staged_release_metadata_is_rejected(self):
        _git(
            self.runtime,
            "add",
            self.module.RELEASE_VERSION_RELATIVE.as_posix(),
        )
        with (
            self._identity_patches()[0],
            self._identity_patches()[1],
            self.assertRaises(SystemExit),
        ):
            self.module.require_runtime_state(
                self.environment,
                self.base,
            )

    def test_invalid_release_metadata_is_rejected(self):
        self.release_path.write_text(
            '{"version_code": true}\n',
            encoding="utf-8",
        )
        with (
            self._identity_patches()[0],
            self._identity_patches()[1],
            self.assertRaises(SystemExit),
        ):
            self.module.require_runtime_state(
                self.environment,
                self.base,
            )

    def test_regressed_operational_release_is_rejected(self):
        _write_counter(
            self.release_path,
            code=62,
            patch_number=8,
        )
        self.release_path.chmod(self.module.RELEASE_VERSION_MODE)
        with (
            self._identity_patches()[0],
            self._identity_patches()[1],
            self.assertRaises(SystemExit),
        ):
            self.module.require_runtime_state(
                self.environment,
                self.base,
            )

    def test_reset_failure_still_restores_operational_release(self):
        candidate, branch = self._candidate()
        _git(
            self.runtime,
            "fetch",
            str(self.target),
            f"refs/heads/{branch}:refs/remotes/test/candidate",
        )
        before = self.release_path.stat()
        original_run = self.module.run

        def fail_after_reset(command, *, user=None, check=True):
            result = original_run(
                command,
                user=user,
                check=check,
            )
            if command[-3:] == ["reset", "--hard", candidate]:
                raise RuntimeError("simulated response loss after reset")
            return result

        identity_patches = self._identity_patches()
        with (
            identity_patches[0],
            identity_patches[1],
            identity_patches[2],
            patch.object(
                self.module,
                "run",
                side_effect=fail_after_reset,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "simulated response loss",
            ),
        ):
            self.module.reset_runtime_preserving_operational_release(
                self.environment,
                expected_before=self.base,
                target=candidate,
            )

        self.assertEqual(
            self.release_path.read_bytes(),
            self.live_content,
        )
        after = self.release_path.stat()
        self.assertEqual(
            stat.S_IMODE(after.st_mode),
            stat.S_IMODE(before.st_mode),
        )
        self.assertEqual(after.st_uid, before.st_uid)
        self.assertEqual(after.st_gid, before.st_gid)

    def test_qa_runtime_does_not_allow_operational_release_delta(self):
        qa_environment = self.module.Environment(
            service="qa.service",
            target_repo=self.target,
            target_branch="qa-main",
            runtime=self.runtime,
            runtime_branch="main",
            runtime_user="root",
        )
        with self.assertRaises(SystemExit):
            self.module.require_runtime_state(
                qa_environment,
                self.base,
            )

    def test_promote_and_restore_preserve_exact_operational_release(self):
        candidate, branch = self._candidate()
        before = self.release_path.stat()

        identity_patches = self._identity_patches()
        with identity_patches[0], identity_patches[1], identity_patches[2]:
            self.module.promote(
                self.environment,
                [
                    branch,
                    candidate,
                    self.base,
                    (
                        "rollback-before-agent-card-"
                        "11111111-1111-4111-8111-111111111111-r1"
                    ),
                ],
            )

            self.assertEqual(
                _git(self.runtime, "rev-parse", "HEAD"),
                candidate,
            )
            self.assertEqual(
                self.release_path.read_bytes(),
                self.live_content,
            )
            promoted = self.release_path.stat()
            self.assertEqual(
                stat.S_IMODE(promoted.st_mode),
                stat.S_IMODE(before.st_mode),
            )
            self.assertEqual(promoted.st_uid, before.st_uid)
            self.assertEqual(promoted.st_gid, before.st_gid)
            self.assertEqual(
                _git(
                    self.runtime,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=no",
                ),
                "M deployments/release_version.json",
            )

            self.module.restore(
                self.environment,
                [candidate, self.base],
            )

        self.assertEqual(
            _git(self.runtime, "rev-parse", "HEAD"),
            self.base,
        )
        self.assertEqual(
            self.release_path.read_bytes(),
            self.live_content,
        )
        restored = self.release_path.stat()
        self.assertEqual(
            stat.S_IMODE(restored.st_mode),
            stat.S_IMODE(before.st_mode),
        )
        self.assertEqual(restored.st_uid, before.st_uid)
        self.assertEqual(restored.st_gid, before.st_gid)


if __name__ == "__main__":
    unittest.main()
