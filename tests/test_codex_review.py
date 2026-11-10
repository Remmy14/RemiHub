"""Development-only independent review tests: temporary Git repos and mocked SDK/DB."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase, require_card_transition
from backend.core.agent_worker import AgentLeaseLostError, AgentWorkerConfigurationError
from backend.core.codex_review import (
    CodexReviewExecutor, OpenAICodexReviewGateway, build_patch, candidate_digest,
    parse_verdict, review_target, verify_candidate,
)
from backend.services import agent_worker_service as queue
from backend.services.agent_service import AgentStateConflictError, _require_passing_independent_review
from tests.test_agent_worker import claimed_run


class SyntheticCandidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'AGENTS.md').write_text('Review synthetic code only.\n')
        (self.repo / 'example.py').write_text('value = 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic baseline')
        self.base = self.git('rev-parse', 'HEAD').strip()
        (self.repo / 'example.py').write_text('value = 2\n')
        (self.repo / 'new.py').write_text('new_value = 3\n')
        self.run_id = str(uuid4())
        self.cycle = str(uuid4())
        self.metadata = self.capture()
        self.gateway = MagicMock()
        self.gateway.run_turn.side_effect = self.passing_turn
        self.claim = replace(claimed_run(phase=RunPhase.IMPLEMENTATION),
                             id=str(uuid4()), phase=RunPhase.REVIEW,
                             card_status=CardStatus.REVIEWING, codex_thread_id='implementer-thread',
                             result_metadata={'review_source': self.source()})
        self.executor = CodexReviewExecutor(artifact_root=self.artifacts,
            repository_scope=RepositoryScope.BACKEND, gateway=self.gateway)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def capture(self):
        patch_bytes = build_patch(self.repo)
        artifact = self.artifacts / (self.run_id + '.patch')
        artifact.write_bytes(patch_bytes)
        metadata = {'repository_scope': 'backend', 'phase': 'implementation',
                    'review_cycle': self.cycle, 'review_attempt': 1,
                    'tests': [{'command': 'synthetic unittest', 'status': 'passed', 'details': 'ok'}],
                    'workspace': {'artifact_patch': str(artifact), 'base_commit': self.base,
                                  'worktree_path': str(self.repo),
                                  'patch_sha256': hashlib.sha256(patch_bytes).hexdigest()}}
        metadata['candidate_digest'] = candidate_digest(metadata, run_id=self.run_id, revision=1)
        return metadata

    def source(self, attempt=1):
        return {'review_cycle': self.cycle, 'review_attempt': attempt,
                'implementation_run_id': self.run_id, 'implementation_metadata': self.metadata}

    def passing_turn(self, **kwargs):
        frozen = kwargs['repository_path']
        self.assertFalse((frozen / '.git').exists())
        self.assertEqual((frozen / 'example.py').read_text(), 'value = 2\n')
        self.assertEqual((frozen / 'new.py').read_text(), 'new_value = 3\n')
        prompt = json.loads(kwargs['prompt'])
        self.assertIn('new.py', prompt['diff'])
        self.assertEqual(prompt['validation'], self.metadata['tests'])
        return {'response': json.dumps({'verdict': 'pass', 'summary': 'Reviewed synthetic change', 'findings': []}),
                'thread_id': 'reviewer-thread', 'turn_id': 'reviewer-turn'}

    def test_first_pass_is_ready_but_requires_human_approval(self):
        result = self.executor.execute(self.claim)
        self.assertEqual(result.card_status, CardStatus.REVIEW_READY)
        self.assertEqual(result.metadata['independent_review']['review_attempt'], 1)
        with self.assertRaises(ValueError):
            require_card_transition(CardStatus.REVIEWING, CardStatus.DEPLOYMENT_QUEUED)
        self.gateway.run_turn.assert_called_once()

    def test_lease_loss_before_execute_cannot_be_cleared_by_prepare(self):
        self.executor.cancel(self.claim)
        with self.assertRaises(TimeoutError):
            self.executor.execute(self.claim)
        self.gateway.prepare.assert_not_called()
        self.gateway.run_turn.assert_not_called()

    def test_fail_fix_pass_and_third_failure(self):
        finding = {'severity': 'high', 'path': 'example.py', 'description': 'Bad value', 'correction': 'Fix value', 'kind': 'required'}
        self.gateway.run_turn.side_effect = None
        self.gateway.run_turn.return_value = {'response': json.dumps({'verdict': 'changes_requested', 'summary': 'Fix value', 'findings': [finding]}), 'thread_id': 'r', 'turn_id': 't'}
        result = self.executor.execute(self.claim)
        self.assertEqual(result.card_status, CardStatus.IMPLEMENTATION_QUEUED)
        claim3 = replace(self.claim, result_metadata={'review_source': self.source(3)})
        self.assertEqual(self.executor.execute(claim3).card_status, CardStatus.BLOCKED)
        (self.repo / 'example.py').write_text('value = 4\n')
        self.run_id = str(uuid4())
        self.metadata = self.capture()
        self.gateway.run_turn.return_value['response'] = json.dumps({'verdict': 'pass', 'summary': 'Correction reviewed', 'findings': []})
        claim2 = replace(self.claim, result_metadata={'review_source': self.source(2)})
        self.assertEqual(self.executor.execute(claim2).card_status, CardStatus.REVIEW_READY)

    def test_malformed_or_missing_reviewer_evidence_fails_closed(self):
        for turn in ({'response': 'approved'}, {'response': json.dumps({'verdict': 'pass', 'summary': 'ok', 'findings': []}), 'thread_id': 'implementer-thread', 'turn_id': 't'}):
            with self.subTest(turn=turn):
                self.gateway.run_turn.side_effect = None
                self.gateway.run_turn.return_value = turn
                self.assertEqual(self.executor.execute(self.claim).card_status, CardStatus.BLOCKED)

    def test_malformed_schema_response_preserves_real_turn_identity(self):
        self.gateway.run_turn.side_effect = None
        self.gateway.run_turn.return_value = {
            'response': json.dumps({
                'verdict': 'changes_requested', 'summary': 'Issue',
                'findings': [{'severity': 'not-an-enum', 'path': 'example.py',
                              'description': 'Fix', 'correction': 'Fix it', 'kind': 'required'}],
            }),
            'thread_id': 'returned-thread', 'turn_id': 'returned-turn',
        }
        result = self.executor.execute(self.claim)
        self.assertEqual(result.card_status, CardStatus.BLOCKED)
        evidence = result.metadata['independent_review']
        self.assertEqual(evidence['failure_code'], 'review_output_invalid')
        self.assertEqual(evidence['failure_type'], 'ReviewOutputValidationError')
        self.assertEqual(evidence['thread_id'], 'returned-thread')
        self.assertEqual(evidence['turn_id'], 'returned-turn')

    def test_timeout_quota_and_executor_error_fail_closed(self):
        for error in (TimeoutError(), RuntimeError('quota unavailable'), OSError('executor unavailable')):
            with self.subTest(error=type(error)):
                self.gateway.run_turn.side_effect = error
                result = self.executor.execute(self.claim)
                self.assertEqual(result.card_status, CardStatus.BLOCKED)
                self.assertEqual(result.metadata['independent_review']['review_attempt'], 1)

    def test_missing_failed_or_not_run_validation_blocks(self):
        for tests in ([], None, [{'status': 'failed'}], [{'status': 'not_run'}]):
            self.metadata['tests'] = tests
            self.assertEqual(self.executor.execute(self.claim).card_status, CardStatus.BLOCKED)
        self.gateway.run_turn.assert_not_called()

    def test_mixed_validation_reaches_independent_reviewer(self):
        self.metadata["tests"] = [
            {"command": "python -m unittest tests.test_example",
             "status": "passed", "details": "Relevant tests passed"},
            {"command": "python -m unittest discover -s tests -q",
             "status": "failed", "details": "Full suite interrupted"},
        ]
        self.metadata["candidate_digest"] = candidate_digest(
            self.metadata, run_id=self.run_id, revision=1
        )

        result = self.executor.execute(self.claim)

        self.assertEqual(result.card_status, CardStatus.REVIEW_READY)
        self.gateway.run_turn.assert_called_once()

    def test_mixed_validation_can_request_corrections(self):
        self.metadata["tests"] = [
            {"command": "python -m unittest tests.test_example",
             "status": "passed", "details": "Relevant tests passed"},
            {"command": "python -m unittest discover -s tests -q",
             "status": "failed", "details": "Full suite interrupted"},
        ]
        self.metadata["candidate_digest"] = candidate_digest(
            self.metadata, run_id=self.run_id, revision=1
        )

        self.gateway.run_turn.side_effect = None
        self.gateway.run_turn.return_value = {
            "response": json.dumps({
                "verdict": "changes_requested",
                "summary": "Additional validation required",
                "findings": [{
                    "severity": "medium",
                    "path": "example.py",
                    "description": "Required coverage is missing",
                    "correction": "Run the relevant missing tests",
                    "kind": "required",
                }],
            }),
            "thread_id": "reviewer-thread",
            "turn_id": "reviewer-turn",
        }

        result = self.executor.execute(self.claim)

        self.assertEqual(result.card_status, CardStatus.IMPLEMENTATION_QUEUED)

    def test_export_attributes_cannot_hide_or_rewrite_reviewed_files(self):
        (self.repo / '.gitattributes').write_text('hidden.txt export-ignore\nsubstitution.txt export-subst\n')
        (self.repo / 'hidden.txt').write_text('must remain visible\n')
        (self.repo / 'substitution.txt').write_text('$Format:%H$\n')
        self.git('add', '.gitattributes', 'hidden.txt', 'substitution.txt')
        self.git('commit', '-qm', 'synthetic export attributes')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.metadata = self.capture()
        claim = replace(self.claim, result_metadata={'review_source': self.source()})
        def inspect_raw(**kwargs):
            self.assertEqual((kwargs['repository_path'] / 'hidden.txt').read_text(), 'must remain visible\n')
            self.assertEqual((kwargs['repository_path'] / 'substitution.txt').read_text(), '$Format:%H$\n')
            return self.passing_turn(**kwargs)
        self.gateway.run_turn.side_effect = inspect_raw
        self.assertEqual(self.executor.execute(claim).card_status, CardStatus.REVIEW_READY)

    def test_binary_untracked_candidate_is_frozen_exactly(self):
        payload = bytes(range(256)) * 2
        (self.repo / 'asset.bin').write_bytes(payload)
        self.metadata = self.capture()
        claim = replace(self.claim, result_metadata={'review_source': self.source()})
        def inspect_binary(**kwargs):
            self.assertEqual((kwargs['repository_path'] / 'asset.bin').read_bytes(), payload)
            return self.passing_turn(**kwargs)
        self.gateway.run_turn.side_effect = inspect_binary
        self.assertEqual(self.executor.execute(claim).card_status, CardStatus.REVIEW_READY)

    def test_changed_head_and_validation_evidence_invalidate_pass(self):
        self.metadata['tests'][0]['details'] = 'changed after capture'
        with self.assertRaises(ValueError):
            verify_candidate(self.metadata, run_id=self.run_id, revision=1, live=True)
        self.metadata = self.capture()
        self.git('commit', '--allow-empty', '-qm', 'different base')
        with self.assertRaises(ValueError):
            verify_candidate(self.metadata, run_id=self.run_id, revision=1, live=True)

    def test_review_claim_retries_preserve_durable_ordinal(self):
        row = {'phase': 'review', 'card_status': 'reviewing', 'run_status': 'running',
               'id': self.claim.id, 'card_id': self.claim.card_id,
               'result_metadata': self.claim.result_metadata,
               'review_cycle': self.cycle, 'review_attempt': 1}
        phase, _, status = queue._validate_candidate(row)
        self.assertEqual(phase, RunPhase.REVIEW)
        self.assertEqual(status, CardStatus.REVIEWING)
        with self.assertRaises(queue.AgentQueueStateError):
            queue._validate_candidate({**row, 'review_attempt': 2})
        with self.assertRaises(queue.AgentQueueStateError):
            queue._validate_candidate({**row, 'review_cycle': str(uuid4())})

    def test_worker_lock_rejects_revision_and_ordinal_drift(self):
        cur = MagicMock()
        base = {'phase': 'review', 'revision': 1, 'card_revision': 1,
                'review_cycle': self.cycle, 'review_attempt': 1}
        with patch.object(queue, '_row_to_dict', side_effect=lambda c, r: r):
            for changed in ({'revision': 2}, {'card_revision': 2}, {'review_attempt': 2}, {'review_cycle': str(uuid4())}):
                cur.fetchone.return_value = {**base, **changed}
                with self.assertRaises(AgentLeaseLostError):
                    queue._lock_owned_run(cur, self.claim, statuses=('running',))
            cur.fetchone.return_value = base
            self.assertEqual(queue._lock_owned_run(cur, self.claim, statuses=('running',)), base)
        query = cur.execute.call_args.args[0]
        self.assertIn('runs.lease_token = %s', query)
        self.assertIn('runs.lease_expires_at > CURRENT_TIMESTAMP', query)
        self.assertIn('FOR UPDATE OF runs, cards', query)

    def test_changed_untracked_file_blocks(self):
        (self.repo / 'new.py').write_text('new_value = 9\n')
        self.assertEqual(self.executor.execute(self.claim).card_status, CardStatus.BLOCKED)
        self.gateway.run_turn.assert_not_called()

    def test_patch_changed_blocks(self):
        Path(self.metadata['workspace']['artifact_patch']).write_bytes(b'tampered')
        self.assertEqual(self.executor.execute(self.claim).card_status, CardStatus.BLOCKED)

    def test_stale_revision_or_digest_blocks(self):
        self.assertEqual(self.executor.execute(replace(self.claim, card_revision=2)).card_status, CardStatus.BLOCKED)
        self.metadata['candidate_digest'] = '0' * 64
        self.assertEqual(self.executor.execute(self.claim).card_status, CardStatus.BLOCKED)

    def test_changes_during_review_block(self):
        def mutate(**kwargs):
            turn = self.passing_turn(**kwargs)
            (self.repo / 'new.py').write_text('changed during review\n')
            return turn
        self.gateway.run_turn.side_effect = mutate
        self.assertEqual(self.executor.execute(self.claim).card_status, CardStatus.BLOCKED)

    def test_reviewer_edit_to_frozen_candidate_blocks(self):
        def mutate(**kwargs):
            turn = self.passing_turn(**kwargs)
            (kwargs['repository_path'] / 'example.py').write_text('reviewer edited\n')
            return turn
        self.gateway.run_turn.side_effect = mutate
        self.assertEqual(self.executor.execute(self.claim).card_status, CardStatus.BLOCKED)
        self.assertEqual((self.repo / 'example.py').read_text(), 'value = 2\n')

    def test_android_uses_same_gate_and_requires_trusted_validation(self):
        self.metadata['repository_scope'] = 'android'
        self.metadata['candidate_digest'] = candidate_digest(self.metadata, run_id=self.run_id, revision=1)
        executor = CodexReviewExecutor(artifact_root=self.artifacts, repository_scope=RepositoryScope.ANDROID, gateway=self.gateway)
        claim = replace(self.claim, repository_scope=RepositoryScope.ANDROID)
        self.assertEqual(executor.execute(claim).card_status, CardStatus.BLOCKED)
        self.metadata['trusted_validation'] = {'success': True, 'gradle_offline': True, 'network': 'denied', 'protected_build_files_unchanged': True, 'release_apk': {'signed': False, 'package_name': 'com.alex.remihub'}}
        self.metadata['candidate_digest'] = candidate_digest(self.metadata, run_id=self.run_id, revision=1)
        self.assertEqual(executor.execute(claim).card_status, CardStatus.REVIEW_READY)

    def test_unsupported_scope_fails_closed(self):
        with self.assertRaises(AgentWorkerConfigurationError):
            CodexReviewExecutor(artifact_root=self.artifacts, repository_scope=RepositoryScope.BACKEND_AND_ANDROID, gateway=self.gateway)
        with self.assertRaises(AgentWorkerConfigurationError):
            self.executor.execute(replace(self.claim, repository_scope=RepositoryScope.ANDROID))

    def test_artifact_escape_fails_closed(self):
        self.metadata['workspace']['artifact_patch'] = str(self.root / 'outside.patch')
        with self.assertRaises(ValueError):
            self.executor.execute(self.claim)

    def test_symlink_candidate_rejected(self):
        (self.repo / 'link.py').symlink_to('example.py')
        self.metadata = self.capture()
        claim = replace(self.claim, result_metadata={'review_source': self.source()})
        self.assertEqual(self.executor.execute(claim).card_status, CardStatus.BLOCKED)

    def test_deployment_gate_requires_matching_persisted_pass(self):
        result = {'id': self.run_id, 'result_metadata': self.metadata}
        evidence = self.executor.execute(self.claim).metadata['independent_review']
        cur = MagicMock()
        with patch('backend.services.agent_service._row_to_dict', side_effect=lambda cur, row: row):
            for changed in (None, {'verdict': 'changes_requested'}, {**evidence, 'card_revision': 2}, {**evidence, 'candidate_digest': 'stale'}):
                cur.fetchone.return_value = {'result_metadata': {'independent_review': changed}} if changed else None
                with self.assertRaises(AgentStateConflictError):
                    _require_passing_independent_review(cur, result, card_id=self.claim.card_id, card_revision=1)
            cur.fetchone.return_value = {'id': self.claim.id, 'result_metadata': {'independent_review': evidence}}
            approved = _require_passing_independent_review(cur, result, card_id=self.claim.card_id, card_revision=1)
            self.assertEqual(approved['result_metadata']['independent_review'], evidence)
            (self.repo / 'new.py').write_text('same size bad!\n')
            with self.assertRaises(AgentStateConflictError):
                _require_passing_independent_review(cur, result, card_id=self.claim.card_id, card_revision=1)

    def test_transactional_review_completion_and_duplicate_lease_fencing(self):
        result = self.executor.execute(self.claim)
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        with patch.object(queue, 'get_db_conn', return_value=conn), patch.object(queue, 'put_db_conn'), patch.object(queue, '_lock_owned_run', return_value={'card_status': 'reviewing'}), patch.object(queue, '_insert_message', return_value='message'), patch.object(queue, '_insert_event') as event, patch.object(queue, 'insert_notification'):
            queue.complete_run(self.claim, result)
            conn.commit.assert_called_once()
            self.assertIn('review.verdict', [call.kwargs['event_type'] for call in event.call_args_list])
            self.assertFalse(any('INSERT INTO agent.approvals' in str(c) for c in cur.execute.call_args_list))
            self.assertFalse(any('INSERT INTO agent.runs' in str(c) for c in cur.execute.call_args_list))
        for reason in ('expired lease', 'duplicate completion', 'cancelled card'):
            with self.subTest(reason=reason), patch.object(queue, 'get_db_conn', return_value=conn), patch.object(queue, 'put_db_conn'), patch.object(queue, '_lock_owned_run', side_effect=AgentLeaseLostError(reason)), patch.object(queue, '_review_handoff') as handoff:
                with self.assertRaises(AgentLeaseLostError):
                    queue.complete_run(self.claim, result)
                handoff.assert_not_called()
                self.assertGreater(conn.rollback.call_count, 0)

    def test_correction_handoff_preserves_cycle_and_budget(self):
        evidence = {'verdict': 'changes_requested', 'review_cycle': self.cycle, 'review_attempt': 2, 'findings': [{'description': 'fix'}]}
        cur = MagicMock()
        cur.fetchone.return_value = {'requested_by': 'authorizing-human'}
        with patch.object(queue, '_row_to_dict', side_effect=lambda c, r: r), patch.object(queue, '_insert_event'):
            queue._review_handoff(cur, self.claim, {'independent_review': evidence}, CardStatus.IMPLEMENTATION_QUEUED, 'message')
        insert = next(c for c in cur.execute.call_args_list if 'INSERT INTO agent.runs' in c.args[0])
        args = insert.args[1]
        self.assertEqual(args[2], 'implementation')
        self.assertEqual(args[4], 'authorizing-human')
        self.assertEqual(args[-2:], (self.cycle, 3))
        self.assertEqual(json.loads(args[-3])['review_attempt'], 3)
        cur.reset_mock()
        with patch.object(queue, '_insert_event'):
            queue._review_handoff(cur, self.claim, {'independent_review': {**evidence, 'review_attempt': 3}}, CardStatus.BLOCKED, 'message')
            cur.execute.assert_not_called()

    def test_implementation_handoff_reserves_one_attempt(self):
        cur = MagicMock()
        cur.fetchone.return_value = {'requested_by': 'human'}
        claim = replace(self.claim, id=self.run_id, phase=RunPhase.IMPLEMENTATION)
        with patch.object(queue, '_row_to_dict', side_effect=lambda c, r: r), patch.object(queue, '_insert_event'):
            queue._review_handoff(cur, claim, self.metadata, CardStatus.REVIEW_QUEUED, 'message')
        insert = next(c for c in cur.execute.call_args_list if 'INSERT INTO agent.runs' in c.args[0])
        self.assertEqual(insert.args[1][2], 'review')
        self.assertEqual(insert.args[1][-2:], (self.cycle, 1))


class ReviewContractTests(unittest.TestCase):
    def test_api_projects_review_progress_and_verdict_without_source_payload(self):
        from backend.models.agent_models import AgentRun
        identity = str(uuid4())
        model = AgentRun(id=identity, card_id=identity, phase='review', status='queued', attempt_count=0,
                         card_revision=1, requested_by=identity,
                         available_at='2026-11-04T12:00:00Z', created_at='2026-11-04T12:00:00Z',
                         updated_at='2026-11-04T12:00:00Z',
                         result_metadata={'review_source': {'review_cycle': identity, 'review_attempt': 2,
                             'implementation_run_id': identity, 'implementation_metadata': {'private_context': 'not projected'}}})
        projection = model.model_dump(mode='json')['independent_review']
        self.assertEqual(projection['review_attempt'], 2)
        self.assertNotIn('implementation_metadata', projection)
        model.result_metadata['independent_review'] = {'verdict': 'blocked', 'findings': [], 'review_attempt': 2}
        self.assertEqual(model.model_dump(mode='json')['independent_review']['verdict'], 'blocked')

    def test_schema_restricts_severity_and_parser_diagnoses_fields(self):
        from backend.core.codex_review import REVIEW_SCHEMA, ReviewOutputValidationError
        finding = REVIEW_SCHEMA['properties']['findings']['items']['properties']
        self.assertEqual(finding['severity']['enum'], ['critical', 'high', 'medium', 'low'])
        valid = {'verdict': 'changes_requested', 'summary': 'Fix issue',
                 'findings': [{'severity': 'high', 'path': 'a.py',
                               'description': 'Issue', 'correction': 'Fix it', 'kind': 'required'}]}
        self.assertEqual(parse_verdict(json.dumps(valid))['verdict'], 'changes_requested')
        for label, change in (
            ('severity', {'severity': 'major'}),
            ('kind', {'kind': 'optional'}),
            ('correction', {'correction': '   '}),
            ('description', {'description': '   '}),
            ('path', {'path': 'x' * 2001}),
            ('severity type', {'severity': []}),
            ('kind type', {'kind': []}),
        ):
            with self.subTest(label=label):
                payload = json.loads(json.dumps(valid))
                payload['findings'][0].update(change)
                with self.assertRaises(ReviewOutputValidationError):
                    parse_verdict(json.dumps(payload))
        missing_kind = json.loads(json.dumps(valid))
        del missing_kind['findings'][0]['kind']
        with self.assertRaises(ReviewOutputValidationError):
            parse_verdict(json.dumps(missing_kind))
        for response in ('{', '[]', '{"verdict": {}}'):
            with self.subTest(response=response), self.assertRaises(ReviewOutputValidationError):
                parse_verdict(response)

    def test_invalid_verdicts_cannot_pass(self):
        for payload in ({'verdict': 'approved'}, {'verdict': 'pass', 'summary': 'ok', 'findings': [{}]}, {'verdict': 'changes_requested', 'summary': 'fix', 'findings': []}):
            with self.assertRaises(ValueError):
                parse_verdict(json.dumps(payload))

    def test_budget_never_accepts_zero_four_or_boolean(self):
        for attempt in (0, 4, True, '1'):
            with self.assertRaises(ValueError):
                review_target('pass', attempt)

    def test_migration_budget_and_retry_identity_are_durable(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / 'backend/database/migrations/0020_independent_review.up.sql').read_text()
        self.assertIn('review_attempt BETWEEN 1 AND 3', migration)
        self.assertIn('card_id, card_revision, review_attempt', migration)
        self.assertIn("WHERE phase = 'review'", migration)
        down = (root / 'backend/database/migrations/0020_independent_review.down.sql').read_text()
        self.assertIn('RAISE EXCEPTION', down)




class ReviewGatewayTests(unittest.TestCase):
    def fake_sdk(self, calls, *, on_run=None):
        import types
        class Config:
            def __init__(self, **kwargs):
                calls['config'] = kwargs
        class Handle:
            def run(self):
                if on_run:
                    on_run()
                return types.SimpleNamespace(final_response='{}', id='review-turn')
        class Thread:
            id = 'fresh-review-thread'
            def turn(self, prompt, **kwargs):
                calls['turn'] = kwargs
                return Handle()
        class Codex:
            def __init__(self, config):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
            def close(self):
                calls['closed'] = calls.get('closed', 0) + 1
            def thread_start(self, **kwargs):
                calls['start'] = kwargs
                return Thread()
            def thread_resume(self, *args, **kwargs):
                raise AssertionError('Independent reviewer must never resume a thread')
        return types.SimpleNamespace(Codex=Codex, CodexConfig=Config,
            ApprovalMode=types.SimpleNamespace(deny_all='deny-all'),
            Sandbox=types.SimpleNamespace(read_only='read-only'))

    def test_gateway_uses_dedicated_wrapper_fresh_thread_read_only_and_deny_all(self):
        import sys
        calls = {}
        with tempfile.NamedTemporaryFile() as wrapper:
            Path(wrapper.name).chmod(0o700)
            gateway = OpenAICodexReviewGateway(codex_bin=wrapper.name)
            with patch.dict(sys.modules, {'openai_codex': self.fake_sdk(calls)}):
                result = gateway.run_turn(repository_path=Path('/tmp/frozen'), prompt='Review', model='synthetic')
        self.assertEqual(result['thread_id'], 'fresh-review-thread')
        self.assertEqual(calls['config']['codex_bin'], wrapper.name)
        self.assertTrue(calls['start']['ephemeral'])
        self.assertEqual(calls['start']['approval_mode'], 'deny-all')
        self.assertEqual(calls['start']['sandbox'], 'read-only')
        self.assertEqual(calls['turn']['sandbox'], 'read-only')
        self.assertEqual(calls['turn']['approval_mode'], 'deny-all')
        self.assertIn('untrusted', calls['start']['developer_instructions'])

    def test_lease_loss_cancellation_closes_isolated_server_and_cannot_pass(self):
        import sys
        calls = {}
        with tempfile.NamedTemporaryFile() as wrapper:
            Path(wrapper.name).chmod(0o700)
            gateway = OpenAICodexReviewGateway(codex_bin=wrapper.name)
            with patch.dict(sys.modules, {'openai_codex': self.fake_sdk(calls, on_run=gateway.cancel)}):
                with self.assertRaises(TimeoutError):
                    gateway.run_turn(repository_path=Path('/tmp/frozen'), prompt='Review', model=None)
        self.assertGreaterEqual(calls['closed'], 1)

    def test_cancellation_before_turn_is_not_lost(self):
        import sys
        calls = {}
        with tempfile.NamedTemporaryFile() as wrapper:
            Path(wrapper.name).chmod(0o700)
            gateway = OpenAICodexReviewGateway(codex_bin=wrapper.name)
            gateway.prepare()
            gateway.cancel()
            with patch.dict(sys.modules, {'openai_codex': self.fake_sdk(calls)}):
                with self.assertRaises(TimeoutError):
                    gateway.run_turn(repository_path=Path('/tmp/frozen'), prompt='Review', model=None)
        self.assertNotIn('start', calls)

    def test_timeout_cannot_return_a_passing_turn(self):
        import sys
        calls = {}
        with tempfile.NamedTemporaryFile() as wrapper:
            Path(wrapper.name).chmod(0o700)
            gateway = OpenAICodexReviewGateway(codex_bin=wrapper.name)
            def fire_timer(seconds, callback):
                timer = MagicMock()
                timer.start.side_effect = callback
                return timer
            with patch.dict(sys.modules, {'openai_codex': self.fake_sdk(calls)}), patch('backend.core.codex_review.threading.Timer', side_effect=fire_timer):
                with self.assertRaises(TimeoutError):
                    gateway.run_turn(repository_path=Path('/tmp/frozen'), prompt='Review', model=None)
        self.assertNotIn('start', calls)


if __name__ == '__main__':
    unittest.main()
