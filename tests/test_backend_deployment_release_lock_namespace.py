from pathlib import Path
import unittest


class BackendDeploymentReleaseLockNamespaceTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    LOCK_DIRECTORY = Path(
        "/var/lib/remihub-agent/android-release"
    )
    UNIT_TEMPLATES = (
        "remihub-agent-deployment-qa.service.in",
        "remihub-agent-deployment-production.service.in",
    )

    def _template_text(self, name: str) -> str:
        return (
            self.ROOT
            / "deployments"
            / "agent_backend"
            / "systemd"
            / name
        ).read_text(encoding="utf-8")

    def test_backend_workers_expose_exact_shared_lock_directory(self):
        expected = f"ReadWritePaths={self.LOCK_DIRECTORY}"
        for name in self.UNIT_TEMPLATES:
            with self.subTest(unit=name):
                text = self._template_text(name)
                self.assertEqual(text.count(expected), 1)
                self.assertIn("ProtectSystem=strict", text)

    def test_backend_workers_do_not_expose_broader_var_lib_paths(self):
        expected = f"ReadWritePaths={self.LOCK_DIRECTORY}"
        for name in self.UNIT_TEMPLATES:
            with self.subTest(unit=name):
                entries = [
                    line.removeprefix("ReadWritePaths=").strip()
                    for line in self._template_text(name).splitlines()
                    if line.startswith("ReadWritePaths=")
                ]
                var_lib_entries = [
                    entry
                    for entry in entries
                    if entry == "/var/lib"
                    or entry.startswith("/var/lib/")
                ]
                self.assertEqual(
                    var_lib_entries,
                    [str(self.LOCK_DIRECTORY)],
                )
                self.assertIn(expected, self._template_text(name))

    def test_installer_renders_both_reviewed_templates(self):
        installer = (
            self.ROOT
            / "deployments"
            / "agent_backend"
            / "install-package.sh"
        ).read_text(encoding="utf-8")
        for name in self.UNIT_TEMPLATES:
            with self.subTest(unit=name):
                self.assertIn(
                    f'$ASSETS/systemd/{name}',
                    installer,
                )


if __name__ == "__main__":
    unittest.main()
