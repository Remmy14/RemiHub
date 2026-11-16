#!/usr/bin/env bash
# Read-only source/runtime identity check. This script never installs files.
set -euo pipefail
[[ $# -eq 0 ]] || {
  echo 'Usage: check-sandbox.sh (read-only; no installation option)' >&2; exit 2;
}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_file="$source_dir/backend_validation_sandbox.py"
target_dir=/opt/remihub-agent/validators
target="$target_dir/backend_validation_sandbox.py"
expected=1580e3ff3120bb9a13d4ff725bb4a74d5b7414ecaca4072b42219d791cf1eee8
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
fail "Installed validator differs: $actual; stop for a separately reviewed operator procedure"
