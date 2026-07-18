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
  PROD_TARGET_EXISTED=0
  PROD_DEPLOYMENT_EXISTED=0
  PROD_PROMOTED=0
  SOURCE_PROMOTED=0
  PLANNING_PROMOTED=0
  SYSTEM_INSTALLED=0
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
  QA_PARENT_EXISTED=0
  QA_PARENT_OWNER=""
  QA_PARENT_MODE=""
  CONFIG_PARENT_EXISTED=0
  CONFIG_PARENT_OWNER=""
  CONFIG_PARENT_MODE=""

  required_commands=(
    git systemctl systemd-analyze runuser install sha256sum tar sed curl visudo
    getent groupadd useradd usermod userdel groupdel find seq sudo python3 psql
    stat journalctl
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
  )

  backup_system_paths() {
    mkdir -p "$BACKUP/system-root"
    : >"$BACKUP/system-absent.txt"
    for path in "${SYSTEM_PATHS[@]}"; do
      if [[ -e "$path" || -L "$path" ]]; then
        cp -a --parents "$path" "$BACKUP/system-root"
      else
        printf '%s\n' "$path" >>"$BACKUP/system-absent.txt"
      fi
    done
  }

  restore_system_paths() {
    for path in "${SYSTEM_PATHS[@]}"; do
      rm -rf -- "$path"
    done
    if [[ -d "$BACKUP/system-root" ]]; then
      cp -a "$BACKUP/system-root/." /
    fi
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
    local existed="${2:?existence flag required}"
    local owner="${3:-}"
    local mode="${4:-}"

    if [[ "$existed" -eq 0 ]]; then
      rm -rf -- "$path"
      return 0
    fi

    [[ -d "$path" ]] || mkdir -p "$path"
    chown "$owner" "$path"
    chmod "$mode" "$path"
  }

  rollback() {
    local status=$?
    trap - EXIT
    if [[ "$COMPLETE" -eq 1 ]]; then exit "$status"; fi
    echo "Installation failed; restoring the pre-install state from $BACKUP" >&2
    set +e
    systemctl stop remihub-agent-deployment-qa.service remihub-agent-deployment-production.service remihub-backend-qa.service
    if [[ "$PLANNING_PROMOTED" -eq 1 ]]; then
      runuser -u alex -- git -C "$PLANNING" reset --hard "$EXPECTED_BASE"
      /usr/local/libexec/remihub-backend-deployment-control \
        harden-planning production "$EXPECTED_BASE" 2>/dev/null || true
    fi
    if [[ "$SOURCE_PROMOTED" -eq 1 ]]; then
      runuser -u remihub-agent -- git --git-dir="$SOURCE" update-ref refs/heads/main "$EXPECTED_BASE" "$NEW_COMMIT"
    fi
    if [[ "$PROD_PROMOTED" -eq 1 ]]; then
      systemctl stop remihub.service
      runuser -u alex -- git -C "$PROD" reset --hard "$EXPECTED_BASE"
    fi
    if [[ -f "$BACKUP/qa-repository.tar" ]]; then
      rm -rf /opt/remihub-agent/deployment/qa/repository.git
      tar -C / -xf "$BACKUP/qa-repository.tar"
    elif [[ -n "$OLD_QA" && -d /opt/remihub-agent/deployment/qa/repository.git ]]; then
      runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/qa/repository.git update-ref refs/heads/qa-main "$OLD_QA"
    fi
    rm -rf /opt/remihub-agent/deployment/qa/application
    if [[ -f "$BACKUP/qa-application.tar" ]]; then
      tar -C / -xf "$BACKUP/qa-application.tar"
    fi
    if [[ "$PROD_DEPLOYMENT_EXISTED" -eq 0 ]]; then
      rm -rf /opt/remihub-agent/deployment/production
    elif [[ -f "$BACKUP/production-deployment.tar" ]]; then
      rm -rf /opt/remihub-agent/deployment/production
      tar -C / -xf "$BACKUP/production-deployment.tar"
    elif [[ -n "$OLD_PROD_TARGET" ]]; then
      runuser -u remihub-deployer -- git --git-dir=/opt/remihub-agent/deployment/production/repository.git update-ref refs/heads/production-main "$OLD_PROD_TARGET"
    fi
    rm -rf "/opt/remihub-agent/deployment/qa/artifacts/install-verification-$STAMP"
    [[ -n "$RELEASE" ]] && rm -rf "$RELEASE"
    [[ "$SYSTEM_INSTALLED" -eq 1 ]] && restore_system_paths
    chown -R remihub-agent:remihub-agent /opt/remihub-agent/deployment/qa/repository.git /opt/remihub-agent/deployment/qa/worktrees /opt/remihub-agent/deployment/qa/artifacts 2>/dev/null
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
    if [[ "$QA_USER_CREATED" -eq 1 ]]; then userdel remihub-qa-app 2>/dev/null || true; fi
    if [[ "$QA_GROUP_CREATED" -eq 1 ]]; then groupdel remihub-qa-app 2>/dev/null || true; fi
    if [[ "$DEPLOYER_USER_CREATED" -eq 1 ]]; then userdel remihub-deployer 2>/dev/null || true; fi
    if [[ "$DEPLOYER_GROUP_CREATED" -eq 1 ]]; then groupdel remihub-deployer 2>/dev/null || true; fi
    runuser -u remihub-agent -- git --git-dir="$SOURCE" update-ref -d "refs/remihub-install/$STAMP" 2>/dev/null || true
    if [[ -d "$STAGING" ]]; then
      runuser -u alex -- git -C "$PROD" worktree remove --force "$STAGING" 2>/dev/null || true
    fi
    runuser -u alex -- git -C "$PROD" worktree prune 2>/dev/null || true
    runuser -u alex -- git -C "$PROD" branch -D "$INSTALL_BRANCH" 2>/dev/null || true
    restart_original_services
    rm -rf "$STAGING"
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
  DEPLOYMENT_PARENT_OWNER="$(stat -c '%u:%g' /opt/remihub-agent/deployment)"
  DEPLOYMENT_PARENT_MODE="$(stat -c '%a' /opt/remihub-agent/deployment)"
  printf 'owner=%s\nmode=%s\n' \
    "$DEPLOYMENT_PARENT_OWNER" "$DEPLOYMENT_PARENT_MODE" \
    >"$BACKUP/deployment-parent-before.txt"

  if [[ -d /opt/remihub-agent/deployment/qa ]]; then
    QA_PARENT_EXISTED=1
    QA_PARENT_OWNER="$(stat -c '%u:%g' /opt/remihub-agent/deployment/qa)"
    QA_PARENT_MODE="$(stat -c '%a' /opt/remihub-agent/deployment/qa)"
  fi
  printf 'existed=%s\nowner=%s\nmode=%s\n' \
    "$QA_PARENT_EXISTED" "$QA_PARENT_OWNER" "$QA_PARENT_MODE" \
    >"$BACKUP/qa-parent-before.txt"

  if [[ -d /opt/remihub-agent/deployment/config ]]; then
    CONFIG_PARENT_EXISTED=1
    CONFIG_PARENT_OWNER="$(stat -c '%u:%g' /opt/remihub-agent/deployment/config)"
    CONFIG_PARENT_MODE="$(stat -c '%a' /opt/remihub-agent/deployment/config)"
  fi
  printf 'existed=%s\nowner=%s\nmode=%s\n' \
    "$CONFIG_PARENT_EXISTED" "$CONFIG_PARENT_OWNER" "$CONFIG_PARENT_MODE" \
    >"$BACKUP/config-parent-before.txt"
  backup_system_paths
  SYSTEM_INSTALLED=1
  if [[ -d /opt/remihub-agent/deployment/qa/application ]]; then
    tar -C / -cf "$BACKUP/qa-application.tar" opt/remihub-agent/deployment/qa/application
  fi
  OLD_QA="$(git --git-dir=/opt/remihub-agent/deployment/qa/repository.git rev-parse qa-main^{commit})"
  printf '%s\n' "$OLD_QA" >"$BACKUP/qa-target-before.txt"
  git --git-dir=/opt/remihub-agent/deployment/qa/repository.git bundle create "$BACKUP/qa-target.bundle" --all
  tar -C / -cf "$BACKUP/qa-repository.tar" opt/remihub-agent/deployment/qa/repository.git
  if [[ -d /opt/remihub-agent/deployment/production ]]; then
    PROD_DEPLOYMENT_EXISTED=1
    tar -C / -cf "$BACKUP/production-deployment.tar" opt/remihub-agent/deployment/production
  fi
  if [[ -d /opt/remihub-agent/deployment/production/repository.git ]]; then
    PROD_TARGET_EXISTED=1
    OLD_PROD_TARGET="$(git --git-dir=/opt/remihub-agent/deployment/production/repository.git rev-parse production-main^{commit})"
    git --git-dir=/opt/remihub-agent/deployment/production/repository.git bundle create "$BACKUP/production-target.bundle" --all
  fi

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
  chown -R remihub-deployer:remihub-agent \
    /opt/remihub-agent/deployment/qa/repository.git \
    /opt/remihub-agent/deployment/qa/worktrees \
    /opt/remihub-agent/deployment/qa/artifacts
  chmod 0750 /opt/remihub-agent/deployment/qa/repository.git /opt/remihub-agent/deployment/qa/worktrees /opt/remihub-agent/deployment/qa/artifacts
  install -d -o remihub-qa-app -g remihub-qa-app -m 0750 /opt/remihub-agent/deployment/qa/logs
  install -d -o remihub-deployer -g root -m 0750 \
    /var/backups/remihub-agent/backend-deployments/qa \
    /var/backups/remihub-agent/backend-deployments/production

  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/worker-config/qa-worker.ini /opt/remihub-agent/deployment/config/qa-worker.ini
  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/worker-config/prod-worker.ini /opt/remihub-agent/deployment/config/prod-worker.ini
  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/config/qa-migrator.ini /opt/remihub-agent/deployment/config/qa-migrator.ini
  install -o root -g remihub-deployer -m 0640 /opt/remihub-agent/config/prod-migrator.ini /opt/remihub-agent/deployment/config/prod-migrator.ini
  install -o root -g remihub-qa-app -m 0640 /opt/remihub-agent/config/qa-app.ini /opt/remihub-agent/deployment/config/qa-app.ini
  install -o root -g remihub-qa-app -m 0640 "$ASSETS/validation-support/application.ini" /opt/remihub-agent/deployment/config/qa-application.ini
  install -o root -g root -m 0644 \
    "$ASSETS/gitconfig/remihub-backend-deployment.gitconfig" \
    "$GIT_SAFE_CONFIG"

  echo "Validating deployment account traversal before repository seeding"
  require_account_path remihub-deployer -x /opt/remihub-agent/deployment
  require_account_path remihub-deployer -x /opt/remihub-agent/deployment/qa
  require_account_path remihub-deployer -x /opt/remihub-agent/deployment/config
  require_account_path remihub-deployer -r /opt/remihub-agent/deployment/config/qa-worker.ini
  require_account_path remihub-deployer -r /opt/remihub-agent/deployment/config/qa-migrator.ini
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
  install -o root -g root -m 0755 "$ASSETS/libexec/remihub-backend-validation-sandbox" /usr/local/libexec/remihub-backend-validation-sandbox
  install -o root -g root -m 0755 "$ASSETS/libexec/remihub-backend-qa-server" /usr/local/libexec/remihub-backend-qa-server
  rm -rf /usr/local/libexec/remihub-backend-validation-support
  install -d -o root -g root -m 0755 /usr/local/libexec/remihub-backend-validation-support
  install -o root -g root -m 0644 "$ASSETS/validation-support/"* /usr/local/libexec/remihub-backend-validation-support/
  install -o root -g root -m 0440 "$ASSETS/sudoers/remihub-backend-deployment" /etc/sudoers.d/remihub-backend-deployment
  visudo -cf /etc/sudoers.d/remihub-backend-deployment

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
  chown -R remihub-deployer:remihub-agent /opt/remihub-agent/deployment/production/repository.git
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
  runuser -u alex -- git -C "$PROD" worktree remove --force "$STAGING"
  runuser -u alex -- git -C "$PROD" branch -D "$INSTALL_BRANCH" >/dev/null
  rm -rf "$STAGING"
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
