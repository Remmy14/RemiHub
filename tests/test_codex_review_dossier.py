"""Real-card selection regression tests; disposable Git and offline objects only."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from backend.core.codex_review import _freeze_base, build_patch
from backend.core.codex_review_bounded import dossier, Runtime
from backend.core.codex_review_dossier import source_selection, safe_path


class RealDossierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        for name, text in {'AGENTS.md': 'Offline only\n', 'src/old.py': 'value = 1\n',
                           'src/delete.py': 'deleted = True\n', 'unrelated/large.dat': 'x' * 600000}.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').decode().strip()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True).stdout

    def selected(self):
        frozen = self.root / 'frozen'
        frozen.mkdir()
        manifest = _freeze_base(self.repo, self.base, frozen)
        raw = build_patch(self.repo)
        subprocess.run(['git', '-C', str(frozen), 'apply', '--no-index', '--binary', '-'],
                       input=raw, check=True, capture_output=True)
        selection = source_selection(self.repo, frozen, manifest)
        prompt = json.dumps({'source_selection': selection, 'raw_diff_b64': base64.b64encode(raw).decode(),
                             'validation': [{'status': 'passed'}]})
        return json.loads(dossier(frozen, prompt)), raw

    def test_rename_delete_untracked_complete_images_and_explicit_omission(self):
        self.git('mv', 'src/old.py', 'src/new.py')
        (self.repo / 'src/delete.py').unlink()
        (self.repo / 'src/untracked.py').write_bytes(b'untracked = True\r\n')
        value, raw = self.selected()
        self.assertFalse(value['complete_tree'])
        self.assertEqual(base64.b64decode(value['raw_diff_b64']), raw)
        sources = {(s['path'], s['version']): s for s in value['sources']}
        self.assertEqual(sources['src/old.py', 'base']['text'], 'value = 1\n')
        self.assertEqual(sources['src/new.py', 'candidate']['text'], 'value = 1\n')
        self.assertEqual(sources['src/delete.py', 'base']['text'], 'deleted = True\n')
        self.assertEqual(sources['src/untracked.py', 'candidate']['text'], 'untracked = True\r\n')
        self.assertIn(('AGENTS.md', 'candidate'), sources)
        self.assertEqual([s['path'] for s in value['omitted']], ['unrelated/large.dat'])

    def test_binary_and_oversize_changes_fail_closed(self):
        for content in (b'\0binary', b'\xff', b'x' * 600000):
            with self.subTest(content_length=len(content)):
                (self.repo / 'src/old.py').write_bytes(content)
                with self.assertRaises((ValueError, UnicodeError)):
                    self.selected()
                import shutil
                shutil.rmtree(self.root / 'frozen')

    def test_mode_only_change_is_complete(self):
        (self.repo / 'src/old.py').chmod(0o755)
        value, _ = self.selected()
        entries = [s for s in value['sources'] if s['path'] == 'src/old.py']
        self.assertEqual([s['mode'] for s in entries], [0o100644, 0o100755])

    def test_unrelated_large_blob_is_never_read_into_memory(self):
        (self.repo / 'src/old.py').write_text('value = 2\n')
        original = Path.read_bytes
        def guarded(path):
            if path.name == 'large.dat':
                raise AssertionError('Unrelated assets must be streamed')
            return original(path)
        with patch.object(Path, 'read_bytes', guarded):
            value, _ = self.selected()
        self.assertIn('unrelated/large.dat', [entry['path'] for entry in value['omitted']])

    def test_hostile_paths_block(self):
        for name in ('../x', '/tmp/x', 'a/../x', 'a/.git/config', 'a\\x', 'a\nx', 'a//x', './x', 'C:x'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_path(name)

    def test_symlink_candidate_and_unsupported_pin_block(self):
        (self.repo / 'src/link.py').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            self.selected()
        with self.assertRaisesRegex(ValueError, 'hash'):
            Runtime('/usr/bin/true', 'gpt-6-sol')
        with self.assertRaisesRegex(ValueError, 'model'):
            Runtime('/usr/bin/true', 'fallback')

    def test_executor_real_selection_interface(self):
        from tests.test_codex_review import SyntheticCandidate
        fixture = SyntheticCandidate('test_first_pass_is_ready_but_requires_human_approval')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.gateway.requires_raw_diff = True
        fixture.gateway.requires_real_dossier = True
        def review(**kwargs):
            value = json.loads(dossier(kwargs['repository_path'], kwargs['prompt']))
            self.assertIn('new.py', value['changed_paths'])
            return fixture.passing_turn(**kwargs)
        fixture.gateway.run_turn.side_effect = review
        result = fixture.executor.execute(fixture.claim)
        self.assertEqual(result.metadata['independent_review']['verdict'], 'pass')

class WorkerIntegrationTests(unittest.TestCase):
    def test_actual_worker_uses_bounded_gateway_and_requires_home_and_model(self):
        import os
        from backend.agent_worker import AgentWorkerSettings, build_executor
        from backend.core.agent_worker import AgentWorkerConfigurationError
        from backend.core.codex_review_bounded import BoundedReviewGateway
        with tempfile.TemporaryDirectory() as home:
            env = {'REMIHUB_AGENT_EXECUTOR': 'codex-review',
                   'REMIHUB_AGENT_ARTIFACT_ROOT': home,
                   'REMIHUB_AGENT_REPOSITORY_SCOPE': 'backend',
                   'REMIHUB_REVIEW_CODEX_BIN': '/usr/bin/true',
                   'REMIHUB_REVIEW_CODEX_HOME': home,
                   'REMIHUB_CODEX_MODEL': 'gpt-6-sol'}
            with patch.dict(os.environ, env, clear=True):
                executor = build_executor(AgentWorkerSettings.from_environment())
                self.assertIsInstance(executor.gateway, BoundedReviewGateway)
                self.assertEqual(executor.gateway.qualifier.__module__, 'backend.core.codex_review_qualification')
                executor.cancel(None)
                self.assertTrue(executor.gateway._cancelled)
            for key in ('REMIHUB_REVIEW_CODEX_HOME', 'REMIHUB_CODEX_MODEL', 'REMIHUB_REVIEW_CODEX_BIN'):
                bad = dict(env)
                bad.pop(key)
                with patch.dict(os.environ, bad, clear=True), self.assertRaises(AgentWorkerConfigurationError):
                    build_executor(AgentWorkerSettings.from_environment())

    def test_api_key_or_missing_account_rejected_before_thread(self):
        from unittest.mock import MagicMock
        from backend.core.codex_review_bounded import BoundedReviewGateway
        try:
            import openai_codex.client
        except ImportError:
            self.skipTest('SDK absent')
        with tempfile.TemporaryDirectory() as home:
            gateway = BoundedReviewGateway(codex_bin='/usr/bin/true', control_home=home, qualifier=None)
            runtime = MagicMock()
            runtime.cancel_requested.return_value = False
            for account in (None, {'type': 'apiKey'}):
                with patch('openai_codex.client.CodexClient') as factory:
                    client = factory.return_value
                    client.account_read.return_value.model_dump.return_value = {'account': account}
                    gateway.prepare()
                    with self.assertRaisesRegex(ValueError, 'ChatGPT'):
                        gateway._run(runtime, '{}', home=home)
                    client.thread_start.assert_not_called()
                    client.close.assert_called()
