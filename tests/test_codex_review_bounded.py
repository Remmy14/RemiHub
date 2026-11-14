"""Bounded reviewer tests use only private synthetic trees and no credentials."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from backend.core.codex_review_bounded import (
    BoundedReviewGateway, dossier, private_directory, sha, tool_config, toml,
)


class DossierTests(unittest.TestCase):
    def test_failed_turn_propagates_last_reported_usage_without_accepting_review(self):
        gateway = object.__new__(BoundedReviewGateway)
        gateway._lock = threading.Lock()
        gateway._cancelled = False
        gateway._client = None
        gateway.timeout_seconds = 3
        runtime = MagicMock()
        runtime.model = "review-model"
        runtime.config = {}
        runtime.identity = {"qualified": True}
        runtime.cancel_requested.return_value = False
        client = MagicMock()
        client.account_read.return_value.model_dump.return_value = {"account": {"type": "chatgpt"}}
        client.request.return_value.model_dump.return_value = {"config": {}}
        client.thread_start.return_value.thread.id = "thread-1"
        client.turn_start.return_value.turn.id = "turn-1"

        def event(method, payload):
            result = MagicMock()
            result.method = method
            result.payload.model_dump.return_value = payload
            return result

        client.next_turn_notification.side_effect = [
            event("thread/tokenUsage/updated", {"tokenUsage": {"lastTokenUsage": {
                "inputTokens": 7, "outputTokens": 3, "totalTokens": 10}}}),
            event("thread/tokenUsage/updated", {"tokenUsage": {"lastTokenUsage": {
                "inputTokens": "invalid", "outputTokens": 9}}}),
            event("turn/completed", {"turn": {"status": "failed"}}),
        ]
        fake_package = types.ModuleType("openai_codex")
        fake_package.__path__ = []
        fake_client = types.ModuleType("openai_codex.client")
        fake_client.CodexClient = lambda *_args: client
        with patch.dict("sys.modules", {"openai_codex": fake_package, "openai_codex.client": fake_client}):
            with self.assertRaises(RuntimeError) as failure:
                gateway._run(runtime, "{}", home=Path("/tmp"))
        self.assertEqual(failure.exception.codex_turn_id, "turn-1")
        self.assertEqual(failure.exception.codex_usage["last"]["total_tokens"], 10)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tree = self.root / 'tree'
        self.tree.mkdir()
        (self.tree / 'source.py').write_bytes(b'x = 1\r\n')
        (self.tree / 'test_source.py').write_text('assert True\n')
        self.raw = b'diff\x00exact\xff\n'
        self.prompt = json.dumps({'raw_diff_b64': base64.b64encode(self.raw).decode(),
                                  'validation': [{'status': 'passed', 'name': 'synthetic'}]})

    def test_complete_manifest_preserves_exact_source_test_and_diff_bytes(self):
        value = json.loads(dossier(self.tree, self.prompt))
        self.assertTrue(value['complete_tree'])
        self.assertEqual(base64.b64decode(value['raw_diff_b64']), self.raw)
        self.assertEqual(value['raw_diff_sha256'], sha(self.raw))
        self.assertEqual({f['path'] for f in value['files']}, {'source.py', 'test_source.py'})
        for entry in value['files']:
            content = (self.tree / entry['path']).read_bytes()
            self.assertEqual(base64.b64decode(entry['bytes_b64']), content)
            self.assertEqual(entry['sha256'], sha(content))
            self.assertEqual(entry['size'], len(content))
        self.assertEqual(dossier(self.tree, self.prompt), dossier(self.tree, self.prompt))

    def test_no_truncation_or_partial_context(self):
        with patch('backend.core.codex_review_bounded.MAX_BYTES', 5):
            with self.assertRaisesRegex(ValueError, 'budget'):
                dossier(self.tree, self.prompt)
        with patch('backend.core.codex_review_bounded.MAX_FILES', 1):
            with self.assertRaisesRegex(ValueError, 'budget'):
                dossier(self.tree, self.prompt)
        with patch('backend.core.codex_review_bounded.MAX_WIRE_BYTES', 5):
            with self.assertRaisesRegex(ValueError, 'budget'):
                dossier(self.tree, self.prompt)

    def test_missing_or_failed_evidence_and_missing_raw_diff_block(self):
        for extra in ({}, {'raw_diff_b64': '***'}, {'raw_diff_b64': ''},
                      {'raw_diff_b64': '', 'validation': [{'status': 'failed'}]}):
            with self.subTest(extra=extra), self.assertRaises((ValueError, KeyError)):
                dossier(self.tree, json.dumps(extra))

    def test_links_special_files_and_nontext_block(self):
        path = self.tree / 'danger'
        path.symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(ValueError, 'links'):
            dossier(self.tree, self.prompt)
        path.unlink()
        os.link(self.tree / 'source.py', path)
        with self.assertRaisesRegex(ValueError, 'links'):
            dossier(self.tree, self.prompt)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, 'special'):
            dossier(self.tree, self.prompt)
        path.unlink()
        path.write_bytes(b'\xff')
        with self.assertRaisesRegex(ValueError, 'Non-text'):
            dossier(self.tree, self.prompt)

    def test_control_directory_is_dedicated_and_owned(self):
        self.assertEqual(private_directory(self.root), self.root)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, '0700'):
            private_directory(self.root)
        self.root.chmod(0o700)
        (self.root / 'config.toml').write_text('untrusted = true\n')
        with self.assertRaisesRegex(ValueError, 'configuration'):
            BoundedReviewGateway(codex_bin='/usr/bin/true', control_home=self.root, qualifier=None)

    def test_absence_is_not_inferred_from_additive_empty_tables(self):
        config = tool_config(['shell_tool', 'code_mode', 'multi_agent'])
        for name in ['shell_tool', 'code_mode', 'multi_agent']:
            self.assertIs(config['features.' + name], False)
        self.assertEqual(config['web_search'], 'disabled')
        self.assertEqual(toml({'requires_openai_auth': False}), '{"requires_openai_auth"=false}')

    def test_incomplete_dossier_never_reaches_qualifier_or_auth_home(self):
        qualifier = unittest.mock.Mock()
        gateway = BoundedReviewGateway(codex_bin='/usr/bin/true', control_home=self.root, qualifier=qualifier)
        with self.assertRaises(KeyError):
            gateway.run_turn(repository_path=self.tree, prompt='{}', model='synthetic')
        qualifier.assert_not_called()

    def test_cancellation_before_start(self):
        gateway = BoundedReviewGateway(codex_bin='/usr/bin/true', control_home=self.root, qualifier=None)
        gateway.cancel()
        class Runtime:
            def sdk_config(self, *args):
                from openai_codex import CodexConfig
                return CodexConfig(launch_args_override=('/usr/bin/false',))
        try:
            import openai_codex
        except ImportError:
            self.skipTest('Installed SDK unavailable')
        with self.assertRaises(TimeoutError):
            gateway._run(Runtime(), '{}', home=self.root)
        gateway.prepare()
        self.assertFalse(gateway._cancelled)

    def test_existing_executor_supplies_exact_patch_and_records_boundary(self):
        from tests.test_codex_review import SyntheticCandidate
        fixture = SyntheticCandidate(methodName='test_first_pass_is_ready_but_requires_human_approval')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.gateway.requires_raw_diff = True
        def accept(**kwargs):
            context = json.loads(kwargs['prompt'])
            raw = Path(fixture.metadata['workspace']['artifact_patch']).read_bytes()
            self.assertEqual(base64.b64decode(context['raw_diff_b64']), raw)
            turn = fixture.passing_turn(**kwargs)
            turn.update(boundary='qualified-no-tools', dossier_sha256='synthetic', qualification={'synthetic': True})
            return turn
        fixture.gateway.run_turn.side_effect = accept
        result = fixture.executor.execute(fixture.claim)
        self.assertEqual(result.metadata['independent_review']['boundary'], 'qualified-no-tools')

    def test_executor_records_reported_usage_from_failed_turn_and_blocks(self):
        from tests.test_codex_review import SyntheticCandidate
        fixture = SyntheticCandidate(methodName='test_first_pass_is_ready_but_requires_human_approval')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def fail(**_kwargs):
            error = RuntimeError("review turn failed")
            error.codex_turn_id = "failed-turn"
            error.codex_usage = {"last": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}
            raise error

        fixture.gateway.run_turn.side_effect = fail
        with patch("backend.services.agent_usage_writer.record_turn") as record:
            result = fixture.executor.execute(fixture.claim)
        self.assertEqual(result.metadata["independent_review"]["verdict"], "blocked")
        self.assertEqual(result.metadata["independent_review"]["turn_id"], "failed-turn")
        self.assertEqual(record.call_args.kwargs["usage"]["last"]["total_tokens"], 10)

    def test_qualification_provider_rejects_credentials_and_nonempty_tools(self):
        # Protocol validation is also tested without a listening socket; a host
        # qualify run additionally uses the real SDK and actual app-server.
        from backend.core.codex_review_qualification import SyntheticProvider
        provider = object.__new__(SyntheticProvider)
        provider.requests = [{'tools': [{'name': 'shell'}]}]
        provider.failures = []
        with self.assertRaisesRegex(RuntimeError, 'Nonempty'):
            provider.verify()
        provider.requests = []
        with self.assertRaisesRegex(RuntimeError, 'Transport'):
            provider.verify()


if __name__ == '__main__':
    unittest.main()
