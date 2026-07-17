from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from dataclasses import dataclass

from backend.core.agent_worker import (
    AgentWorker,
    AgentWorkerConfigurationError,
    FakeAgentExecutor,
)
from backend.services.agent_worker_service import DatabaseAgentQueue


logger = logging.getLogger("remihub.agent_worker")


def _positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    configured = os.environ.get(name, str(default)).strip()
    try:
        value = int(configured)
    except ValueError as exc:
        raise AgentWorkerConfigurationError(f"{name} must be an integer") from exc

    if value < minimum:
        raise AgentWorkerConfigurationError(f"{name} must be at least {minimum}")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    configured = os.environ.get(name)
    if configured is None:
        return default

    normalized = configured.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AgentWorkerConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True)
class AgentWorkerSettings:
    environment: str
    executor_name: str
    worker_id: str
    poll_seconds: int
    lease_seconds: int
    max_attempts: int
    run_once: bool
    allow_fake_executor: bool

    @classmethod
    def from_environment(cls) -> "AgentWorkerSettings":
        environment = (
            os.environ.get(
                "REMIHUB_AGENT_ENVIRONMENT",
                "production",
            )
            .strip()
            .lower()
        )
        if environment not in {"qa", "production"}:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_ENVIRONMENT must be qa or production"
            )

        executor_name = (
            os.environ.get(
                "REMIHUB_AGENT_EXECUTOR",
                "disabled",
            )
            .strip()
            .lower()
        )
        worker_id = os.environ.get(
            "REMIHUB_AGENT_WORKER_ID",
            f"{socket.gethostname()}:{os.getpid()}",
        ).strip()

        if not worker_id:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_WORKER_ID must not be blank"
            )
        if len(worker_id) > 200:
            raise AgentWorkerConfigurationError(
                "REMIHUB_AGENT_WORKER_ID must be at most 200 characters"
            )

        return cls(
            environment=environment,
            executor_name=executor_name,
            worker_id=worker_id,
            poll_seconds=_positive_int("REMIHUB_AGENT_POLL_SECONDS", 5),
            lease_seconds=_positive_int(
                "REMIHUB_AGENT_LEASE_SECONDS",
                120,
                minimum=5,
            ),
            max_attempts=_positive_int("REMIHUB_AGENT_MAX_ATTEMPTS", 3),
            run_once=_boolean("REMIHUB_AGENT_RUN_ONCE"),
            allow_fake_executor=_boolean("REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR"),
        )


def build_executor(settings: AgentWorkerSettings):
    if settings.executor_name == "disabled":
        raise AgentWorkerConfigurationError(
            "Agent execution is disabled; configure REMIHUB_AGENT_EXECUTOR"
        )

    if settings.executor_name == "fake":
        if settings.environment != "qa":
            raise AgentWorkerConfigurationError(
                "The fake agent executor is restricted to QA"
            )
        if not settings.allow_fake_executor:
            raise AgentWorkerConfigurationError(
                "The fake executor requires REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR=true"
            )
        return FakeAgentExecutor()

    raise AgentWorkerConfigurationError(
        f"Unknown agent executor: {settings.executor_name!r}"
    )


def run_worker(settings: AgentWorkerSettings) -> None:
    executor = build_executor(settings)
    queue = DatabaseAgentQueue(environment=settings.environment)
    identity = queue.verify_identity()
    logger.info(
        "Agent worker database identity verified: database=%s role=%s",
        identity[0],
        identity[1],
    )

    worker = AgentWorker(
        queue=queue,
        executor=executor,
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        max_attempts=settings.max_attempts,
    )

    if settings.run_once:
        processed = worker.process_once()
        logger.info("Agent worker run-once complete: processed=%s", processed)
        return

    stop_event = threading.Event()

    def request_stop(signum, _frame):
        logger.info("Agent worker received signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info(
        "Agent worker started: worker=%s executor=%s environment=%s",
        settings.worker_id,
        settings.executor_name,
        settings.environment,
    )

    while not stop_event.is_set():
        processed = worker.process_once()
        if not processed:
            stop_event.wait(settings.poll_seconds)

    logger.info("Agent worker stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = AgentWorkerSettings.from_environment()
    run_worker(settings)


if __name__ == "__main__":
    main()
