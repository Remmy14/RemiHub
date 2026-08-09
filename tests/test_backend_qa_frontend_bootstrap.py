from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deployments/agent_backend/install-package.sh"
QA_VERIFY = ROOT / "deployments/agent_backend/qa-verify.sh"
README = ROOT / "deployments/agent_backend/README.md"


class BackendQaFrontendBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = INSTALLER.read_text(encoding="utf-8")
        cls.qa_verify = QA_VERIFY.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")

    def test_fresh_qa_git_runtime_must_not_smuggle_generated_dist(self) -> None:
        self.assertIn(
            '[[ ! -e "$qa_runtime/frontend-web/dist" && \\\n'
            '       ! -L "$qa_runtime/frontend-web/dist" ]]',
            self.installer,
        )
        self.assertIn(
            "Fresh QA Git runtime unexpectedly already contains frontend dist.",
            self.installer,
        )

    def test_bootstrap_prepares_exact_candidate_lockfile_through_protected_boundary(self) -> None:
        self.assertIn(
            '/usr/local/libexec/remihub-backend-deployment-control \\\n'
            '      frontend-prepare qa \\\n'
            '      "$QA_FRONTEND_BOOTSTRAP_WORKTREE" \\\n'
            '      "$NEW_COMMIT" \\\n'
            '      "$candidate_tree"',
            self.installer,
        )
        self.assertIn(
            'git -C "$QA_FRONTEND_BOOTSTRAP_WORKTREE" rev-parse \'HEAD^{tree}\'',
            self.installer,
        )

    def test_bootstrap_reuses_deterministic_two_build_artifact_builder(self) -> None:
        self.assertIn(
            "from backend.core.agent_deployment import LocalFrontendArtifactBuilder",
            self.installer,
        )
        self.assertIn(
            'LocalFrontendArtifactBuilder(\n    timeout_seconds=900,\n    environment="qa",\n).build(',
            self.installer,
        )
        self.assertIn(
            'changed_files=("frontend-web/package.json",)',
            self.installer,
        )
        self.assertIn(
            'if not result.reproducibility.get("matched"):',
            self.installer,
        )

    def test_prepare_build_install_verify_all_precede_qa_route_verification(self) -> None:
        prepare = self.installer.index("frontend-prepare qa")
        build = self.installer.index("LocalFrontendArtifactBuilder")
        install = self.installer.index("frontend-install qa")
        verify = self.installer.index("frontend-verify qa")
        bootstrap_call = self.installer.index("  bootstrap_qa_frontend\n")
        qa_stage = self.installer.index(
            'echo "[7/10] Run complete QA validation before production promotion"'
        )
        qa_verify_call = self.installer.index(
            '"$RELEASE/deployments/agent_backend/qa-verify.sh"',
            qa_stage,
        )
        self.assertLess(prepare, build)
        self.assertLess(build, install)
        self.assertLess(install, verify)
        self.assertLess(bootstrap_call, qa_verify_call)

    def test_installed_index_must_be_real_and_readable_by_qa_app(self) -> None:
        self.assertIn(
            '[[ -f "$qa_runtime/frontend-web/dist/index.html" && \\\n'
            '       ! -L "$qa_runtime/frontend-web/dist/index.html" ]]',
            self.installer,
        )
        self.assertIn(
            'runuser -u remihub-qa-app -- \\\n'
            '      /usr/bin/test -r "$qa_runtime/frontend-web/dist/index.html"',
            self.installer,
        )

    def test_bootstrap_cleanup_is_one_exact_worktree_only(self) -> None:
        self.assertIn(
            'local expected="/opt/remihub-agent/deployment/qa/worktrees/'
            'card-${QA_FRONTEND_BOOTSTRAP_CARD}-r1"',
            self.installer,
        )
        self.assertIn(
            '[[ "$QA_FRONTEND_BOOTSTRAP_WORKTREE" == "$expected" ]]',
            self.installer,
        )
        self.assertIn(
            "QA frontend bootstrap worktree identity drifted; refusing broad cleanup.",
            self.installer,
        )
        self.assertIn(
            "Exact QA frontend bootstrap worktree cleanup failed; no broad cleanup was attempted.",
            self.installer,
        )

    def test_qa_frontend_route_verifier_remains_strict(self) -> None:
        self.assertIn("verify_qa_frontend_routes", self.qa_verify)
        for route in ("/race", "/race/draft", "/storage"):
            self.assertIn(
                f"curl -fsS http://127.0.0.1:8001{route}",
                self.qa_verify,
            )
        self.assertIn(
            'wait_for_qa_health "$RECORD/qa-openapi.json" "candidate-health"',
            self.qa_verify,
        )
        self.assertIn(
            'verify_qa_frontend_routes "$RECORD/frontend-routes"',
            self.qa_verify,
        )

    def test_readme_propagates_bootstrap_and_no_third_incident_rule(self) -> None:
        self.assertIn(
            "CRITICAL SERVER-WIDE PERMISSION SAFETY RULE — NON-NEGOTIABLE",
            self.readme,
        )
        self.assertIn(
            "There will not be a third server-wide permissions incident.",
            self.readme,
        )
        self.assertIn("Fresh QA runtime frontend bootstrap", self.readme)
        self.assertIn("frontend-prepare", self.readme)
        self.assertIn("LocalFrontendArtifactBuilder", self.readme)
        self.assertIn("frontend-install", self.readme)
        self.assertIn("frontend-verify", self.readme)


if __name__ == "__main__":
    unittest.main()
