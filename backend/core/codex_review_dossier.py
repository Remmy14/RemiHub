"""Bounded real-card context from verified offline Git blobs and applied patch.

Selection is deterministic: complete changed pre/post images, ancestor instructions,
referenced sibling sources, and tests named for changed modules. All other paths are explicit
omissions. Context adequacy remains a mandatory reviewer decision.
"""
import base64
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import stat
import subprocess


def safe_path(name):
    if not isinstance(name, str) or not name or '\\' in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('Unsafe dossier path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {'', '.', '..', '.git'} for p in name.split('/')) or ':' in name:
        raise ValueError('Unsafe dossier path')
    name.encode('utf-8', errors='strict')
    return name


def read_source(root, name):
    safe_path(name)
    root = Path(root)
    path = root / name
    for parent in [path, *path.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError('Dossier path traverses symlink')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Dossier rejects links and special files')
    from backend.core.codex_review_bounded import MAX_BYTES
    if info.st_size > MAX_BYTES:
        raise ValueError('Complete changed/supporting source exceeds budget')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError('Source exceeds budget')
    return content, 0o100755 if info.st_mode & 0o111 else 0o100644


def object_hash(content, algorithm):
    return hashlib.new(algorithm, b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


def source_selection(repository, root, base):
    """Compare all postimages with verified base manifest, not patch path parsing.

    Renames are represented losslessly as deleted old and added new paths. This
    also covers mode changes, copies, deletions and untracked additions.
    """
    after = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_dir() and not path.is_symlink():
            continue
        name = safe_path(path.relative_to(root).as_posix())
        # Hash streamed bytes without decoding unrelated binary assets.
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe candidate file')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after[name] = {'sha256': digest,
                       'mode': 0o100755 if info.st_mode & 0o111 else 0o100644,
                       'size': info.st_size}
    changed = sorted(n for n in base.keys() | after.keys() if
                     {k: base.get(n, {}).get(k) for k in ('sha256', 'mode', 'size')} != after.get(n))
    if not changed:
        raise ValueError('Empty candidate change')
    supporting = set()
    references = set()
    for name in changed:
        if name in after:
            content, _ = read_source(root, name)
            references.update(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', content.decode('utf-8')))

    for name in changed:
        parent = PurePosixPath(name).parent
        for ancestor in [parent, *parent.parents]:
            instruction = str(ancestor / 'AGENTS.md')
            if instruction in after:
                supporting.add(instruction)
        for other in after:
            p = PurePosixPath(other)
            if (p.parent == parent and p.stem in references) or p.name == 'test_' + PurePosixPath(name).stem + '.py':
                supporting.add(other)
    supporting -= set(changed)
    # Never silently trim supporting candidates to fit the limit.
    from backend.core.codex_review_bounded import MAX_FILES, MAX_BYTES
    if len(changed) * 2 + len(supporting) > MAX_FILES:
        raise ValueError('Complete source selection exceeds file budget')
    sources = []
    budget = 0
    def append(name, version, role, content, mode):
        nonlocal budget
        budget += len(content)
        if budget > MAX_BYTES or b'\0' in content:
            raise ValueError('Binary or oversized selected source')
        text = content.decode('utf-8', errors='strict')
        sources.append({'path': name, 'version': version, 'role': role, 'mode': mode,
                        'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
                        'bytes_b64': base64.b64encode(content).decode(), 'text': text})
    for name in changed:
        if name in base:
            entry = base[name]
            if entry['size'] + budget > MAX_BYTES:
                raise ValueError('Complete changed base source exceeds budget')
            result = subprocess.run(['git', '-c', 'safe.directory=' + str(repository), '-C', str(repository), 'cat-file', 'blob', entry['oid']],
                env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_GLOBAL': '/dev/null',
                     'GIT_CONFIG_NOSYSTEM': '1', 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1'},
                check=True, capture_output=True, timeout=120)
            content = result.stdout
            if object_hash(content, 'sha1' if len(entry['oid']) == 40 else 'sha256') != entry['oid']:
                raise ValueError('Git blob hash mismatch')
            append(name, 'base', 'changed', content, entry['mode'])
            sources[-1]['git_blob_oid'] = entry['oid']
        if name in after:
            append(name, 'candidate', 'changed', *read_source(root, name))
    for name in sorted(supporting):
        append(name, 'candidate', 'supporting', *read_source(root, name))
    omitted = [{'path': n, **after[n]} for n in sorted(after.keys() - set(changed) - supporting)]
    return {'base_manifest_sha256': hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest(),
            'policy': 'changed-pre-post+ancestor-instructions+referenced-siblings+named-tests-v1',
            'changed_paths': changed, 'sources': sources, 'omitted': omitted,
            'deleted_paths': sorted(set(changed) - after.keys()),
            'coverage': 'partial; reviewer must block if omitted context is necessary'}


def selected_dossier(root, context, raw, selection):
    from backend.core.codex_review_bounded import MAX_BYTES, MAX_FILES, MAX_WIRE_BYTES, sha
    sources = selection['sources']
    if not sources or len(sources) > MAX_FILES:
        raise ValueError('Insufficient source selection')
    budget = len(raw) + len(json.dumps(context).encode())
    for entry in sources:
        safe_path(entry['path'])
        content = base64.b64decode(entry['bytes_b64'], validate=True)
        budget += len(content)
        if (b'\0' in content or len(content) != entry['size'] or sha(content) != entry['sha256']
            or content.decode('utf-8') != entry['text']):
            raise ValueError('Invalid complete source')
        if entry['version'] == 'candidate' and read_source(root, entry['path']) != (content, entry['mode']):
            raise ValueError('Frozen source changed')
    if budget > MAX_BYTES:
        raise ValueError('Complete dossier exceeds budget')
    payload = {'format': 'remihub-real-card-review-v2', 'complete_tree': False,
               'context': context, 'raw_diff_b64': base64.b64encode(raw).decode(),
               'raw_diff_sha256': sha(raw), **selection}
    wire = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    if len(wire.encode()) > MAX_WIRE_BYTES:
        raise ValueError('Complete encoded dossier exceeds budget')
    return wire
