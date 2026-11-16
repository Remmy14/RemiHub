#!/usr/bin/env bash
# Operator-only publisher. Default is a read-only identity check.
set -euo pipefail
mode=${1:---check}
[[ $# -le 1 && ( "$mode" == --check || "$mode" == --install ) ]] || {
  echo 'Usage: install-sandbox.sh [--check|--install]' >&2; exit 2;
}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_file="$source_dir/backend_validation_sandbox.py"
target_dir=/opt/remihub-agent/validators
target="$target_dir/backend_validation_sandbox.py"
expected=1580e3ff3120bb9a13d4ff725bb4a74d5b7414ecaca4072b42219d791cf1eee8
predecessor=ae5aa1e84c020c24d3980218ebf04799493cb989fa5b9f6b2fe5d089c652857d
fail() { echo "$*" >&2; exit 1; }
[[ -f "$source_file" && ! -L "$source_file" ]] || fail 'Unsafe source file'
[[ $(sha256sum "$source_file" | cut -d ' ' -f1) == "$expected" ]] || fail 'Source identity differs from reviewed repair'
for path in /opt /opt/remihub-agent "$target_dir"; do
  [[ -d "$path" && ! -L "$path" ]] || fail 'Unsafe installation directory'
done
[[ -f "$target" && ! -L "$target" ]] || fail 'Existing regular validator required'
[[ $(stat -c '%u:%g:%a:%h' "$target") == 0:0:755:1 ]] || fail 'Unexpected validator ownership, mode, or links'
actual=$(sha256sum "$target" | cut -d ' ' -f1)
if [[ "$actual" == "$expected" ]]; then
  echo "Validator already matches reviewed source: $expected"
  exit 0
fi
[[ "$mode" == --install ]] || fail "Installed validator differs: $actual"
[[ $EUID -eq 0 ]] || fail 'Installation requires root and operator authorization'
[[ "$actual" == "$predecessor" ]] || fail 'Refusing to replace an unknown or newer validator'
for path in /opt /opt/remihub-agent "$target_dir"; do
  [[ $(stat -c '%u' "$path") == 0 ]] || fail 'Installation directory is not root-owned'
  permissions=$(stat -c '%a' "$path")
  (( (8#$permissions & 0022) == 0 )) || fail 'Installation directory is writable by non-root'
done
# The approved recovery procedure must first establish idle implementation workers.
# This script does not stop workers, change refs, restart services, or deploy code.
backup=$(mktemp -d /var/backups/remihub-sandbox-XXXXXXXX)
chmod 0700 "$backup"
cp -p -- "$target" "$backup/backend_validation_sandbox.py"
staged=$(mktemp "$target_dir/.sandbox-XXXXXXXX")
trap 'rm -f -- "$staged"' EXIT
install -o root -g root -m 0755 -- "$source_file" "$staged"
[[ $(sha256sum "$staged" | cut -d ' ' -f1) == "$expected" ]] || fail 'Staged source identity changed'
[[ $(sha256sum "$target" | cut -d ' ' -f1) == "$predecessor" ]] || fail 'Validator changed during preparation'
mv -T -- "$staged" "$target"
echo "Installed $expected; previous file retained at $backup/backend_validation_sandbox.py"
