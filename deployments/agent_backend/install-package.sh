#!/usr/bin/env bash
(
  set -euo pipefail

  EXPECTED_BASE="6fc38758b6d40d81173af625d0ed186afc582e76"
  PACKAGE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  PATCH_FILE="$PACKAGE_ROOT/repository.patch"
  ASSETS="$PACKAGE_ROOT/assets"
  PROD="/opt/remihub"
  SOURCE="/opt/remihub-agent/repositories/remihub-implementation.git"
  PLANNING="/opt/remihub-agent/repositories/remihub-planning"
  GIT_SAFE_CONFIG="/opt/remihub-agent/deployment/config/git-safe-directory.ini"
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  BACKUP="/var/backups/remihub-agent/backend-deployment-final-$STAMP"
  STAGING="/tmp/remihub-backend-deployment-final-$STAMP"
  INSTALL_BRANCH="install/backend-deployment-final-$STAMP"
  RELEASE=""
  NEW_COMMIT=""
  OLD_QA=""
  OLD_PROD_TARGET=""
  PROD_TARGET_EXISTED="UNRECORDED"
  PROD_DEPLOYMENT_EXISTED="UNRECORDED"
  QA_REPOSITORY_EXISTED="UNRECORDED"
  QA_APPLICATION_EXISTED="UNRECORDED"
  PROD_PROMOTED=0
  SOURCE_PROMOTED=0
  PLANNING_PROMOTED=0
  SYSTEM_BACKUP_CAPTURED=0
  SYSTEM_MUTATED=0
  ROLLBACK_STATE_CAPTURED=0
  QA_REINITIALIZED=0
  DEPLOYER_USER_CREATED=0
  DEPLOYER_GROUP_CREATED=0
  QA_USER_CREATED=0
  QA_GROUP_CREATED=0
  COMPLETE=0
  PG_DUMP_BINARY=""
  PG_RESTORE_BINARY=""
  DEPLOYMENT_PARENT_OWNER=""
  DEPLOYMENT_PARENT_MODE=""
  QA_PARENT_EXISTED="UNRECORDED"
  QA_PARENT_OWNER=""
  QA_PARENT_MODE=""
  CONFIG_PARENT_EXISTED="UNRECORDED"
  CONFIG_PARENT_OWNER=""
  CONFIG_PARENT_MODE=""
  QA_FRONTEND_BOOTSTRAP_CARD="33333333-3333-4333-8333-333333333333"
  QA_FRONTEND_BOOTSTRAP_RUN="44444444-4444-4444-8444-444444444444"
  QA_FRONTEND_BOOTSTRAP_APPROVAL="55555555-5555-4555-8555-555555555555"
  QA_FRONTEND_BOOTSTRAP_IMPLEMENTATION="66666666-6666-4666-8666-666666666666"
  QA_FRONTEND_BOOTSTRAP_WORKTREE=""
  QA_FRONTEND_BOOTSTRAP_ARTIFACT_ROOT=""

  required_commands=(
    git systemctl systemd-analyze systemd-run runuser install sha256sum tar sed curl visudo
    getent groupadd useradd usermod userdel groupdel find seq sudo python3 psql
    stat journalctl node npm
  )
  for command in "${required_commands[@]}"; do
    command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
  done
  [[ "$EUID" -eq 0 ]] || { echo "Run this installer with sudo." >&2; exit 1; }
  umask 0027
  [[ -f "$PATCH_FILE" && -d "$ASSETS" ]] || { echo "Package patch/assets are missing." >&2; exit 1; }

  resolve_postgresql_client() {
    local tool="${1:?tool required}"
    local psql_version major candidate

    psql_version="$(/usr/bin/psql --version)"
    if [[ ! "$psql_version" =~ PostgreSQL\)\ ([0-9]+)(\.[0-9]+)? ]]; then
      echo "Unable to determine the installed PostgreSQL client major version: $psql_version" >&2
      return 1
    fi
    major="${BASH_REMATCH[1]}"
    candidate="/usr/lib/postgresql/$major/bin/$tool"

    [[ -x "$candidate" ]] || {
      echo "Required versioned PostgreSQL client is missing: $candidate" >&2
      return 1
    }
    "$candidate" --version >/dev/null
    printf '%s\n' "$candidate"
  }

  PG_DUMP_BINARY="$(resolve_postgresql_client pg_dump)"
  PG_RESTORE_BINARY="$(resolve_postgresql_client pg_restore)"
  printf 'PostgreSQL backup clients: %s ; %s\n' \
    "$PG_DUMP_BINARY" "$PG_RESTORE_BINARY"
  [[ "$(node --version)" == "v22.22.2" ]] || { echo "Unsupported Node version" >&2; exit 1; }
  [[ "$(npm --version)" == "10.9.7" ]] || { echo "Unsupported npm version" >&2; exit 1; }

  mkdir -p "$BACKUP"
  chmod 0700 "$BACKUP"

  service_was_active() {
    systemctl is-active --quiet "$1"
  }
  declare -A ACTIVE=()
  for service in remihub.service remihub-agent-worker.service remihub-agent-implementation.service; do
    if service_was_active "$service"; then ACTIVE["$service"]=1; else ACTIVE["$service"]=0; fi
  done

  SYSTEM_PATHS=(
    /usr/local/libexec/remihub-backend-deployment-control
    /usr/local/libexec/remihub-backend-npm-cache-control
    /usr/local/libexec/remihub-backend-validation-sandbox
    /usr/local/libexec/remihub-backend-qa-server
    /usr/local/libexec/remihub-backend-validation-support
    /etc/sudoers.d/remihub-backend-deployment
    /etc/systemd/system/remihub-backend-qa.service
    /etc/systemd/system/remihub-agent-deployment-qa.service
    /etc/systemd/system/remihub-agent-deployment-production.service
    /opt/remihub-agent/deployment/config/qa-worker.ini
    /opt/remihub-agent/deployment/config/prod-worker.ini
    /opt/remihub-agent/deployment/config/qa-migrator.ini
    /opt/remihub-agent/deployment/config/prod-migrator.ini
    /opt/remihub-agent/deployment/config/qa-app.ini
    /opt/remihub-agent/deployment/config/qa-application.ini
    /opt/remihub-agent/deployment/config/git-safe-directory.ini
    /opt/remihub-agent/deployment/config/frontend-web-policy.json
    /var/cache/remihub-agent/npm
    /var/lib/remihub-agent/npm-prep
  )

  CRITICAL_PARENTS=(
    /
    /usr
    /etc
    /opt
    /var
    /home
    /dev
    /usr/local
    /usr/local/libexec
    /etc/systemd
    /etc/systemd/system
    /opt/remihub-agent
    /opt/remihub-agent/deployment
    /var/cache
    /var/lib
    /var/backups
  )

  capture_critical_parent_state() {
    local path
    : >"$BACKUP/critical-parent-state.tsv"
    for path in "${CRITICAL_PARENTS[@]}"; do
      [[ -d "$path" && ! -L "$path" ]] || {
        echo "Critical parent is missing, not a directory, or is a symlink: $path" >&2
        return 1
      }
      printf '%s\t%s\t%s\t%s\n' \
        "$path" "$(stat -c '%u:%g' "$path")" \
        "$(stat -c '%a' "$path")" "$(stat -c '%F' "$path")" \
        >>"$BACKUP/critical-parent-state.tsv"
    done
    [[ -c /dev/null && ! -L /dev/null ]] || {
      echo "/dev/null is not the expected character device." >&2
      return 1
    }
    printf '/dev/null\t%s\t%s\t%s\t%s\n' \
      "$(stat -c '%u:%g' /dev/null)" "$(stat -c '%a' /dev/null)" \
      "$(stat -c '%t:%T' /dev/null)" "$(stat -c '%F' /dev/null)" \
      >"$BACKUP/critical-device-state.tsv"
  }

  verify_critical_parent_state() {
    local path owner mode kind observed device_path device_owner device_mode device_id device_kind
    [[ -f "$BACKUP/critical-parent-state.tsv" ]] || {
      echo "Critical parent state was not captured; refusing to infer it." >&2
      return 1
    }
    while IFS=$'\t' read -r path owner mode kind; do
      [[ -n "$path" ]] || continue
      [[ -d "$path" && ! -L "$path" ]] || {
        echo "Critical parent changed type or disappeared: $path" >&2
        return 1
      }
      observed="$(stat -c '%u:%g|%a|%F' "$path")"
      [[ "$observed" == "$owner|$mode|$kind" ]] || {
        echo "Critical parent ownership/mode/type changed: $path expected=$owner|$mode|$kind observed=$observed" >&2
        return 1
      }
    done <"$BACKUP/critical-parent-state.tsv"

    [[ -f "$BACKUP/critical-device-state.tsv" ]] || {
      echo "Critical device state was not captured." >&2
      return 1
    }
    IFS=$'\t' read -r device_path device_owner device_mode device_id device_kind \
      <"$BACKUP/critical-device-state.tsv"
    [[ "$device_path" == "/dev/null" && -c /dev/null && ! -L /dev/null ]] || {
      echo "/dev/null changed type." >&2
      return 1
    }
    observed="$(stat -c '%u:%g|%a|%t:%T|%F' /dev/null)"
    [[ "$observed" == "$device_owner|$device_mode|$device_id|$device_kind" ]] || {
      echo "/dev/null ownership/mode/device identity changed." >&2
      return 1
    }
  }

  backup_system_paths() {
    local index=0 path parent base archive
    mkdir -p "$BACKUP/system-leaves"
    : >"$BACKUP/system-leaf-state.tsv"
    for path in "${SYSTEM_PATHS[@]}"; do
      index=$((index + 1))
      parent="$(dirname -- "$path")"
      base="$(basename -- "$path")"
      archive="$BACKUP/system-leaves/$(printf '%03d' "$index").tar"
      [[ -d "$parent" && ! -L "$parent" ]] || {
        echo "System leaf parent is missing or unsafe: $parent" >&2
        return 1
      }
      if [[ -e "$path" || -L "$path" ]]; then
        tar -C "$parent" -cpf "$archive" -- "$base"
        printf '%03d\tpresent\t%s\n' "$index" "$path" >>"$BACKUP/system-leaf-state.tsv"
      else
        printf '%03d\tabsent\t%s\n' "$index" "$path" >>"$BACKUP/system-leaf-state.tsv"
      fi
    done
    SYSTEM_BACKUP_CAPTURED=1
  }

  restore_system_paths() {
    local index=0 path manifest_index state expected_path parent base archive expected_index
    [[ "$SYSTEM_BACKUP_CAPTURED" -eq 1 ]] || {
      echo "System leaf backup was not fully captured; refusing restore." >&2
      return 1
    }
    [[ -f "$BACKUP/system-leaf-state.tsv" ]] || {
      echo "System leaf state manifest is missing; refusing restore." >&2
      return 1
    }

    for path in "${SYSTEM_PATHS[@]}"; do
      index=$((index + 1))
      expected_index="$(printf '%03d' "$index")"
      IFS=$'\t' read -r manifest_index state expected_path < <(
        sed -n "${index}p" "$BACKUP/system-leaf-state.tsv"
      )
      [[ "$manifest_index" == "$expected_index" && "$expected_path" == "$path" ]] || {
        echo "System leaf backup manifest mismatch at index $expected_index." >&2
        return 1
      }
      [[ "$state" == "present" || "$state" == "absent" ]] || {
        echo "Invalid system leaf state for $path: $state" >&2
        return 1
      }

      parent="$(dirname -- "$path")"
      base="$(basename -- "$path")"
      archive="$BACKUP/system-leaves/${expected_index}.tar"
      [[ -d "$parent" && ! -L "$parent" ]] || {
        echo "System leaf restore parent is missing or unsafe: $parent" >&2
        return 1
      }

      rm -rf -- "$path"
      if [[ "$state" == "present" ]]; then
        [[ -f "$archive" && ! -L "$archive" ]] || {
          echo "System leaf archive is missing or unsafe: $archive" >&2
          return 1
        }
        tar -C "$parent" -xpf "$archive" -- "$base"
      else
        [[ ! -e "$archive" && ! -L "$archive" ]] || {
          echo "Unexpected archive exists for originally absent system leaf: $path" >&2
          return 1
        }
      fi
    done
    systemctl daemon-reload || true
  }

  restart_original_services() {
    for service in remihub.service remihub-agent-worker.service remihub-agent-implementation.service; do
      if [[ "${ACTIVE[$service]:-0}" -eq 1 ]]; then
        systemctl start "$service" || true
      fi
    done
  }

  wait_for_service_stable() {
    local service="${1:?service required}"
    local stable_checks=0
    local state

    for _ in $(seq 1 30); do
      state="$(systemctl show "$service" --property=ActiveState --value)"
      if [[ "$state" == "active" ]]; then
        stable_checks=$((stable_checks + 1))
        if [[ "$stable_checks" -ge 5 ]]; then
          return 0
        fi
      else
        stable_checks=0
      fi
      sleep 1
    done

    echo "$service did not remain active for five consecutive checks." >&2
    systemctl status "$service" --no-pager --full >&2 || true
    journalctl -u "$service" -n 100 --no-pager >&2 || true
    return 1
  }

  print_path_chain() {
    local path="${1:?path required}"
    local current="/"
    local remainder="${path#/}"
    local component
    local -a components=()

    printf '%-8s %-18s %-22s %s\n' "MODE" "OWNER" "GROUP" "PATH"
    stat --format='%-8a %-18U %-22G %n' /
    IFS='/' read -r -a components <<<"$remainder"
    for component in "${components[@]}"; do
      [[ -n "$component" ]] || continue
      if [[ "$current" == "/" ]]; then
        current="/$component"
      else
        current="$current/$component"
      fi
      if [[ -e "$current" || -L "$current" ]]; then
        stat --format='%-8a %-18U %-22G %n' "$current"
      else
        printf '%-8s %-18s %-22s %s\n' "MISSING" "-" "-" "$current"
        break
      fi
    done
  }

  require_account_path() {
    local user="${1:?user required}"
    local check="${2:?test check required}"
    local path="${3:?path required}"

    if runuser -u "$user" -- /usr/bin/test "$check" "$path"; then
      printf 'Permission probe passed: user=%s check=%s path=%s\n' \
        "$user" "$check" "$path"
      return 0
    fi

    printf 'Permission probe FAILED: user=%s check=%s path=%s\n' \
      "$user" "$check" "$path" >&2
    print_path_chain "$path" >&2
    return 1
  }

  restore_parent_directory_state() {
    local path="${1:?path required}"
    local existed="${2:?existence state required}"
    local owner="${3:-}"
    local mode="${4:-}"

    case "$existed" in
      0)
        rm -rf -- "$path"
        ;;
      1)
        [[ -n "$owner" && -n "$mode" ]] || {
          echo "Captured parent metadata is incomplete for $path; refusing restore." >&2
          return 1
        }
        [[ -d "$path" && ! -L "$path" ]] || {
          echo "Captured parent no longer exists safely: $path" >&2
          return 1
        }
        chown "$owner" "$path"
        chmod "$mode" "$path"
        ;;
      *)
        echo "Parent existence state was not captured for $path; refusing restore." >&2
        return 1
        ;;
    esac
  }

  cleanup_qa_frontend_bootstrap_worktree() {
    local expected="/opt/remihub-agent/deployment/qa/worktrees/card-${QA_FRONTEND_BOOTSTRAP_CARD}-r1"

    if [[ -z "$QA_FRONTEND_BOOTSTRAP_WORKTREE" ]]; then
      return 0
    fi
    [[ "$QA_FRONTEND_BOOTSTRAP_WORKTREE" == "$expected" ]] || {
      echo "QA frontend bootstrap worktree identity drifted; refusing broad cleanup." >&2
      return 1
    }
    if [[ -L "$QA_FRONTEND_BOOTSTRAP_WORKTREE" ]]; then
      echo "QA frontend bootstrap worktree became a symlink; refusing cleanup." >&2
      return 1
    fi
    if [[ -e "$QA_FRONTEND_BOOTSTRAP_WORKTREE" ]]; then
      [[ -d "$QA_FRONTEND_BOOTSTRAP_WORKTREE" ]] || {
        echo "QA frontend bootstrap worktree is not a directory; refusing cleanup." >&2
        return 1
      }
      runuser -u remihub-deployer -- env \
        HOME=/nonexistent \
        GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
        GIT_TERMINAL_PROMPT=0 \
        git --git-dir=/opt/remihub-agent/deployment/qa/repository.git \
        worktree remove --force "$QA_FRONTEND_BOOTSTRAP_WORKTREE" || return 1
    fi
    [[ ! -e "$QA_FRONTEND_BOOTSTRAP_WORKTREE" && \
       ! -L "$QA_FRONTEND_BOOTSTRAP_WORKTREE" ]] || {
      echo "Exact QA frontend bootstrap worktree remains after Git cleanup." >&2
      return 1
    }
    QA_FRONTEND_BOOTSTRAP_WORKTREE=""
  }

  bootstrap_qa_frontend() {
    local qa_runtime="/opt/remihub-agent/deployment/qa/application"
    local qa_repo="/opt/remihub-agent/deployment/qa/repository.git"
    local worktree_root="/opt/remihub-agent/deployment/qa/worktrees"
    local candidate_tree manifest archive identity builder_record

    [[ -d "$qa_runtime" && ! -L "$qa_runtime" ]] || {
      echo "Fresh QA runtime is missing or unsafe before frontend bootstrap." >&2
      return 1
    }
    [[ -d "$qa_repo" && ! -L "$qa_repo" ]] || {
      echo "Protected QA repository is missing or unsafe before frontend bootstrap." >&2
      return 1
    }
    [[ -f "$qa_runtime/frontend-web/package.json" && \
       ! -L "$qa_runtime/frontend-web/package.json" ]] || {
      echo "Fresh QA runtime frontend package.json is missing or unsafe." >&2
      return 1
    }
    [[ -f "$qa_runtime/frontend-web/package-lock.json" && \
       ! -L "$qa_runtime/frontend-web/package-lock.json" ]] || {
      echo "Fresh QA runtime frontend package-lock.json is missing or unsafe." >&2
      return 1
    }
    [[ ! -e "$qa_runtime/frontend-web/dist" && \
       ! -L "$qa_runtime/frontend-web/dist" ]] || {
      echo "Fresh QA Git runtime unexpectedly already contains frontend dist." >&2
      return 1
    }

    QA_FRONTEND_BOOTSTRAP_WORKTREE="$worktree_root/card-${QA_FRONTEND_BOOTSTRAP_CARD}-r1"
    [[ ! -e "$QA_FRONTEND_BOOTSTRAP_WORKTREE" && \
       ! -L "$QA_FRONTEND_BOOTSTRAP_WORKTREE" ]] || {
      echo "QA frontend bootstrap worktree path already exists." >&2
      return 1
    }

    runuser -u remihub-deployer -- env \
      HOME=/nonexistent \
      GIT_CONFIG_NOSYSTEM=1 \
      GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
      GIT_TERMINAL_PROMPT=0 \
      git --git-dir="$qa_repo" \
      worktree add --detach "$QA_FRONTEND_BOOTSTRAP_WORKTREE" "$NEW_COMMIT"
    runuser -u remihub-deployer -- chmod 0700 "$QA_FRONTEND_BOOTSTRAP_WORKTREE"

    candidate_tree="$(
      runuser -u remihub-deployer -- env \
        HOME=/nonexistent \
        GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
        GIT_TERMINAL_PROMPT=0 \
        git -C "$QA_FRONTEND_BOOTSTRAP_WORKTREE" rev-parse 'HEAD^{tree}'
    )"
    [[ "$(
      runuser -u remihub-deployer -- env \
        HOME=/nonexistent \
        GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
        GIT_TERMINAL_PROMPT=0 \
        git -C "$QA_FRONTEND_BOOTSTRAP_WORKTREE" rev-parse HEAD
    )" == "$NEW_COMMIT" ]] || {
      echo "QA frontend bootstrap worktree commit mismatch." >&2
      return 1
    }

    /usr/local/libexec/remihub-backend-deployment-control \
      frontend-prepare qa \
      "$QA_FRONTEND_BOOTSTRAP_WORKTREE" \
      "$NEW_COMMIT" \
      "$candidate_tree"

    QA_FRONTEND_BOOTSTRAP_ARTIFACT_ROOT="/opt/remihub-agent/deployment/qa/artifacts/install-verification-$STAMP/frontend-bootstrap"
    runuser -u remihub-deployer -- \
      install -d -m 0750 "$QA_FRONTEND_BOOTSTRAP_ARTIFACT_ROOT"

    builder_record="$BACKUP/qa-frontend-bootstrap-builder.json"
    runuser -u remihub-deployer -- env \
      HOME=/nonexistent \
      LANG=C.UTF-8 \
      PATH=/opt/remihub/.venv/bin:/usr/bin:/bin \
      PYTHONPATH="$RELEASE" \
      PYTHONDONTWRITEBYTECODE=1 \
      /opt/remihub/.venv/bin/python - \
        "$QA_FRONTEND_BOOTSTRAP_WORKTREE" \
        "$QA_FRONTEND_BOOTSTRAP_ARTIFACT_ROOT" \
        "$QA_FRONTEND_BOOTSTRAP_CARD" \
        "$QA_FRONTEND_BOOTSTRAP_RUN" \
        "$QA_FRONTEND_BOOTSTRAP_APPROVAL" \
        "$QA_FRONTEND_BOOTSTRAP_IMPLEMENTATION" \
        "$NEW_COMMIT" \
        >"$builder_record" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from backend.core.agent_deployment import LocalFrontendArtifactBuilder

(
    worktree,
    artifact_root,
    card_id,
    deployment_run_id,
    approval_id,
    implementation_run_id,
    candidate_commit,
) = sys.argv[1:]

result = LocalFrontendArtifactBuilder(
    timeout_seconds=900,
    environment="qa",
).build(
    candidate_worktree=Path(worktree),
    artifact_root=Path(artifact_root),
    card_id=card_id,
    card_revision=1,
    deployment_run_id=deployment_run_id,
    approval_id=approval_id,
    implementation_run_id=implementation_run_id,
    candidate_commit=candidate_commit,
    # Explicit installer bootstrap signal. This is not a candidate diff.
    changed_files=("frontend-web/package.json",),
)

if not result.changed:
    raise SystemExit("QA frontend bootstrap builder unexpectedly reported unchanged")
if not result.reproducibility.get("matched"):
    raise SystemExit("QA frontend bootstrap deterministic builds did not match")
if not all(
    isinstance(value, str) and value
    for value in (
        result.manifest_path,
        result.archive_path,
        result.artifact_identity,
    )
):
    raise SystemExit("QA frontend bootstrap artifact evidence is incomplete")

print(
    json.dumps(
        {
            "manifest_path": result.manifest_path,
            "archive_path": result.archive_path,
            "artifact_identity": result.artifact_identity,
            "lockfile_sha256": result.lockfile_sha256,
            "reproducibility": result.reproducibility,
        },
        sort_keys=True,
    )
)
PY

    manifest="$QA_FRONTEND_BOOTSTRAP_ARTIFACT_ROOT/$QA_FRONTEND_BOOTSTRAP_CARD/$QA_FRONTEND_BOOTSTRAP_RUN/frontend-web/$NEW_COMMIT/manifest.json"
    archive="$QA_FRONTEND_BOOTSTRAP_ARTIFACT_ROOT/$QA_FRONTEND_BOOTSTRAP_CARD/$QA_FRONTEND_BOOTSTRAP_RUN/frontend-web/$NEW_COMMIT/dist.tar"
    [[ -f "$manifest" && ! -L "$manifest" ]] || {
      echo "QA frontend bootstrap manifest is missing or unsafe." >&2
      return 1
    }
    [[ -f "$archive" && ! -L "$archive" ]] || {
      echo "QA frontend bootstrap archive is missing or unsafe." >&2
      return 1
    }

    identity="$(
      /opt/remihub/.venv/bin/python - "$builder_record" <<'PY'
import json
import re
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
identity = payload.get("artifact_identity")
if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
    raise SystemExit("invalid QA frontend bootstrap artifact identity")
print(identity)
PY
    )"

    /usr/local/libexec/remihub-backend-deployment-control \
      frontend-install qa \
      "$manifest" \
      "$archive" \
      "$identity" \
      "$NEW_COMMIT" \
      "$QA_FRONTEND_BOOTSTRAP_CARD" \
      "$QA_FRONTEND_BOOTSTRAP_RUN"

    /usr/local/libexec/remihub-backend-deployment-control \
      frontend-verify qa \
      "$manifest" \
      "$identity"

    [[ -f "$qa_runtime/frontend-web/dist/index.html" && \
       ! -L "$qa_runtime/frontend-web/dist/index.html" ]] || {
      echo "QA frontend bootstrap did not install a safe dist/index.html." >&2
      return 1
    }
    runuser -u remihub-qa-app -- \
      /usr/bin/test -r "$qa_runtime/frontend-web/dist/index.html" || {
      echo "QA frontend index is not readable by remihub-qa-app." >&2
      return 1
    }

    cleanup_qa_frontend_bootstrap_worktree
    echo "QA_FRONTEND_BOOTSTRAP=PASS"
  }

  rollback() {
    local status=$?
    trap - EXIT
    if [[ "$COMPLETE" -eq 1 ]]; then exit "$status"; fi
    echo "Installation failed; preserving or restoring only state explicitly captured in $BACKUP" >&2
    set +e
    systemctl stop remihub-agent-deployment-qa.service remihub-agent-deployment-production.service remihub-backend-qa.service

    if [[ "$PLANNING_PROMOTED" -eq 1 ]]; then
      runuser -u alex -- git -C "$PLANNING" reset --hard "$EXPECTED_BASE"
      /usr/local/libexec/remihub-backend-deployment-control \
        harden-planning production "$EXPECTED_BASE" 2>/dev/null || true
    fi
    if [[ "$SOURCE_PROMOTED" -eq 1 ]]; then
      runuser -u remihub-agent -- git --git-dir="$SOURCE" \
        update-ref refs/heads/main "$EXPECTED_BASE" "$NEW_COMMIT"
    fi
    if [[ "$PROD_PROMOTED" -eq 1 ]]; then
      systemctl stop remihub.service
      runuser -u alex -- git -C "$PROD" reset --hard "$EXPECTED_BASE"
    fi

    if [[ "$ROLLBACK_STATE_CAPTURED" -eq 1 ]]; then
      cleanup_qa_frontend_bootstrap_worktree ||         echo "Exact QA frontend bootstrap worktree cleanup failed; no broad cleanup was attempted." >&2

      case "$QA_REPOSITORY_EXISTED" in
        1)
          rm -rf -- /opt/remihub-agent/deployment/qa/repository.git
          tar -C /opt/remihub-agent/deployment/qa \
            -xpf "$BACKUP/qa-repository.tar" -- repository.git
          ;;
        0)
          rm -rf -- /opt/remihub-agent/deployment/qa/repository.git
          ;;
        *)
          echo "QA repository existence state is uncaptured; refusing rollback mutation." >&2
          ;;
      esac

      case "$QA_APPLICATION_EXISTED" in
        1)
          rm -rf -- /opt/remihub-agent/deployment/qa/application
          tar -C /opt/remihub-agent/deployment/qa \
            -xpf "$BACKUP/qa-application.tar" -- application
          ;;
        0)
          rm -rf -- /opt/remihub-agent/deployment/qa/application
          ;;
        *)
          echo "QA application existence state is uncaptured; refusing rollback mutation." >&2
          ;;
      esac

      case "$PROD_DEPLOYMENT_EXISTED" in
        1)
          rm -rf -- /opt/remihub-agent/deployment/production
          tar -C /opt/remihub-agent/deployment \
            -xpf "$BACKUP/production-deployment.tar" -- production
          ;;
        0)
          rm -rf -- /opt/remihub-agent/deployment/production
          ;;
        *)
          echo "Production deployment existence state is uncaptured; refusing rollback mutation." >&2
          ;;
      esac

      rm -rf -- "/opt/remihub-agent/deployment/qa/artifacts/install-verification-$STAMP"
      [[ -n "$RELEASE" ]] && rm -rf -- "$RELEASE"

      if [[ "$SYSTEM_MUTATED" -eq 1 ]]; then
        restore_system_paths || \
          echo "System leaf rollback failed; no parent permission repair was attempted." >&2
      fi

      restore_parent_directory_state \
        /opt/remihub-agent/deployment/config \
        "$CONFIG_PARENT_EXISTED" \
        "$CONFIG_PARENT_OWNER" \
        "$CONFIG_PARENT_MODE" 2>/dev/null || true
      restore_parent_directory_state \
        /opt/remihub-agent/deployment/qa \
        "$QA_PARENT_EXISTED" \
        "$QA_PARENT_OWNER" \
        "$QA_PARENT_MODE" 2>/dev/null || true
      if [[ -n "$DEPLOYMENT_PARENT_OWNER" && -n "$DEPLOYMENT_PARENT_MODE" ]]; then
        chown "$DEPLOYMENT_PARENT_OWNER" /opt/remihub-agent/deployment 2>/dev/null || true
        chmod "$DEPLOYMENT_PARENT_MODE" /opt/remihub-agent/deployment 2>/dev/null || true
      fi

      verify_critical_parent_state || \
        echo "CRITICAL: a critical parent/device identity changed; no recursive permission repair was attempted." >&2
    else
      echo "Rollback state was not fully captured; refusing deployment/system restore." >&2
    fi

    if [[ "$QA_USER_CREATED" -eq 1 ]]; then userdel remihub-qa-app 2>/dev/null || true; fi
    if [[ "$QA_GROUP_CREATED" -eq 1 ]]; then groupdel remihub-qa-app 2>/dev/null || true; fi
    if [[ "$DEPLOYER_USER_CREATED" -eq 1 ]]; then userdel remihub-deployer 2>/dev/null || true; fi
    if [[ "$DEPLOYER_GROUP_CREATED" -eq 1 ]]; then groupdel remihub-deployer 2>/dev/null || true; fi
    runuser -u remihub-agent -- git --git-dir="$SOURCE" \
      update-ref -d "refs/remihub-install/$STAMP" 2>/dev/null || true
    if [[ -d "$STAGING" ]]; then
      runuser -u alex -- git -C "$PROD" worktree remove --force "$STAGING" 2>/dev/null || true
    fi
    runuser -u alex -- git -C "$PROD" worktree prune 2>/dev/null || true
    runuser -u alex -- git -C "$PROD" branch -D "$INSTALL_BRANCH" 2>/dev/null || true
    restart_original_services
    rm -rf -- "$STAGING"
    echo "Rollback attempted. Review $BACKUP and service status before retrying." >&2
    exit "$status"
  }

  trap rollback EXIT

  echo "[1/10] Preflight exact production state"
  [[ "$(runuser -u alex -- git -C "$PROD" rev-parse --abbrev-ref HEAD)" == "main" ]]
  [[ "$(runuser -u alex -- git -C "$PROD" rev-parse HEAD)" == "$EXPECTED_BASE" ]] || {
    echo "Production is not at the expected base $EXPECTED_BASE" >&2; exit 1;
  }
  [[ -z "$(runuser -u alex -- git -C "$PROD" status --porcelain=v1 --untracked-files=no)" ]] || {
    echo "Production has tracked modifications." >&2; exit 1;
  }
  [[ "$(runuser -u remihub-agent -- git --git-dir="$SOURCE" rev-parse refs/heads/main)" == "$EXPECTED_BASE" ]]
  [[ "$(runuser -u alex -- git -C "$PLANNING" rev-parse HEAD)" == "$EXPECTED_BASE" ]]
  [[ -z "$(runuser -u alex -- git -C "$PLANNING" status --porcelain=v1 --untracked-files=no)" ]]
  runuser -u remihub-agent -- /usr/bin/test -r "$PLANNING/backend/agent_worker.py"
  systemctl is-active --quiet remihub.service || {
    echo "remihub.service must be active before installation." >&2
    exit 1
  }
  ! systemctl is-active --quiet remihub-agent-deployment-qa.service
  ! systemctl is-active --quiet remihub-agent-deployment-production.service
  if find /opt/remihub-agent/implementation/worktrees -mindepth 1 -maxdepth 1 -type d -name 'card-*' -print -quit | grep -q .; then
    echo "An implementation card worktree is active; close or clean it before installation." >&2
    exit 1
  fi
  for config in \
    /opt/remihub-agent/worker-config/qa-worker.ini \
    /opt/remihub-agent/worker-config/prod-worker.ini \
    /opt/remihub-agent/config/qa-migrator.ini \
    /opt/remihub-agent/config/qa-parity-reader.ini \
    /opt/remihub-agent/config/prod-migrator.ini \
    /opt/remihub-agent/config/qa-app.ini; do
    [[ -f "$config" ]] || { echo "Missing required protected config: $config" >&2; exit 1; }
  done
  runuser -u alex -- git -C "$PROD" apply --check --binary "$PATCH_FILE"

  echo "[2/10] Materialize exact candidate and run compile/full tests"
  rm -rf "$STAGING"
  runuser -u alex -- git -C "$PROD" worktree add -b "$INSTALL_BRANCH" "$STAGING" "$EXPECTED_BASE" >/dev/null
  runuser -u alex -- git -C "$STAGING" apply --index --binary --whitespace=error-all "$PATCH_FILE"
  mkdir -p "$STAGING/.test-runtime/logs"
  chown -R alex:storage "$STAGING/.test-runtime"
  (
    cd "$STAGING"
    runuser -u alex -- env \
      HOME=/home/alex \
      PATH=/opt/remihub/.venv/bin:/usr/bin:/bin \
      PYTHONPATH="$STAGING/deployments/agent_backend/validation-support:$STAGING" \
      PYTHONPYCACHEPREFIX="$STAGING/.test-runtime/pycache" \
      PYTHONDONTWRITEBYTECODE=1 \
      REMIHUB_CONFIG_FILE="$STAGING/deployments/agent_backend/validation-support/application.ini" \
      REMIHUB_DATABASE_CONFIG="$STAGING/deployments/agent_backend/validation-support/database.ini" \
      REMIHUB_ENV_FILE=/dev/null \
      REMIHUB_LOG_DIR="$STAGING/.test-runtime/logs" \
      /opt/remihub/.venv/bin/python -m compileall -q backend tests
    runuser -u alex -- env \
      HOME=/home/alex \
      PATH=/opt/remihub/.venv/bin:/usr/bin:/bin \
      PYTHONPATH="$STAGING/deployments/agent_backend/validation-support:$STAGING" \
      PYTHONPYCACHEPREFIX="$STAGING/.test-runtime/pycache" \
      PYTHONDONTWRITEBYTECODE=1 \
      REMIHUB_CONFIG_FILE="$STAGING/deployments/agent_backend/validation-support/application.ini" \
      REMIHUB_DATABASE_CONFIG="$STAGING/deployments/agent_backend/validation-support/database.ini" \
      REMIHUB_ENV_FILE=/dev/null \
      REMIHUB_LOG_DIR="$STAGING/.test-runtime/logs" \
      /opt/remihub/.venv/bin/python -m unittest discover -s tests -v \
      | tee "$BACKUP/preinstall-tests.log"
  )
  rm -rf "$STAGING/.test-runtime"
  runuser -u alex -- git -C "$STAGING" add -A
  runuser -u alex -- git -C "$STAGING" \
    -c user.name='RemiHub Deployment' \
    -c user.email='remihub-deployment@invalid.local' \
    commit -m 'Add consolidated backend deployment executor' >/dev/null
  NEW_COMMIT="$(runuser -u alex -- git -C "$STAGING" rev-parse HEAD)"
  RELEASE="/opt/remihub-agent/runtime/releases/backend-deployment-$NEW_COMMIT"
  printf '%s\n' "$NEW_COMMIT" >"$BACKUP/new-commit.txt"

  echo "[3/10] Back up current deployment/runtime configuration"
  capture_critical_parent_state

  DEPLOYMENT_PARENT_OWNER="$(stat -c '%u:%g' /opt/remihub-agent/deployment)"
  DEPLOYMENT_PARENT_MODE="$(stat -c '%a' /opt/remihub-agent/deployment)"
  printf 'owner=%s\nmode=%s\n' \
    "$DEPLOYMENT_PARENT_OWNER" "$DEPLOYMENT_PARENT_MODE" \
    >"$BACKUP/deployment-parent-before.txt"

  if [[ -d /opt/remihub-agent/deployment/qa && ! -L /opt/remihub-agent/deployment/qa ]]; then
    QA_PARENT_EXISTED=1
    QA_PARENT_OWNER="$(stat -c '%u:%g' /opt/remihub-agent/deployment/qa)"
    QA_PARENT_MODE="$(stat -c '%a' /opt/remihub-agent/deployment/qa)"
  else
    QA_PARENT_EXISTED=0
  fi
  printf 'existed=%s\nowner=%s\nmode=%s\n' \
    "$QA_PARENT_EXISTED" "$QA_PARENT_OWNER" "$QA_PARENT_MODE" \
    >"$BACKUP/qa-parent-before.txt"

  if [[ -d /opt/remihub-agent/deployment/config && ! -L /opt/remihub-agent/deployment/config ]]; then
    CONFIG_PARENT_EXISTED=1
    CONFIG_PARENT_OWNER="$(stat -c '%u:%g' /opt/remihub-agent/deployment/config)"
    CONFIG_PARENT_MODE="$(stat -c '%a' /opt/remihub-agent/deployment/config)"
  else
    CONFIG_PARENT_EXISTED=0
  fi
  printf 'existed=%s\nowner=%s\nmode=%s\n' \
    "$CONFIG_PARENT_EXISTED" "$CONFIG_PARENT_OWNER" "$CONFIG_PARENT_MODE" \
    >"$BACKUP/config-parent-before.txt"

  [[ "$QA_PARENT_EXISTED" -eq 1 ]] || {
    echo "Existing QA deployment parent is required for guarded upgrade." >&2
    exit 1
  }
  [[ -d /opt/remihub-agent/deployment/qa/repository.git && \
     ! -L /opt/remihub-agent/deployment/qa/repository.git ]] || {
    echo "Existing protected QA repository is required for guarded upgrade." >&2
    exit 1
  }

  QA_REPOSITORY_EXISTED=1
  OLD_QA="$(
    git --git-dir=/opt/remihub-agent/deployment/qa/repository.git \
      rev-parse qa-main^{commit}
  )"
  printf '%s\n' "$OLD_QA" >"$BACKUP/qa-target-before.txt"
  git --git-dir=/opt/remihub-agent/deployment/qa/repository.git \
    bundle create "$BACKUP/qa-target.bundle" --all
  tar -C /opt/remihub-agent/deployment/qa \
    -cpf "$BACKUP/qa-repository.tar" -- repository.git

  if [[ -d /opt/remihub-agent/deployment/qa/application && \
        ! -L /opt/remihub-agent/deployment/qa/application ]]; then
    QA_APPLICATION_EXISTED=1
    tar -C /opt/remihub-agent/deployment/qa \
      -cpf "$BACKUP/qa-application.tar" -- application
  else
    QA_APPLICATION_EXISTED=0
  fi

  if [[ -d /opt/remihub-agent/deployment/production && \
        ! -L /opt/remihub-agent/deployment/production ]]; then
    PROD_DEPLOYMENT_EXISTED=1
    tar -C /opt/remihub-agent/deployment \
      -cpf "$BACKUP/production-deployment.tar" -- production
  else
    PROD_DEPLOYMENT_EXISTED=0
  fi

  if [[ -d /opt/remihub-agent/deployment/production/repository.git && \
        ! -L /opt/remihub-agent/deployment/production/repository.git ]]; then
    PROD_TARGET_EXISTED=1
    OLD_PROD_TARGET="$(
      git --git-dir=/opt/remihub-agent/deployment/production/repository.git \
        rev-parse production-main^{commit}
    )"
    git --git-dir=/opt/remihub-agent/deployment/production/repository.git \
      bundle create "$BACKUP/production-target.bundle" --all
  else
    PROD_TARGET_EXISTED=0
  fi

  backup_system_paths
  verify_critical_parent_state
  ROLLBACK_STATE_CAPTURED=1
  echo "ROLLBACK_STATE_CAPTURED=PASS"

  echo "[4/10] Install least-privilege accounts, configs, helpers, and immutable release"
  SYSTEM_MUTATED=1
  echo "[4/10] Install least-privilege accounts, configs, helpers, and immutable release"
  if ! getent group remihub-deployer >/dev/null; then
    groupadd --system remihub-deployer
    DEPLOYER_GROUP_CREATED=1
  fi
  if ! id remihub-deployer >/dev/null 2>&1; then
    useradd --system --gid remihub-deployer --groups remihub-agent --home-dir /nonexistent --shell /usr/sbin/nologin remihub-deployer
    DEPLOYER_USER_CREATED=1
  fi
  usermod -a -G remihub-agent remihub-deployer

  runuser -u remihub-deployer -- "$PG_DUMP_BINARY" --version >/dev/null
  runuser -u remihub-deployer -- "$PG_RESTORE_BINARY" --version >/dev/null
  if ! getent group remihub-qa-app >/dev/null; then
    groupadd --system remihub-qa-app
    QA_GROUP_CREATED=1
  fi
  if ! id remihub-qa-app >/dev/null 2>&1; then
    useradd --system --gid remihub-qa-app --home-dir /nonexistent --shell /usr/sbin/nologin remihub-qa-app
    QA_USER_CREATED=1
  fi

  # Give execute-only traversal through every shared ancestor. Protected
  # repositories, worktrees, artifacts, configs, and logs retain their own
  # restrictive ownership and modes.
  install -d -o root -g root -m 0711 /opt/remihub-agent/deployment
  install -d -o root -g root -m 0711 /opt/remihub-agent/deployment/config
  install -d -o root -g root -m 0711 /opt/remihub-agent/deployment/qa
  install -d -o remihub-deployer -g remihub-agent -m 0750 \
    /opt/remihub-agent/deployment/production \
    /opt/remihub-agent/deployment/production/worktrees \
    /opt/remihub-agent/deployment/production/artifacts
  install -d -o remihub-deployer -g remihub-agent -m 0750 \
    /opt/remihub-agent/deployment/qa/worktrees \
    /opt/remihub-agent/deployment/qa/artifacts
  chown remihub-deployer:remihub-agent \
    /opt/remihub-agent/deployment/qa/repository.git \
    /opt/remihub-agent/deployment/qa/worktrees \
    /opt/remihub-agent/deployment/qa/artifacts
  chmod 0750 /opt/remihub-agent/deployment/qa/repository.git /opt/remihub-agent/deployment/qa/worktrees /opt/remihub-agent/deployment/qa/artifacts
  install -d -o remihub-qa-app -g remihub-qa-app -m 0750 /opt/remihub-agent/deployment/qa/logs
  install -d -o root -g remihub-deployer -m 0750 \
    /opt/remihub-agent/deployment/qa/frontend-backups
  install -d -o remihub-deployer -g root -m 0750 \
    /var/backups/remihub-agent/backend-deployments/qa \
    /var/backups/remihub-agent/backend-deployments/production \
    /var/backups/remihub-agent/frontend-web/production
  install -d -o root -g remihub-deployer -m 0750 \
    /var/cache/remihub-agent/npm \
    /var/lib/remihub-agent/npm-prep

  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/worker-config/qa-worker.ini /opt/remihub-agent/deployment/config/qa-worker.ini
  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/worker-config/prod-worker.ini /opt/remihub-agent/deployment/config/prod-worker.ini
  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/config/qa-migrator.ini /opt/remihub-agent/deployment/config/qa-migrator.ini
  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/config/qa-parity-reader.ini /opt/remihub-agent/deployment/config/qa-parity-reader.ini
  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/config/prod-migrator.ini /opt/remihub-agent/deployment/config/prod-migrator.ini
  install -o root -g remihub-qa-app -m 0640 /opt/remihub-agent/config/qa-app.ini /opt/remihub-agent/deployment/config/qa-app.ini
  install -o root -g remihub-qa-app -m 0640 "$ASSETS/validation-support/application.ini" /opt/remihub-agent/deployment/config/qa-application.ini
  install -o root -g root -m 0644 \
    "$ASSETS/gitconfig/remihub-backend-deployment.gitconfig" \
    "$GIT_SAFE_CONFIG"
  install -o root -g root -m 0644 \
    "$ASSETS/frontend-web-policy.json" \
    /opt/remihub-agent/deployment/config/frontend-web-policy.json

  echo "Validating deployment account traversal before repository seeding"
  require_account_path remihub-deployer -x /opt/remihub-agent/deployment
  require_account_path remihub-deployer -x /opt/remihub-agent/deployment/qa
  require_account_path remihub-deployer -x /opt/remihub-agent/deployment/config
  require_account_path remihub-deployer -r /opt/remihub-agent/deployment/config/qa-worker.ini
  require_account_path remihub-deployer -r /opt/remihub-agent/deployment/config/qa-migrator.ini
  require_account_path remihub-deployer -r /opt/remihub-agent/deployment/config/qa-parity-reader.ini
  require_account_path remihub-deployer -r /opt/remihub-agent/deployment/config/prod-migrator.ini
  require_account_path remihub-deployer -x /var/cache/remihub-agent/npm
  require_account_path remihub-deployer -x /var/lib/remihub-agent/npm-prep
  require_account_path remihub-qa-app -x /opt/remihub-agent/deployment
  require_account_path remihub-qa-app -x /opt/remihub-agent/deployment/qa
  require_account_path remihub-qa-app -x /opt/remihub-agent/deployment/config
  require_account_path remihub-qa-app -r /opt/remihub-agent/deployment/config/qa-app.ini
  require_account_path remihub-qa-app -r /opt/remihub-agent/deployment/config/qa-application.ini
  if runuser -u remihub-qa-app -- /usr/bin/test -r \
    /opt/remihub-agent/deployment/qa/repository.git/HEAD; then
    echo "QA application account can unexpectedly read the protected target repository." >&2
    exit 1
  fi
  echo "Isolation probe passed: remihub-qa-app cannot read repository.git"

  install -o root -g root -m 0755 "$ASSETS/libexec/remihub-backend-deployment-control" /usr/local/libexec/remihub-backend-deployment-control
  install -o root -g root -m 0755 "$ASSETS/libexec/remihub-backend-npm-cache-control" /usr/local/libexec/remihub-backend-npm-cache-control
  install -o root -g root -m 0755 "$ASSETS/libexec/remihub-backend-validation-sandbox" /usr/local/libexec/remihub-backend-validation-sandbox
  install -o root -g root -m 0755 "$ASSETS/libexec/remihub-backend-qa-server" /usr/local/libexec/remihub-backend-qa-server
  rm -rf /usr/local/libexec/remihub-backend-validation-support
  install -d -o root -g root -m 0755 /usr/local/libexec/remihub-backend-validation-support
  install -o root -g root -m 0644 "$ASSETS/validation-support/"* /usr/local/libexec/remihub-backend-validation-support/
  install -o root -g root -m 0440 "$ASSETS/sudoers/remihub-backend-deployment" /etc/sudoers.d/remihub-backend-deployment
  visudo -cf /etc/sudoers.d/remihub-backend-deployment

  [[ "$(stat -c '%U:%G:%a' /usr/local/libexec/remihub-backend-npm-cache-control)" == "root:root:755" ]] || {
    echo "Installed npm cache control ownership or mode is unsafe." >&2
    exit 1
  }
  for npm_root in /var/cache/remihub-agent/npm /var/lib/remihub-agent/npm-prep; do
    [[ "$(stat -c '%U:%G:%a' "$npm_root")" == "root:remihub-deployer:750" ]] || {
      echo "Installed npm cache root ownership or mode is unsafe: $npm_root" >&2
      exit 1
    }
  done

  rm -rf "$RELEASE"
  install -d -o root -g remihub-agent -m 0750 "$RELEASE"
  runuser -u alex -- git -C "$STAGING" archive "$NEW_COMMIT" | tar -x -C "$RELEASE"
  chown -R root:remihub-agent "$RELEASE"
  chmod -R o-rwx "$RELEASE"
  find "$RELEASE" -type d -exec chmod 0750 {} +
  find "$RELEASE" -type f -exec chmod 0640 {} +
  chmod 0750 "$RELEASE/deployments/agent_backend/qa-verify.sh"

  echo "[5/10] Seed isolated QA and production deployment repositories"
  runuser -u remihub-deployer -- env \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
    GIT_TERMINAL_PROMPT=0 \
    git --git-dir=/opt/remihub-agent/deployment/qa/repository.git \
    fetch --no-tags "$PROD" "$INSTALL_BRANCH:refs/remihub-install/$STAMP"
  if [[ "$OLD_QA" != "$NEW_COMMIT" ]]; then
    runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/qa/repository.git tag "archive-before-backend-final-$STAMP" "$OLD_QA"
  fi
  runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/qa/repository.git update-ref refs/heads/qa-main "$NEW_COMMIT" "$OLD_QA"
  QA_REINITIALIZED=1

  if [[ ! -d /opt/remihub-agent/deployment/production/repository.git ]]; then
    runuser -u remihub-deployer -- git init --bare /opt/remihub-agent/deployment/production/repository.git >/dev/null
  fi
  chown remihub-deployer:remihub-agent /opt/remihub-agent/deployment/production/repository.git
  if [[ -n "$(runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/production/repository.git remote)" ]]; then
    echo "Production deployment repository unexpectedly has remotes." >&2
    exit 1
  fi
  runuser -u remihub-deployer -- env \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
    GIT_TERMINAL_PROMPT=0 \
    git --git-dir=/opt/remihub-agent/deployment/production/repository.git \
    fetch --no-tags "$PROD" "$INSTALL_BRANCH:refs/remihub-install/$STAMP"
  if [[ "$PROD_TARGET_EXISTED" -eq 1 ]]; then
    runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/production/repository.git update-ref refs/heads/production-main "$NEW_COMMIT" "$OLD_PROD_TARGET"
  else
    runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/production/repository.git update-ref refs/heads/production-main "$NEW_COMMIT"
  fi

  rm -rf /opt/remihub-agent/deployment/qa/application
  env \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
    GIT_TERMINAL_PROMPT=0 \
    git clone --no-hardlinks --no-checkout \
    /opt/remihub-agent/deployment/qa/repository.git \
    /opt/remihub-agent/deployment/qa/application >/dev/null
  git -C /opt/remihub-agent/deployment/qa/application checkout -b qa-runtime "$NEW_COMMIT" >/dev/null
  /usr/local/libexec/remihub-backend-deployment-control \
    harden-runtime qa "$NEW_COMMIT"
  echo "Validating QA runtime paths as remihub-qa-app"
  require_account_path remihub-qa-app -x /opt/remihub-agent/deployment
  require_account_path remihub-qa-app -x /opt/remihub-agent/deployment/qa
  require_account_path remihub-qa-app -x /opt/remihub-agent/deployment/qa/application
  require_account_path remihub-qa-app -r /opt/remihub-agent/deployment/qa/application/backend/main.py
  require_account_path remihub-qa-app -x /opt/remihub-agent/deployment/config
  require_account_path remihub-qa-app -r /opt/remihub-agent/deployment/config/qa-app.ini
  require_account_path remihub-qa-app -r /opt/remihub-agent/deployment/config/qa-application.ini
  runuser -u remihub-deployer -- \
    git --git-dir=/opt/remihub-agent/deployment/qa/repository.git \
    fsck --full --strict >/dev/null
  [[ "$(
    runuser -u remihub-deployer -- \
      git --git-dir=/opt/remihub-agent/deployment/qa/repository.git \
      rev-parse 'qa-main^{commit}'
  )" == "$NEW_COMMIT" ]]

  echo "Bootstrap deterministic frontend artifact into fresh QA runtime"
  bootstrap_qa_frontend

  echo "[6/10] Install static systemd workers and QA runtime"
  install -o root -g root -m 0644 "$ASSETS/systemd/remihub-backend-qa.service" /etc/systemd/system/remihub-backend-qa.service
  sed \
    -e "s|@RELEASE@|$RELEASE|g" \
    -e "s|@PG_DUMP@|$PG_DUMP_BINARY|g" \
    -e "s|@PG_RESTORE@|$PG_RESTORE_BINARY|g" \
    "$ASSETS/systemd/remihub-agent-deployment-qa.service.in" \
    >"$BACKUP/remihub-agent-deployment-qa.service.new"
  sed \
    -e "s|@RELEASE@|$RELEASE|g" \
    -e "s|@PG_DUMP@|$PG_DUMP_BINARY|g" \
    -e "s|@PG_RESTORE@|$PG_RESTORE_BINARY|g" \
    "$ASSETS/systemd/remihub-agent-deployment-production.service.in" \
    >"$BACKUP/remihub-agent-deployment-production.service.new"
  install -o root -g root -m 0644 \
    "$BACKUP/remihub-agent-deployment-qa.service.new" \
    /etc/systemd/system/remihub-agent-deployment-qa.service
  install -o root -g root -m 0644 \
    "$BACKUP/remihub-agent-deployment-production.service.new" \
    /etc/systemd/system/remihub-agent-deployment-production.service
  systemctl daemon-reload
  systemd-analyze verify \
    /etc/systemd/system/remihub-backend-qa.service \
    /etc/systemd/system/remihub-agent-deployment-qa.service \
    /etc/systemd/system/remihub-agent-deployment-production.service

  echo "[7/10] Run complete QA validation before production promotion"
  "$RELEASE/deployments/agent_backend/qa-verify.sh" \
    "$RELEASE" \
    "$STAMP" \
    "$PG_DUMP_BINARY" \
    "$PG_RESTORE_BINARY" | tee "$BACKUP/qa-verification.log"
  /usr/local/libexec/remihub-backend-deployment-control \
    verify-runtime qa "$NEW_COMMIT" \
    >"$BACKUP/qa-runtime-final-state.txt"
  ! systemctl is-active --quiet remihub-backend-qa.service
  [[ ! -e /run/systemd/system/remihub-backend-qa.service.d/99-remihub-health-failure.conf ]]

  echo "[8/10] Promote the tested foundation commit and synchronize agent sources"
  systemctl stop remihub-agent-worker.service remihub-agent-implementation.service
  systemctl stop remihub.service
  runuser -u alex -- git -C "$PROD" \
    -c user.name='RemiHub Deployment' \
    -c user.email='remihub-deployment@invalid.local' \
    tag -a "rollback-before-backend-deployment-final-$STAMP" "$EXPECTED_BASE" \
    -m "Rollback before consolidated backend deployment installation $STAMP"
  runuser -u alex -- git -C "$PROD" reset --hard "$NEW_COMMIT"
  PROD_PROMOTED=1
  runuser -u remihub-agent -- env \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
    GIT_TERMINAL_PROMPT=0 \
    git --git-dir="$SOURCE" fetch --no-tags "$PROD" \
    "refs/heads/main:refs/remihub-install/$STAMP"
  runuser -u remihub-agent -- git --git-dir="$SOURCE" update-ref refs/heads/main "$NEW_COMMIT" "$EXPECTED_BASE"
  SOURCE_PROMOTED=1
  [[ "$(runuser -u alex -- git -C "$PROD" rev-parse refs/heads/main)" == "$NEW_COMMIT" ]]
  runuser -u alex -- env \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL="$GIT_SAFE_CONFIG" \
    GIT_TERMINAL_PROMPT=0 \
    git -C "$PLANNING" fetch --no-tags "$PROD" refs/heads/main
  runuser -u alex -- git -C "$PLANNING" reset --hard "$NEW_COMMIT"
  /usr/local/libexec/remihub-backend-deployment-control \
    harden-planning production "$NEW_COMMIT"
  PLANNING_PROMOTED=1

  echo "[9/10] Restart production and verify process/OpenAPI health"
  systemctl start remihub.service
  for _ in $(seq 1 30); do
    if systemctl is-active --quiet remihub.service && curl -fsS http://127.0.0.1:8000/openapi.json >"$BACKUP/production-openapi.json"; then
      break
    fi
    sleep 1
  done
  systemctl is-active --quiet remihub.service
  curl -fsS http://127.0.0.1:8000/openapi.json >/dev/null

  echo "[10/10] Final consistency checks and service restoration"
  [[ "$(runuser -u alex -- git -C "$PROD" rev-parse main)" == "$NEW_COMMIT" ]]
  [[ "$(runuser -u remihub-agent -- git --git-dir="$SOURCE" rev-parse refs/heads/main)" == "$NEW_COMMIT" ]]
  [[ "$(runuser -u alex -- git -C "$PLANNING" rev-parse HEAD)" == "$NEW_COMMIT" ]]
  [[ "$(runuser -u remihub-agent -- git -C "$PLANNING" rev-parse HEAD)" == "$NEW_COMMIT" ]]
  runuser -u remihub-agent -- /usr/bin/test -r "$PLANNING/backend/agent_worker.py"
  [[ "$(runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/qa/repository.git rev-parse qa-main)" == "$NEW_COMMIT" ]]
  [[ "$(runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/production/repository.git rev-parse production-main)" == "$NEW_COMMIT" ]]
  [[ -z "$(runuser -u alex -- git -C "$PROD" status --porcelain=v1 --untracked-files=no)" ]]
  [[ -z "$(runuser -u alex -- git -C "$PLANNING" status --porcelain=v1 --untracked-files=no)" ]]
  sudo -l -U remihub-deployer >"$BACKUP/remihub-deployer-sudo.txt"
  printf 'pg_dump=%s\npg_restore=%s\n' \
    "$PG_DUMP_BINARY" "$PG_RESTORE_BINARY" \
    >"$BACKUP/postgresql-client-paths.txt"
  if [[ "${ACTIVE[remihub-agent-worker.service]}" -eq 1 ]]; then
    systemctl start remihub-agent-worker.service
    wait_for_service_stable remihub-agent-worker.service
  fi
  if [[ "${ACTIVE[remihub-agent-implementation.service]}" -eq 1 ]]; then
    systemctl start remihub-agent-implementation.service
    wait_for_service_stable remihub-agent-implementation.service
  fi
  ! systemctl is-active --quiet remihub-agent-deployment-qa.service
  ! systemctl is-active --quiet remihub-agent-deployment-production.service
  cleanup_qa_frontend_bootstrap_worktree
  runuser -u alex -- git -C "$PROD" worktree remove --force "$STAGING"
  runuser -u alex -- git -C "$PROD" branch -D "$INSTALL_BRANCH" >/dev/null
  rm -rf "$STAGING"
  verify_critical_parent_state
  echo "CRITICAL_PARENT_POSTCHECK=PASS"
  printf 'installed_commit=%s\nrelease=%s\nbackup=%s\n' "$NEW_COMMIT" "$RELEASE" "$BACKUP" >"$BACKUP/INSTALL-SUCCEEDED.txt"
  COMPLETE=1
  trap - EXIT
  echo
  echo "Consolidated backend deployment foundation installed successfully."
  echo "Commit: $NEW_COMMIT"
  echo "Release: $RELEASE"
  echo "Rollback/install record: $BACKUP"
  echo "Deployment workers remain static and inactive until explicitly started."
)
