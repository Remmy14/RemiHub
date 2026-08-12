#!/usr/bin/env bash
(
  set -euo pipefail

  [[ "${EUID}" -eq 0 ]] || { echo "qa-verify.sh must run as root" >&2; exit 1; }
  RELEASE="${1:?usage: qa-verify.sh /absolute/release/path [record-stamp]}"
  REQUESTED_STAMP="${2:-}"
  PG_DUMP="${3:?usage: qa-verify.sh /absolute/release/path [record-stamp] /absolute/pg_dump /absolute/pg_restore}"
  PG_RESTORE="${4:?usage: qa-verify.sh /absolute/release/path [record-stamp] /absolute/pg_dump /absolute/pg_restore}"
  [[ "$RELEASE" == /* && -d "$RELEASE" ]] || { echo "invalid release path" >&2; exit 1; }
  [[ "$PG_DUMP" == /* && -x "$PG_DUMP" ]] || { echo "invalid pg_dump path" >&2; exit 1; }
  [[ "$PG_RESTORE" == /* && -x "$PG_RESTORE" ]] || { echo "invalid pg_restore path" >&2; exit 1; }
  if [[ -n "$REQUESTED_STAMP" && ! "$REQUESTED_STAMP" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    echo "invalid verification record stamp" >&2
    exit 1
  fi

  synthetic_health_card_from_stamp() {
    local stamp="${1:?verification stamp required}"
    local digest

    digest="$(printf '%s' "$stamp" | sha256sum)"
    digest="${digest%% *}"
    printf '%s-%s-4%s-8%s-%s\n' \
      "${digest:0:8}" \
      "${digest:8:4}" \
      "${digest:13:3}" \
      "${digest:17:3}" \
      "${digest:20:12}"
  }

  TARGET="/opt/remihub-agent/deployment/qa/repository.git"
  WORKTREES="/opt/remihub-agent/deployment/qa/worktrees"
  ARTIFACTS="/opt/remihub-agent/deployment/qa/artifacts"
  VALIDATOR="/usr/local/libexec/remihub-backend-validation-sandbox"
  HELPER="/usr/local/libexec/remihub-backend-deployment-control"
  PYTHON="/opt/remihub-agent/runtime/venv/bin/python"
  BASE="$(runuser -u remihub-deployer -- git --git-dir="$TARGET" rev-parse qa-main^{commit})"
  STAMP="${REQUESTED_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
  RECORD="$ARTIFACTS/install-verification-$STAMP"
  VALIDATION_CARD="11111111-1111-4111-8111-111111111111"
  HEALTH_CARD="$(synthetic_health_card_from_stamp "$STAMP")"
  VALIDATION_WORKTREE="$WORKTREES/card-$VALIDATION_CARD-r1"
  HEALTH_WORKTREE="$WORKTREES/card-$HEALTH_CARD-r1"
  HEALTH_BRANCH="deployment/card-$HEALTH_CARD/r1"
  ROLLBACK_REF="rollback-before-agent-card-$HEALTH_CARD-r1"
  DROPIN="/run/systemd/system/remihub-backend-qa.service.d/99-remihub-health-failure.conf"
  PROMOTED=0

  mkdir -p "$RECORD"
  chown remihub-deployer:remihub-agent "$RECORD"
  chmod 0750 "$RECORD"

  capture_qa_diagnostics() {
    local label="${1:?diagnostic label required}"
    systemctl status remihub-backend-qa.service --no-pager --full \
      >"$RECORD/$label-service-status.log" 2>&1 || true
    journalctl -u remihub-backend-qa.service -n 200 --no-pager \
      >"$RECORD/$label-service-journal.log" 2>&1 || true
    printf '\nQA service status (%s):\n' "$label" >&2
    cat "$RECORD/$label-service-status.log" >&2
    printf '\nQA service journal (%s):\n' "$label" >&2
    cat "$RECORD/$label-service-journal.log" >&2
  }

  wait_for_qa_health() {
    local output="${1:?health output path required}"
    local label="${2:?health label required}"
    local timeout_seconds="${3:-90}"
    local active_state
    local deadline
    local remaining
    local curl_timeout

    systemctl reset-failed remihub-backend-qa.service >/dev/null 2>&1 || true
    systemctl start remihub-backend-qa.service
    deadline=$((SECONDS + timeout_seconds))
    while (( SECONDS < deadline )); do
      remaining=$((deadline - SECONDS))
      if (( remaining < 1 )); then
        break
      fi
      curl_timeout="$remaining"
      if (( curl_timeout > 5 )); then
        curl_timeout=5
      fi
      if curl -fsS \
        --connect-timeout 1 \
        --max-time "$curl_timeout" \
        http://127.0.0.1:8001/openapi.json >"$output"; then
        return 0
      fi
      active_state="$(systemctl show remihub-backend-qa.service --property=ActiveState --value 2>/dev/null || true)"
      if [[ "$active_state" == "failed" || "$active_state" == "inactive" ]]; then
        break
      fi
      remaining=$((deadline - SECONDS))
      if (( remaining > 1 )); then
        sleep 1
      else
        break
      fi
    done
    capture_qa_diagnostics "$label"
    return 1
  }

  verify_qa_frontend_routes() {
    local output_dir="${1:?frontend output path required}"
    mkdir -p "$output_dir"
    curl -fsS http://127.0.0.1:8001/race >"$output_dir/race.html"
    curl -fsS http://127.0.0.1:8001/race/draft >"$output_dir/race-draft.html"
    curl -fsS http://127.0.0.1:8001/storage >"$output_dir/storage.html"
    if grep -R "frontend-web/dist" "$output_dir" >/dev/null 2>&1; then
      echo "QA frontend route served stale dist path text" >&2
      return 1
    fi
  }

  cleanup() {
    set +e
    systemctl stop remihub-backend-qa.service >/dev/null 2>&1
    if [[ "$PROMOTED" -eq 1 ]]; then
      "$HELPER" restore qa "$(git -C /opt/remihub-agent/deployment/qa/application rev-parse HEAD)" "$BASE" >/dev/null 2>&1
    fi
    rm -f "$DROPIN"
    rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
    systemctl daemon-reload >/dev/null 2>&1
    runuser -u remihub-deployer -- git --git-dir="$TARGET" worktree remove --force "$VALIDATION_WORKTREE" >/dev/null 2>&1
    runuser -u remihub-deployer -- git --git-dir="$TARGET" worktree remove --force "$HEALTH_WORKTREE" >/dev/null 2>&1
    runuser -u remihub-deployer -- git --git-dir="$TARGET" branch -D "$HEALTH_BRANCH" >/dev/null 2>&1
    runuser -u remihub-deployer -- git --git-dir="$TARGET" tag -d "$ROLLBACK_REF" >/dev/null 2>&1
  }
  trap cleanup EXIT

  echo "[1/4] isolated compile and full backend tests"
  rm -rf "$VALIDATION_WORKTREE"
  runuser -u remihub-deployer -- git --git-dir="$TARGET" worktree add --detach "$VALIDATION_WORKTREE" "$BASE" >/dev/null
  runuser -u remihub-deployer -- "$VALIDATOR" "$VALIDATION_WORKTREE" \
    >"$RECORD/isolated-validation.log" 2>&1
  runuser -u remihub-deployer -- git --git-dir="$TARGET" worktree remove --force "$VALIDATION_WORKTREE" >/dev/null

  echo "[2/4] QA PostgreSQL backup, reversible migration, and partial-failure rollback"
  if ! PYTHONPATH="$RELEASE" "$PYTHON" - \
    "$RELEASE" "$RECORD" "$PG_DUMP" "$PG_RESTORE" \
    >"$RECORD/database-verification.log" 2>&1 <<'PY'
from pathlib import Path
import shutil
import sys

from backend.core.agent_deployment import PostgresDeploymentDatabase
from backend.database import migration_runner

release = Path(sys.argv[1])
record = Path(sys.argv[2])
pg_dump = sys.argv[3]
pg_restore = sys.argv[4]
source = release / "backend" / "database" / "migrations"
work = record / "migrations"
shutil.copytree(source, work)

database = PostgresDeploymentDatabase(
    config_path="/opt/remihub-agent/deployment/config/qa-migrator.ini",
    backup_root="/var/backups/remihub-agent/backend-deployments/qa",
    owner_role="remihub_qa_owner",
    pg_dump_binary=pg_dump,
    pg_restore_binary=pg_restore,
)
base_pending = database.pending_versions(source)
if base_pending:
    raise RuntimeError(f"QA database has unexpected pending migrations: {base_pending!r}")
backup = database.backup(card_id="installer-verification", deployment_run_id=record.name)
print(f"backup={backup.path} sha256={backup.sha256} size={backup.size_bytes}")

existing = {migration.version for migration in migration_runner.discover_migrations(work)}
available = [f"{number:04d}" for number in range(9001, 9901) if f"{number:04d}" not in existing]
if len(available) < 3:
    raise RuntimeError("unable to reserve QA smoke migration versions")
success, partial, failure = available[-3:]

success_table = f"remihub_agent_deployment_smoke_{success}"
(work / f"{success}_installer_success.up.sql").write_text(
    f"CREATE TABLE public.{success_table} (id integer PRIMARY KEY);\n", encoding="utf-8"
)
(work / f"{success}_installer_success.down.sql").write_text(
    f"DROP TABLE public.{success_table};\n", encoding="utf-8"
)
assert database.pending_versions(work) == (success,)
assert database.upgrade(work, (success,)) == (success,)
assert database.downgrade(work, (success,)) == (success,)
for direction in ("up", "down"):
    (work / f"{success}_installer_success.{direction}.sql").unlink()
print("reversible_success=passed")

partial_table = f"remihub_agent_deployment_smoke_{partial}"
(work / f"{partial}_installer_partial.up.sql").write_text(
    f"CREATE TABLE public.{partial_table} (id integer PRIMARY KEY);\n", encoding="utf-8"
)
(work / f"{partial}_installer_partial.down.sql").write_text(
    f"DROP TABLE public.{partial_table};\n", encoding="utf-8"
)
(work / f"{failure}_installer_failure.up.sql").write_text(
    "ALTER TABLE public.__remihub_intentionally_missing ADD COLUMN value integer;\n",
    encoding="utf-8",
)
(work / f"{failure}_installer_failure.down.sql").write_text("SELECT 1;\n", encoding="utf-8")
expected = tuple(sorted((partial, failure)))
try:
    database.upgrade(work, expected)
except Exception as exc:
    pending = database.pending_versions(work)
    applied = tuple(version for version in expected if version not in pending)
    if applied != (partial,):
        raise RuntimeError(f"unexpected partial migration state: applied={applied!r} pending={pending!r}") from exc
    assert database.downgrade(work, applied) == (partial,)
    print(f"partial_failure_rollback=passed original={type(exc).__name__}")
else:
    raise RuntimeError("intentional failed migration unexpectedly succeeded")
PY
  then
    cat "$RECORD/database-verification.log" >&2
    exit 1
  fi
  cat "$RECORD/database-verification.log"

  echo "[3/4] QA candidate runtime health"
  wait_for_qa_health "$RECORD/qa-openapi.json" "candidate-health"
  verify_qa_frontend_routes "$RECORD/frontend-routes"
  systemctl stop remihub-backend-qa.service
  "$HELPER" verify-runtime qa "$BASE" \
    >"$RECORD/candidate-runtime-state.txt"

  echo "[4/4] forced QA health failure and deterministic code restore"
  rm -rf "$HEALTH_WORKTREE"
  runuser -u remihub-deployer -- git --git-dir="$TARGET" worktree add -b "$HEALTH_BRANCH" "$HEALTH_WORKTREE" "$BASE" >/dev/null
  python3 - "$HEALTH_WORKTREE/backend/tasks/weather_monitor.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
if "import os\n" not in text:
    text = text.replace("import logging\n", "import logging\nimport os\n", 1)
marker = 'if os.environ.get("REMIHUB_QA_FORCE_HEALTH_FAILURE") == "1":\n    raise RuntimeError("intentional QA health verification failure")\n\n'
text = text.replace("# Local Imports\n", marker + "# Local Imports\n", 1)
path.write_text(text, encoding="utf-8")
PY
  chown remihub-deployer:remihub-agent "$HEALTH_WORKTREE/backend/tasks/weather_monitor.py"
  runuser -u remihub-deployer -- git -C "$HEALTH_WORKTREE" add backend/tasks/weather_monitor.py
  runuser -u remihub-deployer -- git -C "$HEALTH_WORKTREE" \
    -c user.name='RemiHub QA Verification' \
    -c user.email='remihub-qa-verification@invalid.local' \
    commit -m 'QA verification intentional health failure' >/dev/null
  CANDIDATE="$(runuser -u remihub-deployer -- git -C "$HEALTH_WORKTREE" rev-parse HEAD)"
  runuser -u remihub-deployer -- git --git-dir="$TARGET" tag -f "$ROLLBACK_REF" "$BASE"
  "$HELPER" promote qa "$HEALTH_BRANCH" "$CANDIDATE" "$BASE" "$ROLLBACK_REF"
  PROMOTED=1
  mkdir -p "$(dirname "$DROPIN")"
  cat >"$DROPIN" <<'EOF'
[Service]
Environment=REMIHUB_QA_FORCE_HEALTH_FAILURE=1
EOF
  systemctl daemon-reload
  systemctl reset-failed remihub-backend-qa.service >/dev/null 2>&1 || true
  systemctl start remihub-backend-qa.service >/dev/null 2>&1 || true
  INTENTIONAL_HEALTHY=0
  for _ in $(seq 1 10); do
    if curl -fsS http://127.0.0.1:8001/openapi.json \
      >"$RECORD/intentional-failure-openapi.json" 2>/dev/null; then
      INTENTIONAL_HEALTHY=1
      break
    fi
    if [[ "$(systemctl is-active remihub-backend-qa.service || true)" != "active" ]]; then
      break
    fi
    sleep 1
  done
  capture_qa_diagnostics "intentional-health-failure"
  if [[ "$INTENTIONAL_HEALTHY" -eq 1 ]]; then
    echo "intentional QA health failure unexpectedly became healthy" >&2
    exit 1
  fi
  [[ "$(systemctl is-active remihub-backend-qa.service || true)" != "active" ]]
  "$HELPER" restore qa "$CANDIDATE" "$BASE"
  "$HELPER" verify-runtime qa "$BASE" \
    >"$RECORD/first-restore-runtime-state.txt"
  PROMOTED=0
  rm -f "$DROPIN"
  rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
  systemctl daemon-reload

  echo "      retrying the same immutable candidate after safe restore"
  "$HELPER" promote qa "$HEALTH_BRANCH" "$CANDIDATE" "$BASE" "$ROLLBACK_REF"
  PROMOTED=1
  wait_for_qa_health "$RECORD/retry-openapi.json" "retry-health"
  systemctl stop remihub-backend-qa.service
  "$HELPER" verify-runtime qa "$CANDIDATE" \
    >"$RECORD/retry-runtime-state.txt"
  "$HELPER" restore qa "$CANDIDATE" "$BASE"
  "$HELPER" verify-runtime qa "$BASE" \
    >"$RECORD/final-restore-runtime-state.txt"
  PROMOTED=0
  printf '%s\n' "$CANDIDATE" >"$RECORD/retried-candidate.txt"

  wait_for_qa_health "$RECORD/restored-openapi.json" "restored-health"
  systemctl stop remihub-backend-qa.service
  "$HELPER" verify-runtime qa "$BASE" \
    >"$RECORD/restored-health-runtime-state.txt"
  printf '%s\n' "$BASE" >"$RECORD/restored-commit.txt"
  printf 'QA backend deployment verification passed: %s\n' "$RECORD"
)
