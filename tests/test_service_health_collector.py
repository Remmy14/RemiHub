import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.models.health_models import (
    HealthComponent,
    HealthComponentGroup,
    HealthComponentKind,
    HealthStatus,
    ServiceHealthSnapshotResponse,
)
from backend.services import service_health_service
from backend.tasks import service_health_collector


def snapshot() -> ServiceHealthSnapshotResponse:
    checked_at = datetime.now(timezone.utc)
    return ServiceHealthSnapshotResponse(
        success=True,
        checked_at=checked_at,
        overall=HealthStatus.HEALTHY,
        components=[
            HealthComponent(
                id="remihub",
                name="RemiHub Backend",
                group=HealthComponentGroup.CORE,
                kind=HealthComponentKind.SYSTEMD_UNIT,
                status=HealthStatus.HEALTHY,
                message="ok",
                checked_at=checked_at,
            )
        ],
    )


class ServiceHealthCollectorTests(unittest.TestCase):
    @patch("backend.tasks.service_health_collector.persist_snapshot")
    @patch("backend.tasks.service_health_collector.collect_snapshot")
    def test_run_collection_once_collects_and_persists_snapshot(self, collect, persist):
        expected = snapshot()
        collect.return_value = expected

        result = service_health_collector.run_collection_once()

        self.assertEqual(result, expected)
        persist.assert_called_once_with(expected)

    @patch("backend.tasks.service_health_collector.run_collection_once")
    def test_main_returns_zero_after_successful_persistence(self, run_once):
        run_once.return_value = snapshot()

        self.assertEqual(service_health_collector.main(), 0)

    @patch("backend.tasks.service_health_collector.run_collection_once")
    def test_main_returns_nonzero_on_collection_or_persistence_failure(self, run_once):
        run_once.side_effect = RuntimeError("failed")

        self.assertEqual(service_health_collector.main(), 1)

    def test_collector_module_does_not_call_http_or_mutate_services(self):
        source = Path(service_health_collector.__file__).read_text(encoding="utf-8")

        forbidden_fragments = (
            "requests",
            "httpx",
            "urlopen",
            "systemctl start",
            "systemctl stop",
            "systemctl restart",
            "subprocess",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    def test_health_collector_does_not_directly_open_protected_baseline_sources(self):
        source = Path(service_health_service.__file__).read_text(encoding="utf-8")

        forbidden_fragments = (
            "/opt/remihub-agent/repositories/remihub-implementation.git",
            "/opt/remihub-agent/deployment/qa/repository.git",
            "/opt/remihub-agent/deployment/qa/application",
            "/var/lib/remihub-agent/github-sync/backend/latest-result.json",
            "--git-dir",
            "rev-parse",
            "symbolic-ref",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    def test_fastapi_lifespan_does_not_launch_collector(self):
        main_source = (
            Path(__file__).resolve().parents[1] / "backend" / "main.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("service_health_collector", main_source)
        self.assertNotIn("run_collection_once", main_source)


if __name__ == "__main__":
    unittest.main()
