#!/usr/bin/env python3
"""Run an offline PostgreSQL/isolation smoke check in disposable fixtures only.

Run as the unprivileged implementation account. Only the launcher's fixture
root/repository constants are redirected; isolation and bootstrap are unchanged.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
SOURCE = Path(__file__).with_name('backend_validation_sandbox.py')
EXPECTED = '1580e3ff3120bb9a13d4ff725bb4a74d5b7414ecaca4072b42219d791cf1eee8'
if os.geteuid() == 0:
    raise SystemExit('Run as an unprivileged implementation account')
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != EXPECTED:
    raise SystemExit('Source differs from reviewed sandbox repair')
spec = importlib.util.spec_from_file_location('sandbox_under_test', SOURCE)
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)
with tempfile.TemporaryDirectory(prefix='remihub-sandbox-regression-') as tmp:
    root = Path(tmp)
    worktrees = root / 'worktrees'
    fixture = worktrees / 'fixture'
    fixture.mkdir(parents=True)
    (fixture / 'readonly.txt').write_text('fixture')
    repository = root / 'repository.git'
    repository.mkdir()
    # This host-only file must not become visible in the sandbox.
    host_marker = root / 'not-mounted.txt'
    host_marker.write_text('synthetic sentinel; not a credential')
    sandbox.WORKTREE_ROOT = worktrees
    sandbox.REPOSITORY = repository
    probe = r'''
import errno, json, os, pathlib, pwd, grp, socket, subprocess, sys, tempfile
assert pwd.getpwuid(os.getuid()).pw_name == 'remihub-validator'
assert grp.getgrgid(os.getgid()).gr_name == 'remihub-validator'
assert not pathlib.Path(sys.argv[1]).exists(), 'host file exposed'
for path in ['/opt/remihub-agent/config', '/opt/remihub/.env', '/home/alex/.ssh']:
    assert not pathlib.Path(path).exists(), path
try:
    pathlib.Path('readonly.txt').write_text('unexpected write')
except OSError as exc:
    assert exc.errno == errno.EROFS, str(exc)
else:
    raise AssertionError('source mount is writable')
with socket.socket() as sock:
    sock.settimeout(1)
    assert sock.connect_ex(('1.1.1.1',443)) != 0, 'external network reachable'
with tempfile.TemporaryDirectory(prefix='private-pg-') as tmp:
    root=pathlib.Path(tmp);data=root/'data';sock=root/'socket';sock.mkdir()
    tools=pathlib.Path('/usr/lib/postgresql/16/bin')
    def run(*args):
        result=subprocess.run(list(args),capture_output=True,text=True,timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        return result.stdout
    run(str(tools/'initdb'),'-D',str(data),'-U','fixture','--auth=trust','--no-locale','--encoding=UTF8')
    try:
        run(str(tools/'pg_ctl'),'-D',str(data),'-l',str(root/'log'),'-o',f"-F -k {sock} -p 55493 -c listen_addresses=''",'-w','start')
        assert run(str(tools/'psql'),'-h',str(sock),'-p','55493','-U','fixture','-d','postgres','-Atc','SELECT 6*7').strip()=='42'
    finally:
        if (data/'postmaster.pid').exists():
            run(str(tools/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop')
print(json.dumps({'synthetic_identity':'passed','host_file_exclusion':'passed',
'credential_paths_excluded':'passed','readonly_source':'passed',
'external_network_denied':'passed','postgres_init_start_query_stop':'passed'}))
'''
    result = subprocess.run(sandbox.sandbox_command(
        fixture, [str(sandbox.PYTHON), '-c', probe, str(host_marker)]),
        capture_output=True, text=True, timeout=120)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
