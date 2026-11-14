"""Immutable, credential-filtered source snapshots; no model-controlled filesystem IO."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType

MAX_RESULT = 16000
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
PROTECTED_PARTS = frozenset({'.git', '.ssh', '.aws', '.codex', 'credentials', 'secrets', 'node_modules', '.venv', '.docker', '.kube', '.gnupg'})
PROTECTED_FILES = frozenset({'.npmrc', '.yarnrc', '.yarnrc.yml', '.netrc', '_netrc', '.pypirc',
    '.git-credentials', '.gitconfig', 'auth.json', 'service-account.json',
    'config.ini', 'database.ini', 'key.properties', 'keystore.properties', 'gradle.properties', 'local.properties', 'google-services.json'})


def safe_path(name: str) -> str:
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
        or PurePosixPath(name).is_absolute()
        or any(p in {'', '.', '..'} for p in name.split('/'))):
        raise ValueError('Invalid source path')
    return name


def protected(name: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(name).parts)
    return (bool(set(parts) & PROTECTED_PARTS)
            or any(p == '.env' or p.startswith('.env.') for p in parts)
            or parts[-1].lower().endswith(('.pem', '.key', '.p12', '.pfx', '.keystore', '.jks'))
            or parts[-1] in PROTECTED_FILES)


def _number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Source range exceeds bounds')
    return value


class SourceSnapshot:
    """Capture private exported tree once; subsequent tools operate only on bytes.

    The exporter owns the tree. Reject links/special files before opening. Model
    calls cannot substitute paths or observe subsequent on-disk modifications.
    """
    def __init__(self, root: Path):
        root = Path(root).absolute()
        if root.is_symlink() or root.resolve(strict=True) != root:
            raise ValueError('Snapshot root must not traverse symlinks')
        files, manifest, total = {}, {}, 0
        for path in sorted(root.rglob('*')):
            name = safe_path(path.relative_to(root).as_posix())
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('Snapshot contains symlink')
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Snapshot contains nonregular source')
            total += info.st_size
            if total > MAX_SNAPSHOT_BYTES:
                raise ValueError('Source snapshot exceeds operational memory budget')
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
                content = stream.read(MAX_SNAPSHOT_BYTES - (total - info.st_size) + 1)
            if len(content) != info.st_size:
                raise ValueError('Snapshot changed during capture')
            mode = 0o100755 if info.st_mode & 0o111 else 0o100644
            manifest[name] = MappingProxyType({'sha256': hashlib.sha256(content).hexdigest(),
                                               'size': len(content), 'mode': mode})
            if not protected(name):
                files[name] = (content, mode)
        self.files = MappingProxyType(files)
        self.manifest = MappingProxyType(manifest)


def changed_manifest(base: dict, snapshot: SourceSnapshot) -> list[dict]:
    after = snapshot.manifest
    changed = []
    for name in sorted(base.keys() | after.keys()):
        before = {k: base.get(name, {}).get(k) for k in ('sha256', 'size', 'mode')}
        current = dict(after[name]) if name in after else None
        if before != current:
            if protected(name):
                raise ValueError('Changed protected source cannot be reviewed')
            changed.append({'file': name, 'before': before if name in base else None, 'after': current})
    if not changed:
        raise ValueError('Review candidate has no changes')
    return changed


class SourceInspector:
    """One bounded read-only tool over authorized snapshots and verified diff."""
    def __init__(self, snapshots: dict[str, SourceSnapshot], diff: bytes):
        self.snapshots = MappingProxyType(dict(snapshots))
        self.diff = bytes(diff)
        self.calls = []
        self._diff_offset = 0

    def next_diff_chunk(self):
        """Host-deliver any missing contiguous verified diff before approval."""
        if self._diff_offset >= len(self.diff.decode('utf-8', errors='replace')):
            return None
        return self.inspect(operation='diff', offset=self._diff_offset)

    def inspect(self, *, operation, repository='candidate', path='', offset=0,
                limit=100, query=''):
        offset = _number(offset, 0, 2**31 - 1)
        limit = _number(limit, 1, 100)
        if operation == 'diff':
            data = self.diff.decode('utf-8', errors='replace')
            end = min(offset + MAX_RESULT, len(data))
            if offset <= self._diff_offset < end:
                self._diff_offset = end
            result = {'text': data[offset:end],
                      'next_offset': offset + MAX_RESULT if offset + MAX_RESULT < len(data) else None,
                      'sha256': hashlib.sha256(self.diff).hexdigest()}
        else:
            if repository not in self.snapshots:
                raise ValueError('Repository snapshot is not authorized')
            files = self.snapshots[repository].files
            if path:
                safe_path(path)
                if protected(path):
                    raise ValueError('Protected source is unavailable')
            if operation == 'list':
                prefix = path + '/' if path else ''
                entries = sorted({name[len(prefix):].split('/')[0] for name in files if name.startswith(prefix)})
                selected, used = [], 0
                for entry in entries[offset:offset + limit]:
                    size = len(json.dumps(entry))
                    if used + size > MAX_RESULT: break
                    selected.append(entry); used += size
                end = offset + len(selected)
                result = {'entries': selected, 'next_offset': end if end < len(entries) else None}
            elif operation == 'read':
                if path not in files:
                    raise ValueError('Source file does not exist in snapshot')
                content = files[path][0]
                text = content.decode('utf-8', errors='replace')
                result = {'text': text[offset:offset + MAX_RESULT],
                          'next_offset': offset + MAX_RESULT if offset + MAX_RESULT < len(text) else None,
                          'sha256': hashlib.sha256(content).hexdigest()}
            elif operation == 'search':
                if not isinstance(query, str) or not 1 <= len(query) <= 500:
                    raise ValueError('Search requires 1-500 literal characters')
                hits, seen, used, more = [], 0, 0, False
                for name, (content, _) in files.items():
                    if path and name != path and not name.startswith(path + '/'): continue
                    if b'\0' in content: continue
                    for line, text in enumerate(content.decode('utf-8', errors='replace').splitlines(), 1):
                        if query not in text: continue
                        if seen < offset:
                            seen += 1; continue
                        hit = {'file': name, 'line': line, 'text': text[:1000]}
                        size = len(json.dumps(hit))
                        if len(hits) >= limit or used + size > MAX_RESULT:
                            more = True; break
                        hits.append(hit); seen += 1; used += size
                    if more: break
                result = {'matches': hits, 'next_offset': offset + len(hits) if more else None}
            else:
                raise ValueError('Unknown source operation')
        self.calls.append({'operation': operation, 'repository': repository, 'path': path,
                           'offset': offset, 'limit': limit})
        return result


SOURCE_INTERFACE = (
    "Read immutable authorized source: list directories, read file chunks, search literal text, "
    "or read verified diff chunks. Offsets count characters for read/diff and entries for list/search. "
    "Limit is 1-100 entries for list/search; read/diff always return at most 16000 characters. "
    "Follow next_offset until null. No shell, writes, or live host access."
)
