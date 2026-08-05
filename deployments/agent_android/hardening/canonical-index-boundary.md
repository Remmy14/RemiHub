# Canonical Android Git index boundary

The protected Android deployment service must normalize the canonical Android
checkout index before every deployment worker invocation:

```ini
[Service]
ExecStartPre=+/usr/local/libexec/remihub-android-canonical-index-preflight
```

The preflight must run as root through systemd, but every Git inspection of
`/opt/remihub-android` must run as `alex`. It may change only metadata on the
single verified `/opt/remihub-android/.git/index` inode. It must preserve the
index bytes, require owner `alex`, group `storage`, and mode `0600`, and reject
a dirty canonical Android worktree.

Do not run `git status`, `git reset`, `git fetch`, or other worktree Git commands
against `/opt/remihub-android` as root. Root-run Git can refresh the index and
recreate the deployment failure this guard is designed to prevent.
