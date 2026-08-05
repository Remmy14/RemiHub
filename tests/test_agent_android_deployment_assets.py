from __future__ import annotations

import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "deployments" / "agent_android"
CONTROL = ASSETS / "libexec" / "remihub-android-release-control"
VALIDATOR = ASSETS / "libexec" / "remihub-android-release-validator"
PROBE = ASSETS / "libexec" / "remihub-android-release-counter-namespace-probe"
PREFLIGHT = ASSETS / "libexec" / "remihub-android-canonical-index-preflight"
EXPECTED_HASHES = {
    CONTROL: "4101d16a8d1da77c6648c85c321ceadb8f407d641ef9326019e982baa2be940a",
    VALIDATOR: "72b068950ca764817344b30b1c3635395174ea29092f154fd4318f00d66973c2",
    PROBE: "9a2f7c0aa0b7a5955d0746b84242e9a27af6eee0c94ace280e9c3b112540dc01",
    PREFLIGHT: "8185021080406ed2174092b9e459ece52baac8e856bf1109cc5ee7d2855651a0",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_control():
    name = "remihub_android_release_control"
    loader = SourceFileLoader(name, str(CONTROL))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError("unable to load protected release helper")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AndroidDeploymentAssetTests(unittest.TestCase):
    def test_assets_match_proven_production_bytes(self):
        for path, expected in EXPECTED_HASHES.items():
            self.assertTrue(path.is_file(), path)
            self.assertFalse(path.is_symlink(), path)
            self.assertEqual(sha256(path), expected, path)

    def test_release_helper_contains_all_proven_hardening(self):
        source = CONTROL.read_text(encoding="utf-8")
        required = (
            "--upload-pack=git -c ",
            "safe.directory={TARGET_REPO.resolve()} upload-pack",
            "def verify_authenticated_update_boundary",
            '"/app-update/latest"',
            '"/app-update/download/{release_id}"',
            'authenticate = headers.get("www-authenticate", "")',
            "SELECT id, platform, version_code",
            "'platform':r[1]",
            '"platform":"android"',
            "active release database verification failed",
            "published APK no longer matches signed release",
            'VERSION_FILE = Path("/var/lib/remihub-agent/android-release-counter/release_version.json")',
            'rev(CANONICAL_REPO, "master", user="alex")',
            'PLANNING_REPO = Path("/opt/remihub-agent/repositories/remihub-android-planning")',
            'rev(PLANNING_REPO, "HEAD", user="alex")',
        )
        for value in required:
            self.assertIn(value, source)

    def test_authentication_boundary_accepts_only_openapi_and_bearer_401(self):
        module = load_control()
        openapi = json.dumps(
            {
                "paths": {
                    "/app-update/latest": {},
                    "/app-update/download/{release_id}": {},
                }
            }
        ).encode()
        responses = [
            (200, {}, openapi),
            (401, {"www-authenticate": "Bearer"}, b""),
            (401, {"www-authenticate": "Bearer realm=remihub"}, b""),
        ]
        with patch.object(module, "http_response", side_effect=responses):
            observed = module.verify_authenticated_update_boundary(70)
        self.assertEqual(observed["openapi_status"], 200)
        self.assertEqual(observed["latest_unauthenticated_status"], 401)
        self.assertEqual(observed["download_authentication_challenge"], "Bearer")

    def test_authentication_boundary_rejects_unauthenticated_200(self):
        module = load_control()
        openapi = json.dumps(
            {
                "paths": {
                    "/app-update/latest": {},
                    "/app-update/download/{release_id}": {},
                }
            }
        ).encode()
        responses = [(200, {}, openapi), (200, {}, b"{}")]
        with patch.object(module, "http_response", side_effect=responses):
            with self.assertRaisesRegex(
                module.ReleaseError, "expected 401"
            ):
                module.verify_authenticated_update_boundary(70)

    def test_authentication_boundary_rejects_401_without_bearer(self):
        module = load_control()
        openapi = json.dumps(
            {
                "paths": {
                    "/app-update/latest": {},
                    "/app-update/download/{release_id}": {},
                }
            }
        ).encode()
        responses = [(200, {}, openapi), (401, {}, b"")]
        with patch.object(module, "http_response", side_effect=responses):
            with self.assertRaisesRegex(
                module.ReleaseError, "Bearer authentication challenge"
            ):
                module.verify_authenticated_update_boundary(70)

    def test_active_state_preserves_platform(self):
        module = load_control()
        row = {
            "id": 70,
            "platform": "android",
            "version_code": 65,
            "version_name": "0.8.11",
            "apk_filename": "remihub-v65-0.8.11.apk",
            "apk_relative_path": "releases/android/remihub-v65-0.8.11.apk",
            "apk_sha256": "a" * 64,
            "file_size_bytes": 10,
            "is_active": True,
        }
        with patch.object(
            module,
            "db_action",
            return_value={
                "active_rows": [row],
                "releases": [row],
                "max_version_code": 65,
            },
        ):
            observed = module.active_state()
        self.assertEqual(observed["active"]["platform"], "android")

    def test_release_validator_contract_is_offline_and_source_safe(self):
        source = VALIDATOR.read_text(encoding="utf-8")
        required = (
            "set -euo pipefail",
            "CONFIG=/etc/remihub-agent/android-release-validator.conf",
            "--unshare-net",
            "./gradlew --offline --no-daemon --console=plain",
            ":app:testDebugUnitTest",
            ":app:lintDebug",
            ":app:assembleDebug",
            ":app:assembleRelease",
            "candidate changed during validation",
            "protected build files changed",
            "release candidate is unexpectedly signed",
        )
        for value in required:
            self.assertIn(value, source)

    def test_namespace_probe_self_test_leaves_counter_unchanged(self):
        result = subprocess.run(
            [sys.executable, "-B", str(PROBE), "--self-test"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release_counter_probe_self_test=passed", result.stdout)

    def test_systemd_and_sudo_boundaries_are_narrow(self):
        runtime = (
            ASSETS / "systemd" / "30-runtime-working-directory.conf.in"
        ).read_text(encoding="utf-8")
        namespace = (
            ASSETS / "systemd" / "40-release-counter-namespace.conf"
        ).read_text(encoding="utf-8")
        canonical = (
            ASSETS / "systemd" / "50-canonical-index-preflight.conf"
        ).read_text(encoding="utf-8")
        sudoers = (
            ASSETS / "sudoers" / "remihub-android-release"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            runtime,
            "[Service]\nWorkingDirectory=@ANDROID_DEPLOYMENT_RUNTIME@\n",
        )
        self.assertEqual(
            namespace,
            "[Service]\n"
            "ReadWritePaths=/var/lib/remihub-agent/android-release-counter\n"
            "ExecStartPre=+/usr/local/libexec/"
            "remihub-android-release-counter-namespace-probe\n",
        )
        self.assertEqual(
            canonical,
            "[Service]\n"
            "ExecStartPre=+/usr/local/libexec/"
            "remihub-android-canonical-index-preflight\n",
        )
        self.assertEqual(
            sudoers,
            "Defaults:remihub-deployer env_reset\n"
            "remihub-deployer ALL=(root) NOPASSWD: "
            "/usr/local/libexec/remihub-android-release-control *\n",
        )
        namespace_lines = namespace.splitlines()
        self.assertNotIn("ProtectSystem=false", namespace_lines)
        self.assertNotIn("ReadWritePaths=/opt", namespace_lines)


if __name__ == "__main__":
    unittest.main()
