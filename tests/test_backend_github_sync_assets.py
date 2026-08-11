import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKER = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-github-sync"
)
CONTROL = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-github-sync-control"
)
BASELINE_OBSERVER = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-deployment-baseline-observer"
)
DEPLOYMENT_CONTROL = (
    ROOT
    / "deployments"
    / "agent_backend"
    / "libexec"
    / "remihub-backend-deployment-control"
)


class BackendGithubSyncAssetTests(unittest.TestCase):
    def test_network_worker_never_opens_canonical_worktree_index(self):
        source = WORKER.read_text(encoding="utf-8")
        self.assertIn('GIT_DIR = REPO / ".git"', source)
        self.assertIn('f"--git-dir={GIT_DIR}"', source)
        self.assertNotIn('"-C", str(REPO)', source)
        self.assertNotIn('git("status"', source)
        self.assertNotIn("release_sha256", source)
        self.assertNotIn("ALLOWED_DIRTY", source)

    def test_root_control_runs_every_canonical_worktree_git_as_alex(self):
        source = CONTROL.read_text(encoding="utf-8")
        self.assertNotIn("ALLOWED_DIRTY", source)
        self.assertNotIn("RELEASE_FILE", source)
        self.assertIn('fail("canonical worktree is not clean")', source)
        tree = ast.parse(source)
        canonical_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "git_worktree":
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            if node.args[0].id != "CANONICAL_REPO":
                continue
            canonical_calls.append(node)
        self.assertGreaterEqual(len(canonical_calls), 7)
        for call in canonical_calls:
            user_values = [
                keyword.value.value
                for keyword in call.keywords
                if keyword.arg == "user"
                and isinstance(keyword.value, ast.Constant)
            ]
            self.assertEqual(user_values, ["alex"])

    def test_control_rechecks_clean_worktree_after_network_operation(self):
        source = CONTROL.read_text(encoding="utf-8")
        self.assertIn("status_after = git_worktree(", source)
        self.assertIn('fail("canonical worktree changed during GitHub synchronization")', source)


class BackendDeploymentBaselineObserverAssetTests(unittest.TestCase):
    APPROVED_PATHS = {
        "/opt/remihub",
        "/opt/remihub-agent/repositories/remihub-planning",
        "/opt/remihub-agent/repositories/remihub-implementation.git",
        "/opt/remihub-agent/deployment/qa/repository.git",
        "/opt/remihub-agent/deployment/production/repository.git",
        "/var/lib/remihub-agent/github-sync/backend/latest-result.json",
        "/var/lib/remihub-agent/health-observations/backend/deployment-baseline.json",
    }

    def test_observer_uses_fixed_approved_paths_and_refs_only(self):
        source = BASELINE_OBSERVER.read_text(encoding="utf-8")
        for path in self.APPROVED_PATHS:
            self.assertIn(path, source)
        self.assertIn('ref="refs/heads/main"', source)
        self.assertIn('ref="refs/heads/qa-main"', source)
        self.assertIn('ref="refs/heads/production-main"', source)
        self.assertIn('"expected_branch": "qa-runtime"', source)
        self.assertNotIn("argparse", source)
        self.assertNotIn("input(", source)
        self.assertIn("if len(sys.argv) != 1:", source)
        self.assertIn("accepts no arguments", source)

    def test_observer_git_commands_are_read_only(self):
        source = BASELINE_OBSERVER.read_text(encoding="utf-8")
        for forbidden in (
            '"fetch"',
            '"push"',
            '"reset"',
            '"checkout"',
            '"update-ref"',
            '"start"',
            '"restart"',
            '"systemctl"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn('"symbolic-ref"', source)
        self.assertIn('"rev-parse"', source)
        self.assertIn('"GIT_OPTIONAL_LOCKS": "0"', source)

    def test_observer_subprocesses_do_not_use_shell(self):
        tree = ast.parse(BASELINE_OBSERVER.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "run":
                continue
            for keyword in node.keywords:
                self.assertNotEqual(keyword.arg, "shell")

    def test_qa_runtime_observation_boundary_accepts_no_arbitrary_ref(self):
        source = DEPLOYMENT_CONTROL.read_text(encoding="utf-8")
        self.assertIn('if action == "observe-runtime":', source)
        self.assertIn('fail("observe-runtime accepts no additional arguments")', source)
        self.assertIn('"HEAD^{commit}"', source)
        self.assertIn('payload["status"] = "branch_mismatch"', source)

    def test_new_assets_do_not_broaden_collector_permissions(self):
        service = (
            ROOT
            / "deployments"
            / "agent_backend"
            / "systemd"
            / "remihub-agent-deployment-baseline-observer.service"
        ).read_text(encoding="utf-8")
        installer = (
            ROOT / "deployments" / "agent_backend" / "install-package.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn("remihub-health-collector.service.d", installer)
        self.assertNotIn("usermod -aG remihub-agent alex", installer)
        self.assertNotIn("usermod -aG remihub-deployer alex", installer)
        self.assertNotIn("usermod -aG remihub-qa-app alex", installer)
        self.assertNotIn("usermod -aG remihub-github-sync alex", installer)
        self.assertNotIn("setfacl", installer)
        self.assertNotIn("chmod -R", service)
        self.assertIn("ReadWritePaths=/var/lib/remihub-agent/health-observations/backend", service)
        self.assertIn("ReadOnlyPaths=/opt/remihub-agent/deployment/qa/application", service)

    def test_observer_service_documents_required_setid_exception(self):
        service = (
            ROOT
            / "deployments"
            / "agent_backend"
            / "systemd"
            / "remihub-agent-deployment-baseline-observer.service"
        ).read_text(encoding="utf-8")
        self.assertIn("runuser for read-only probes", service)
        self.assertIn("NoNewPrivileges=false", service)
        self.assertNotIn("NoNewPrivileges=true", service)
        self.assertIn("CapabilityBoundingSet=CAP_SETUID CAP_SETGID", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", service)

    def test_observer_does_not_repair_observation_directory(self):
        source = BASELINE_OBSERVER.read_text(encoding="utf-8")
        self.assertIn("path.parent.lstat()", source)
        self.assertIn("observation directory ownership or mode is unsafe", source)
        self.assertNotIn("path.parent.mkdir", source)
        self.assertNotIn("os.chown(path.parent", source)
        self.assertNotIn("os.chmod(path.parent", source)
        self.assertIn("os.fchmod(fd, 0o640)", source)
        self.assertIn("os.fchown(fd, 0, storage_gid)", source)


if __name__ == "__main__":
    unittest.main()
