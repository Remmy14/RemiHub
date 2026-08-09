import base64
import grp
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.core.agent_deployment import (
    DeploymentValidationError,
    LocalFrontendArtifactBuilder,
)


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-npm-cache-control"
)
DEPLOYMENT_CONTROL_PATH = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-deployment-control"
)
POLICY_PATH = ROOT / "deployments" / "agent_backend" / "frontend-web-policy.json"
SANDBOX_PATH = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-validation-sandbox"
)
INSTALLER_PATH = ROOT / "deployments" / "agent_backend" / "install-package.sh"
SYSTEMD_PATH = ROOT / "deployments" / "agent_backend" / "systemd"


def _load_cache_control():
    loader = importlib.machinery.SourceFileLoader(
        "remihub_backend_npm_cache_control_tests", str(CONTROL_PATH)
    )
    specification = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(specification)
    sys.modules[loader.name] = module
    unittest.addModuleCleanup(sys.modules.pop, loader.name, None)
    loader.exec_module(module)
    return module


def _load_deployment_control():
    loader = importlib.machinery.SourceFileLoader(
        "remihub_backend_deployment_control_npm_tests",
        str(DEPLOYMENT_CONTROL_PATH),
    )
    specification = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(specification)
    sys.modules[loader.name] = module
    unittest.addModuleCleanup(sys.modules.pop, loader.name, None)
    loader.exec_module(module)
    return module


npm_cache = _load_cache_control()


def _integrity(value=b"fixture"):
    return "sha512-" + base64.b64encode(hashlib.sha512(value).digest()).decode()


def _package_metadata(
    *,
    resolved="https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
    integrity=None,
):
    package = {
        "name": "fixture",
        "version": "1.0.0",
        "dependencies": {"left-pad": "1.3.0"},
    }
    lockfile = {
        "name": "fixture",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": "fixture",
                "version": "1.0.0",
                "dependencies": {"left-pad": "1.3.0"},
            },
            "node_modules/left-pad": {
                "version": "1.3.0",
                "resolved": resolved,
                "integrity": integrity or _integrity(),
            },
        },
    }
    return (
        json.dumps(package, sort_keys=True).encode(),
        json.dumps(lockfile, sort_keys=True).encode(),
    )


def _frontend_git_archive(package_json_bytes, lockfile_bytes):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        directory = tarfile.TarInfo("frontend-web")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name, payload in (
            ("frontend-web/package.json", package_json_bytes),
            ("frontend-web/package-lock.json", lockfile_bytes),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


class NpmCacheTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.policy_bytes = POLICY_PATH.read_bytes()
        self.policy = json.loads(self.policy_bytes)
        self.owner_patch = patch.object(npm_cache, "CACHE_OWNER_UID", os.getuid())
        # The production helper resolves the protected remihub-deployer group
        # by name. The validation sandbox intentionally does not expose host
        # account databases, so unit tests must not depend on NSS being able
        # to name the sandbox process GID. Patch the helper's group lookup to
        # the current numeric GID instead.
        self.group_patch = patch.object(
            npm_cache.grp,
            "getgrnam",
            return_value=SimpleNamespace(gr_gid=os.getgid()),
        )
        self.owner_patch.start()
        self.group_patch.start()
        self.addCleanup(self.owner_patch.stop)
        self.addCleanup(self.group_patch.stop)

    def _published_cache(self):
        lockfile_bytes = b'{"fixture":"lock"}\n'
        lockfile_sha256 = npm_cache._sha256_bytes(lockfile_bytes)
        cache = self.root / lockfile_sha256
        (cache / "_cacache" / "content-v2").mkdir(parents=True)
        (cache / "_cacache" / "content-v2" / "payload").write_bytes(
            b"dependency-bytes"
        )
        (cache / npm_cache.LOCKFILE_SNAPSHOT_NAME).write_bytes(lockfile_bytes)
        (cache / npm_cache.POLICY_SNAPSHOT_NAME).write_bytes(self.policy_bytes)
        npm_cache._harden_cache_tree(cache)
        npm_cache._write_cache_manifest(
            cache,
            lockfile_sha256=lockfile_sha256,
            candidate_commit="a" * 40,
            candidate_tree="b" * 40,
            node_version=self.policy["node_version"],
            npm_version=self.policy["npm_version"],
            policy=self.policy,
            policy_bytes=self.policy_bytes,
        )
        return cache, lockfile_sha256

    def _verify(self, cache, lockfile_sha256):
        return npm_cache.verify_cache(
            cache,
            lockfile_sha256=lockfile_sha256,
            node_version=self.policy["node_version"],
            npm_version=self.policy["npm_version"],
            policy=self.policy,
            policy_bytes=self.policy_bytes,
        )


class LockfilePolicyBehaviorTests(NpmCacheTestCase):
    def test_exact_registry_lockfile_is_accepted(self):
        package, lockfile = _package_metadata()
        identity = npm_cache.validate_frontend_inputs(
            package_json_bytes=package,
            lockfile_bytes=lockfile,
            policy=self.policy,
        )
        self.assertEqual(identity, npm_cache._sha256_bytes(lockfile))

    def test_unsupported_dependency_sources_fail_closed(self):
        rejected = (
            "http://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
            "https://user:password@registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
            "https://registry.npmjs.org:443/left-pad/-/left-pad-1.3.0.tgz",
            "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz?token=x",
            "https://github.com/example/left-pad.tgz",
            "git+ssh://git@github.com/example/left-pad.git",
            "file:../left-pad",
        )
        for resolved in rejected:
            package, lockfile = _package_metadata(resolved=resolved)
            with self.subTest(resolved=resolved), self.assertRaises(SystemExit):
                npm_cache.validate_frontend_inputs(
                    package_json_bytes=package,
                    lockfile_bytes=lockfile,
                    policy=self.policy,
                )

    def test_missing_malformed_integrity_and_links_are_rejected(self):
        package, lockfile_bytes = _package_metadata()
        for mutation in ("missing", "invalid", "link"):
            lockfile = json.loads(lockfile_bytes)
            metadata = lockfile["packages"]["node_modules/left-pad"]
            if mutation == "missing":
                metadata.pop("integrity")
            elif mutation == "invalid":
                metadata["integrity"] = "sha512-not-base64!"
            else:
                metadata["link"] = True
            with self.subTest(mutation=mutation), self.assertRaises(SystemExit):
                npm_cache.validate_frontend_inputs(
                    package_json_bytes=package,
                    lockfile_bytes=json.dumps(lockfile).encode(),
                    policy=self.policy,
                )

    def test_package_and_lockfile_dependency_maps_must_match(self):
        package, lockfile = _package_metadata()
        package_payload = json.loads(package)
        package_payload["dependencies"]["left-pad"] = "file:../left-pad"
        with self.assertRaises(SystemExit):
            npm_cache.validate_frontend_inputs(
                package_json_bytes=json.dumps(package_payload).encode(),
                lockfile_bytes=lockfile,
                policy=self.policy,
            )


class SafeSnapshotBehaviorTests(NpmCacheTestCase):
    @staticmethod
    def _archive(entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for info, payload in entries:
                archive.addfile(
                    info,
                    io.BytesIO(payload) if payload is not None else None,
                )
        return output.getvalue()

    def test_git_snapshot_rejects_symlink_without_reading_external_sentinel(self):
        sentinel = self.root / "outside-secret"
        sentinel.write_text("must-not-be-copied\n", encoding="utf-8")
        directory = tarfile.TarInfo("frontend-web")
        directory.type = tarfile.DIRTYPE
        link = tarfile.TarInfo("frontend-web/leak")
        link.type = tarfile.SYMTYPE
        link.linkname = str(sentinel)
        archive = self._archive(((directory, None), (link, None)))
        destination = self.root / "snapshot"

        with self.assertRaises(SystemExit):
            npm_cache._extract_frontend_archive(
                archive,
                destination,
                maximum_entries=10,
                maximum_file_bytes=1024,
            )

        self.assertEqual(sentinel.read_text(), "must-not-be-copied\n")
        self.assertFalse((destination / "frontend-web" / "leak").exists())

    def test_cache_copy_rejects_symlinks_and_hardlinks(self):
        for kind in ("symlink", "hardlink"):
            source = self.root / f"source-{kind}"
            source.mkdir()
            sentinel = self.root / f"sentinel-{kind}"
            sentinel.write_bytes(b"external")
            if kind == "symlink":
                (source / "entry").symlink_to(sentinel)
            else:
                os.link(sentinel, source / "entry")
            destination = self.root / f"destination-{kind}"

            with self.subTest(kind=kind), self.assertRaises(SystemExit):
                npm_cache._safe_copy_cache(source, destination, self.policy)

            self.assertEqual(sentinel.read_bytes(), b"external")
            self.assertFalse((destination / "entry").exists())


class CacheManifestBehaviorTests(NpmCacheTestCase):
    def test_first_time_preparation_publishes_and_exact_lockfile_reuses_cache(self):
        package, lockfile = _package_metadata()
        archive = _frontend_git_archive(package, lockfile)
        archive_holder = [archive]
        identity_holder = [("a" * 40, "b" * 40)]
        cache_root = self.root / "cache-root"
        prep_root = self.root / "prep-root"
        candidate = self.root / "candidate"
        candidate.mkdir()
        preparation_calls = []

        def fake_preparation_unit(**arguments):
            preparation_calls.append(arguments["cache"])
            cacache = arguments["cache"] / "_cacache" / "content-v2"
            cacache.mkdir(parents=True)
            (cacache / "dependency").write_bytes(b"downloaded")

        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        patches = (
            patch.object(npm_cache, "NPM_CACHE_ROOT", cache_root),
            patch.object(npm_cache, "NPM_PREP_ROOT", prep_root),
            patch.object(
                npm_cache,
                "_load_policy",
                return_value=(self.policy, self.policy_bytes),
            ),
            patch.object(npm_cache, "_candidate_path", return_value=candidate),
            patch.object(
                npm_cache,
                "_git_identity",
                side_effect=lambda _candidate: identity_holder[0],
            ),
            patch.object(
                npm_cache,
                "_git_frontend_archive",
                side_effect=lambda *args, **kwargs: archive_holder[0],
            ),
            patch.object(
                npm_cache,
                "_frontend_tool_versions",
                return_value=(
                    self.policy["node_version"],
                    self.policy["npm_version"],
                ),
            ),
            patch.object(
                npm_cache,
                "_run_preparation_unit",
                side_effect=fake_preparation_unit,
            ),
            patch.object(npm_cache.pwd, "getpwnam", return_value=account),
        )
        for active_patch in patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

        first = npm_cache.prepare("qa", str(candidate), "a" * 40, "b" * 40)
        second = npm_cache.prepare("qa", str(candidate), "a" * 40, "b" * 40)
        changed_lockfile = lockfile + b"\n"
        archive_holder[0] = _frontend_git_archive(package, changed_lockfile)
        identity_holder[0] = ("c" * 40, "d" * 40)
        changed = npm_cache.prepare(
            "qa", str(candidate), "c" * 40, "d" * 40
        )

        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "cached")
        self.assertEqual(changed["status"], "prepared")
        self.assertEqual(len(preparation_calls), 2)
        self.assertEqual(first["lockfile_sha256"], hashlib.sha256(lockfile).hexdigest())
        self.assertEqual(
            changed["lockfile_sha256"],
            hashlib.sha256(changed_lockfile).hexdigest(),
        )
        self.assertNotEqual(first["cache_path"], changed["cache_path"])
        self.assertTrue((Path(first["cache_path"]) / npm_cache.MANIFEST_NAME).is_file())

    def test_complete_manifest_verifies_and_records_provenance(self):
        cache, lockfile_sha256 = self._published_cache()
        evidence = self._verify(cache, lockfile_sha256)
        self.assertEqual(evidence["status"], "verified")
        self.assertGreater(evidence["entry_count"], 3)
        self.assertEqual(len(evidence["manifest_identity"]), 64)

    def test_one_byte_corruption_is_rejected(self):
        cache, lockfile_sha256 = self._published_cache()
        payload = cache / "_cacache" / "content-v2" / "payload"
        payload.write_bytes(b"dependency-byteX")
        payload.chmod(0o640)
        with self.assertRaises(SystemExit):
            self._verify(cache, lockfile_sha256)

    def test_missing_and_extra_entries_are_rejected(self):
        for mutation in ("missing", "extra"):
            cache, lockfile_sha256 = self._published_cache()
            if mutation == "missing":
                (cache / "_cacache" / "content-v2" / "payload").unlink()
            else:
                extra = cache / "unexpected"
                extra.write_bytes(b"extra")
                extra.chmod(0o640)
            with self.subTest(mutation=mutation), self.assertRaises(SystemExit):
                self._verify(cache, lockfile_sha256)
            shutil.rmtree(cache)

    def test_invalid_manifest_is_rejected(self):
        cache, lockfile_sha256 = self._published_cache()
        manifest = cache / npm_cache.MANIFEST_NAME
        manifest.write_text("{}\n", encoding="utf-8")
        manifest.chmod(0o640)
        with self.assertRaises(SystemExit):
            self._verify(cache, lockfile_sha256)


class LifecycleBehaviorTests(NpmCacheTestCase):
    def test_actual_npm_install_path_does_not_execute_lifecycle_script(self):
        npm_binary = shutil.which("npm")
        node_binary = shutil.which("node")
        if npm_binary is None or node_binary is None:
            self.skipTest("Node or npm is unavailable")
        staging = self.root / "staging"
        source = staging / "frontend-web"
        home = staging / "home"
        cache = staging / "cache"
        for path in (source, home, cache):
            path.mkdir(parents=True, exist_ok=True)
        marker = self.root / "lifecycle-ran"
        package = {
            "name": "lifecycle-fixture",
            "version": "1.0.0",
            "scripts": {
                "preinstall": (
                    "node -e \"require('fs').writeFileSync("
                    + json.dumps(str(marker))
                    + ", 'ran')\""
                )
            },
        }
        lockfile = {
            "name": "lifecycle-fixture",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {"name": "lifecycle-fixture", "version": "1.0.0"}
            },
        }
        (source / "package.json").write_text(json.dumps(package), encoding="utf-8")
        (source / "package-lock.json").write_text(
            json.dumps(lockfile), encoding="utf-8"
        )
        wrapper = self.root / "systemd-run-fixture"
        wrapper.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
workdir=''
while (($#)); do
  case "$1" in
    --setenv=*) export "${1#--setenv=}"; shift ;;
    --property=WorkingDirectory=*) workdir="${1#--property=WorkingDirectory=}"; shift ;;
    --property=*|--unit=*|--wait|--collect|--pipe|--quiet) shift ;;
    *) break ;;
  esac
done
export PATH="__NODE_BIN__:/usr/bin:/bin"
[[ -n "$workdir" ]] && cd "$workdir"
exec "$@"
""".replace("__NODE_BIN__", str(Path(node_binary).parent)),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        with patch.object(npm_cache, "SYSTEMD_RUN", str(wrapper)), patch.object(
            npm_cache, "NPM", npm_binary
        ):
            npm_cache._run_preparation_unit(
                staging=staging,
                source=source,
                home=home,
                cache=cache,
                registry_url=self.policy["registry_url"],
            )

        self.assertFalse(marker.exists())


class ExercisingFrontendBuilder(LocalFrontendArtifactBuilder):
    def _verify_prepared_cache(self, lockfile_sha256):
        return {
            "command": "behavioral cache verifier fixture",
            "duration_ms": 0,
            "return_code": 0,
            "stdout_sha256": "0" * 64,
            "stdout_tail": "verified",
            "stderr_tail": "",
            "network": "not_required",
        }


class FrontendBuilderBehaviorTests(NpmCacheTestCase):
    def _builder_fixture(self, *, mismatched=False):
        candidate = self.root / "candidate"
        frontend = candidate / "frontend-web"
        frontend.mkdir(parents=True)
        (frontend / "package.json").write_text(
            '{"name":"fixture"}\n', encoding="utf-8"
        )
        lockfile = frontend / "package-lock.json"
        lockfile.write_text('{"lockfileVersion":3}\n', encoding="utf-8")
        lockfile_sha256 = hashlib.sha256(lockfile.read_bytes()).hexdigest()
        cache = self.root / "cache" / lockfile_sha256
        cache.mkdir(parents=True)
        (cache / "dependency-token").write_text("offline\n", encoding="utf-8")
        node = self.root / "node"
        node.write_text("#!/bin/sh\necho v22.22.2\n", encoding="utf-8")
        node.chmod(0o755)
        counter = self.root / "build-counter"
        marker = self.root / "npm-command-ran"
        npm = self.root / "npm"
        npm.write_text(
            f"""#!/usr/bin/env python3
import os
from pathlib import Path
import sys
if sys.argv[1:] == ['--version']:
    print('10.9.7')
    raise SystemExit(0)
if os.environ.get('NPM_CONFIG_OFFLINE') != 'true':
    raise SystemExit('npm command was not offline')
Path({str(marker)!r}).write_text('ran')
if sys.argv[1:3] == ['ci', '--ignore-scripts']:
    if not (Path(os.environ['NPM_CONFIG_CACHE']) / 'dependency-token').is_file():
        raise SystemExit('prepared cache was not supplied')
    Path('node_modules').mkdir(exist_ok=True)
elif sys.argv[1:] == ['run', 'lint']:
    pass
elif sys.argv[1:] == ['run', 'build']:
    counter = Path({str(counter)!r})
    value = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(value))
    Path('dist').mkdir(exist_ok=True)
    output = str(value) if {mismatched!r} else 'stable'
    Path('dist/index.html').write_text(output)
else:
    raise SystemExit('unexpected npm arguments: ' + repr(sys.argv[1:]))
""",
            encoding="utf-8",
        )
        npm.chmod(0o755)
        builder = ExercisingFrontendBuilder(
            timeout_seconds=30,
            node_binary=node,
            npm_binary=npm,
            cache_root=self.root / "cache",
            environment="qa",
        )
        return builder, candidate, marker

    def _build(self, builder, candidate):
        return builder.build(
            candidate_worktree=candidate,
            artifact_root=self.root / "artifacts",
            card_id="11111111-1111-1111-1111-111111111111",
            card_revision=1,
            deployment_run_id="22222222-2222-2222-2222-222222222222",
            approval_id="33333333-3333-3333-3333-333333333333",
            implementation_run_id="44444444-4444-4444-4444-444444444444",
            candidate_commit="a" * 40,
            changed_files=("frontend-web/package-lock.json",),
        )

    def test_two_separate_offline_builds_produce_matching_identity(self):
        builder, candidate, marker = self._builder_fixture()
        evidence = self._build(builder, candidate)
        self.assertTrue(marker.is_file())
        self.assertTrue(evidence.reproducibility["matched"])
        self.assertEqual(
            evidence.reproducibility["first_identity"],
            evidence.reproducibility["second_identity"],
        )
        self.assertEqual(
            sum(
                command["command"].endswith("run build")
                for command in evidence.commands
            ),
            2,
        )

    def test_injected_artifact_difference_fails_closed(self):
        builder, candidate, _ = self._builder_fixture(mismatched=True)
        with self.assertRaisesRegex(
            DeploymentValidationError,
            "artifact identity is not reproducible",
        ):
            self._build(builder, candidate)

    def test_cache_verification_failure_prevents_npm_execution(self):
        builder, candidate, marker = self._builder_fixture()

        def reject(_lockfile_sha256):
            raise DeploymentValidationError("corrupted prepared cache")

        builder._verify_prepared_cache = reject
        with self.assertRaisesRegex(DeploymentValidationError, "corrupted"):
            self._build(builder, candidate)
        self.assertFalse(marker.exists())


class PermanentAssetContractTests(unittest.TestCase):
    def test_root_cache_work_runs_in_its_own_narrow_transient_unit(self):
        deployment_control = _load_deployment_control()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"verified"}\n',
            stderr="",
        )
        with patch.object(
            deployment_control.subprocess,
            "run",
            return_value=completed,
        ) as run:
            evidence = deployment_control._run_npm_cache_control(
                "verify", ["a" * 64]
            )

        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--property=User=root", command)
        self.assertNotIn("--property=NoNewPrivileges=yes", command)
        self.assertIn("--property=RestrictSUIDSGID=yes", command)
        self.assertIn("--property=RestrictAddressFamilies=AF_UNIX", command)
        self.assertIn("--property=IPAddressDeny=any", command)
        self.assertIn(
            "--property=ReadWritePaths=/var/cache/remihub-agent/npm",
            command,
        )
        self.assertIn(
            "--property=ReadWritePaths=/var/lib/remihub-agent/npm-prep",
            command,
        )
        self.assertEqual(command[-2:], ["verify", "a" * 64])
        self.assertEqual(evidence["status"], "verified")

    def test_worker_units_allow_netlink_but_keep_external_network_denied(self):
        for name in (
            "remihub-agent-deployment-qa.service.in",
            "remihub-agent-deployment-production.service.in",
        ):
            source = (SYSTEMD_PATH / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn(
                    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
                    source,
                )
                self.assertIn("IPAddressDeny=any", source)
                self.assertIn("IPAddressAllow=localhost", source)
                self.assertNotIn(
                    "ReadWritePaths=/var/cache/remihub-agent/npm", source
                )
                self.assertNotIn("zzzz-npm-auto-hotfix.conf", source)

    def test_validator_executes_network_behavioral_probe(self):
        source = SANDBOX_PATH.read_text(encoding="utf-8")
        self.assertIn("socket.AF_NETLINK", source)
        self.assertIn('socket.create_connection(("1.1.1.1", 443)', source)
        self.assertIn("--unshare-net", source)
        self.assertIn("frontend-cache-verify", source)

    def test_installer_owns_new_helper_and_cache_roots(self):
        source = INSTALLER_PATH.read_text(encoding="utf-8")
        self.assertIn("remihub-backend-npm-cache-control", source)
        self.assertIn("install -d -o root -g remihub-deployer -m 0750", source)
        self.assertIn("/var/cache/remihub-agent/npm", source)
        self.assertIn("/var/lib/remihub-agent/npm-prep", source)
        self.assertNotIn("zzzz-npm-auto-hotfix.conf", source)


if __name__ == "__main__":
    unittest.main()
