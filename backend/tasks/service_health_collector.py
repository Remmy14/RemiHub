from __future__ import annotations

import logging
import sys

from backend.models.health_models import ServiceHealthSnapshotResponse
from backend.services import service_health_service


logger = logging.getLogger("remihub.service_health_collector")


def collect_snapshot() -> ServiceHealthSnapshotResponse:
    snapshot = service_health_service.collect_service_health_snapshot()
    return ServiceHealthSnapshotResponse.model_validate(snapshot)


def persist_snapshot(snapshot: ServiceHealthSnapshotResponse) -> None:
    service_health_service.persist_service_health_snapshot(snapshot)


def run_collection_once() -> ServiceHealthSnapshotResponse:
    snapshot = collect_snapshot()
    persist_snapshot(snapshot)
    return snapshot


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        snapshot = run_collection_once()
    except Exception:
        logger.exception("Service health collection failed")
        return 1

    logger.info(
        "Persisted service health snapshot checked_at=%s overall=%s components=%s",
        snapshot.checked_at.isoformat(),
        snapshot.overall.value,
        len(snapshot.components),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
