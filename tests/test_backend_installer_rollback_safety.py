from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deployments/agent_backend/install-package.sh"
README = ROOT / "deployments/agent_backend/README.md"


class BackendInstallerRollbackSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = INSTALLER.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")

    def test_broad_root_restore_is_permanently_forbidden(self) -> None:
        text = self.installer
        self.assertNotIn('cp -a "$BACKUP/system-root/." /', text)
        self.assertNotIn("system-root", text)
        self.assertNotRegex(text, r"\bcp\s+-a\b")
        self.assertNotIn("tar -C / -xf", text)
        self.assertNotIn("tar -C / -cf", text)

    def test_system_rollback_is_explicit_leaf_only(self) -> None:
        text = self.installer
        self.assertIn('mkdir -p "$BACKUP/system-leaves"', text)
        self.assertIn('tar -C "$parent" -cpf "$archive" -- "$base"', text)
        self.assertIn('tar -C "$parent" -xpf "$archive" -- "$base"', text)
        self.assertIn('printf \'%03d\\tpresent\\t%s\\n\'', text)
        self.assertIn('printf \'%03d\\tabsent\\t%s\\n\'', text)
        self.assertIn('[[ "$manifest_index" == "$expected_index" && "$expected_path" == "$path" ]]', text)

    def test_uncaptured_existence_state_never_means_absent(self) -> None:
        text = self.installer
        for name in (
            "PROD_TARGET_EXISTED",
            "PROD_DEPLOYMENT_EXISTED",
            "QA_REPOSITORY_EXISTED",
            "QA_APPLICATION_EXISTED",
            "QA_PARENT_EXISTED",
            "CONFIG_PARENT_EXISTED",
        ):
            self.assertIn(f'{name}="UNRECORDED"', text)
        self.assertIn("ROLLBACK_STATE_CAPTURED=0", text)
        self.assertIn("ROLLBACK_STATE_CAPTURED=1", text)
        self.assertIn(
            "Rollback state was not fully captured; refusing deployment/system restore.",
            text,
        )

    def test_rollback_contains_no_recursive_permission_repair(self) -> None:
        text = self.installer
        start = text.index("  rollback() {")
        end = text.index("  trap rollback EXIT", start)
        rollback = text[start:end]
        self.assertNotIn("chown -R", rollback)
        self.assertNotIn("chmod -R", rollback)
        self.assertIn(
            "no parent permission repair was attempted",
            rollback,
        )

    def test_qa_and_production_archives_are_parent_bounded(self) -> None:
        text = self.installer
        self.assertIn(
            'tar -C /opt/remihub-agent/deployment/qa \\\n'
            '    -cpf "$BACKUP/qa-repository.tar" -- repository.git',
            text,
        )
        self.assertIn(
            'tar -C /opt/remihub-agent/deployment/qa \\\n'
            '      -cpf "$BACKUP/qa-application.tar" -- application',
            text,
        )
        self.assertIn(
            'tar -C /opt/remihub-agent/deployment \\\n'
            '      -cpf "$BACKUP/production-deployment.tar" -- production',
            text,
        )
        self.assertIn(
            'tar -C /opt/remihub-agent/deployment/qa \\\n'
            '            -xpf "$BACKUP/qa-repository.tar" -- repository.git',
            text,
        )
        self.assertIn(
            'tar -C /opt/remihub-agent/deployment \\\n'
            '            -xpf "$BACKUP/production-deployment.tar" -- production',
            text,
        )

    def test_critical_parent_identity_is_captured_and_reverified(self) -> None:
        text = self.installer
        self.assertIn("CRITICAL_PARENTS=(", text)
        for path in ("/", "/usr", "/etc", "/opt", "/var", "/home", "/dev"):
            self.assertRegex(
                text,
                rf"(?m)^\s+{re.escape(path)}\s*$",
            )
        self.assertIn("capture_critical_parent_state", text)
        self.assertIn("verify_critical_parent_state", text)
        self.assertIn('[[ -c /dev/null && ! -L /dev/null ]]', text)
        self.assertIn('echo "CRITICAL_PARENT_POSTCHECK=PASS"', text)

    def test_existing_deployment_tree_is_not_recursively_reowned(self) -> None:
        text = self.installer
        self.assertNotIn(
            "chown -R remihub-deployer:remihub-agent \\\n"
            "    /opt/remihub-agent/deployment/qa/repository.git",
            text,
        )
        self.assertNotIn(
            "chown -R remihub-deployer:remihub-agent "
            "/opt/remihub-agent/deployment/production/repository.git",
            text,
        )

    def test_project_readme_carries_nonnegotiable_permission_rule(self) -> None:
        readme = self.readme
        self.assertIn(
            "CRITICAL SERVER-WIDE PERMISSION SAFETY RULE — NON-NEGOTIABLE",
            readme,
        )
        self.assertIn(
            "There will not be a third server-wide permissions incident.",
            readme,
        )
        self.assertIn(
            'cp -a "$BACKUP/system-root/." /',
            readme,
        )
        self.assertIn(
            "rollback MUST FAIL CLOSED",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
