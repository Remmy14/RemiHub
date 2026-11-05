"""Synthetic API-service/queue/prompt and browser request contracts; no external services."""
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from pydantic import ValidationError
from backend.core.agent_state import CardStatus, RunPhase
from backend.core.codex_review import parse_verdict
from backend.core.codex_implementation import _review_correction_prompt
from backend.models.agent_models import AgentReviewRevisionRequest
from backend.services import agent_service as service, agent_worker_service as queue
from tests.test_agent_worker import claimed_run
from tests import test_codex_review as review_tests


FINDING = {'severity': 'medium', 'path': 'example.py', 'description': 'Required defect', 'correction': 'Fix it'}


class FindingContractTests(unittest.TestCase):
    def test_advisories_do_not_block_pass_legacy_findings_do(self):
        advisory = {**FINDING, 'kind': 'advisory'}
        self.assertEqual(parse_verdict(json.dumps({'verdict': 'pass', 'summary': 'ok', 'findings': [advisory]}))['findings'], [advisory])
        for verdict, findings in [('pass', [FINDING]), ('changes_requested', [advisory]),
                                  ('pass', [{**FINDING, 'kind': 'optional'}])]:
            with self.subTest(verdict=verdict, findings=findings), self.assertRaises(ValueError):
                parse_verdict(json.dumps({'verdict': verdict, 'summary': 'ok', 'findings': findings}))
        parse_verdict(json.dumps({'verdict': 'changes_requested', 'summary': 'fix', 'findings': [FINDING, advisory]}))

    def test_request_model_rejects_missing_notes_and_forged_authority(self):
        valid = dict(review_run_id=str(uuid4()), candidate_digest='a' * 64, notes='Fix one, defer the other',
                     decisions=[{'finding_index': 0, 'decision': 'fix'}])
        AgentReviewRevisionRequest(**valid)
        for delta in ({'notes': ' '}, {'actor_user_id': str(uuid4())}, {'decisions': []},
                      {'decisions': [{'finding_index': 0, 'decision': 'pass'}]}):
            with self.subTest(delta=delta), self.assertRaises(ValidationError):
                AgentReviewRevisionRequest(**{**valid, **delta})

    def test_exact_finding_coverage(self):
        evidence = {'verdict': 'changes_requested', 'summary': 'fix', 'findings': [FINDING, {**FINDING, 'kind': 'advisory'}]}
        valid = [{'finding_index': 0, 'decision': 'fix'}, {'finding_index': 1, 'decision': 'defer_ignore'}]
        self.assertEqual(service._review_decisions(evidence, valid)[1]['finding'], evidence['findings'][1])
        for decisions in (valid[:1], valid + valid[:1], [valid[0], valid[0]],
                          [{**valid[0], 'finding_index': True}, valid[1]],
                          [{**valid[0], 'finding': 'forged'}, valid[1]]):
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                service._review_decisions(evidence, decisions)

    def test_failure_preserves_authorized_cycle_and_human_instructions(self):
        metadata = {'review_cycle': 'cycle', 'review_attempt': 2, 'review_findings': [FINDING],
                    'human_review_handoffs': [{'notes': 'operator'}], 'garbage': 'discard'}
        self.assertEqual(queue._failure_metadata(metadata), {k: v for k, v in metadata.items() if k != 'garbage'})


class HumanHandoffTests(unittest.TestCase):
    # Reuse the temporary Git candidate fixture, without duplicating its test methods.
    setUp = review_tests.SyntheticCandidate.setUp
    git = review_tests.SyntheticCandidate.git
    capture = review_tests.SyntheticCandidate.capture
    source = review_tests.SyntheticCandidate.source
    passing_turn = review_tests.SyntheticCandidate.passing_turn

    def fixture(self, stack):
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value.__enter__.return_value
        self.review_id = str(uuid4())
        self.card = {'id': self.claim.card_id, 'revision': 1, 'status': 'blocked',
                     'resume_status': 'review_queued', 'repository_scope': 'backend',
                     'codex_thread_id': 'implementer-thread', 'worktree_path': str(self.repo)}
        self.evidence = {'verdict': 'changes_requested', 'summary': 'fix', 'findings': [FINDING],
                         'candidate_digest': self.metadata['candidate_digest'], 'review_attempt': 3,
                         'review_cycle': self.cycle, 'card_revision': 1, 'implementation_run_id': self.run_id}
        self.review = {'id': self.review_id, 'phase': 'review', 'status': 'succeeded', 'card_revision': 1,
                       'review_cycle': self.cycle, 'review_attempt': 3,
                       'result_metadata': {'independent_review': self.evidence, 'review_source': self.source(3)}}
        self.implementation = {'id': self.run_id, 'result_metadata': self.metadata}
        self.cur.fetchone.side_effect = [self.review, self.implementation]
        stack.enter_context(patch.object(service, 'get_db_conn', return_value=self.conn))
        stack.enter_context(patch.object(service, 'put_db_conn'))
        stack.enter_context(patch.object(service, '_row_to_dict', side_effect=lambda c, r: r))
        stack.enter_context(patch.object(service, '_locked_card', return_value=self.card))
        stack.enter_context(patch.object(service, '_card_detail', return_value={'synthetic': True}))
        self.kwargs = dict(card_id=self.card['id'], requested_by=str(uuid4()), review_run_id=self.review_id,
                           candidate_digest=self.metadata['candidate_digest'], notes='Fix this defect, keep scope narrow',
                           decisions=[{'finding_index': 0, 'decision': 'fix'}])

    def test_real_service_transaction_audits_actor_exact_findings_and_new_cycle(self):
        with ExitStack() as stack:
            self.fixture(stack)
            self.assertIn('request_revision', service._decorate_card(self.card, latest_run=self.review)['allowed_actions'])
            prior = {'review_run_id': str(uuid4()), 'notes': 'Prior cycle scope decision'}
            self.metadata['human_review_handoffs'] = [prior]
            service.request_review_revision(**self.kwargs)
            calls = self.cur.execute.call_args_list
            update = next(c for c in calls if 'UPDATE agent.runs SET result_metadata' in c.args[0])
            metadata = json.loads(update.args[1][0])
            self.assertEqual(metadata['human_review_handoffs'][0], prior)
            handoff = metadata['human_review_handoffs'][-1]
            self.assertEqual(handoff['actor_user_id'], self.kwargs['requested_by'])
            self.assertEqual(handoff['review_run_id'], self.review_id)
            self.assertEqual(handoff['decisions'][0]['finding'], FINDING)
            self.assertEqual(metadata['review_attempt'], 1)
            self.assertNotEqual(metadata['review_cycle'], self.cycle)
            approval = next(c for c in calls if 'INSERT INTO agent.approvals' in c.args[0])
            self.assertEqual(approval.args[1][2:5], ('implementation', 2, self.kwargs['requested_by']))
            self.assertFalse(any('deployment' in str(c.args[1]) for c in calls))
            self.assertFalse(any('codex_thread_id =' in c.args[0] for c in calls))
            self.conn.commit.assert_called_once()
            # A serialized duplicate sees the already queued revision and cannot enqueue again.
            self.card.update(status='implementation_queued', revision=2)
            self.cur.fetchone.side_effect = [self.review]
            with self.assertRaises(service.AgentStateConflictError):
                service.request_review_revision(**self.kwargs)
            self.assertEqual(self.conn.commit.call_count, 1)
            self.conn.rollback.assert_called_once()

    def test_stale_wrong_candidate_non_exhausted_and_invalid_decisions_roll_back(self):
        for change in ('review_id', 'digest', 'attempt', 'verdict', 'candidate', 'findings', 'modified_tree', 'revision', 'scope'):
            with self.subTest(change=change), ExitStack() as stack:
                self.fixture(stack)
                if change == 'review_id': self.kwargs['review_run_id'] = str(uuid4())
                if change == 'digest': self.kwargs['candidate_digest'] = 'f' * 64
                if change == 'attempt': self.evidence['review_attempt'] = 2
                if change == 'verdict': self.evidence['verdict'] = 'blocked'
                if change == 'candidate': self.implementation['id'] = str(uuid4())
                if change == 'findings': self.kwargs['decisions'] = []
                if change == 'modified_tree': (self.repo / 'example.py').write_text('tampered\n')
                if change == 'revision': self.card['revision'] = 2
                if change == 'scope': self.card['repository_scope'] = 'auto'
                with self.assertRaises((service.AgentStateConflictError, ValueError)):
                    service.request_review_revision(**self.kwargs)
                self.conn.commit.assert_not_called()
                self.conn.rollback.assert_called_once()
                self.assertFalse(any('INSERT' in c.args[0] for c in self.cur.execute.call_args_list))
                (self.repo / 'example.py').write_text('value = 2\n')

    def test_api_deployment_gate_accepts_only_advisories_on_a_pass(self):
        evidence = self.executor.execute(self.claim).metadata['independent_review']
        cur = MagicMock()
        result = {'id': self.run_id, 'result_metadata': self.metadata}
        with patch.object(service, '_row_to_dict', side_effect=lambda c, r: r):
            for kind in ('advisory', 'required', None):
                finding = dict(FINDING)
                if kind is not None: finding['kind'] = kind
                cur.fetchone.return_value = {'result_metadata': {'independent_review': {**evidence, 'findings': [finding]}}}
                if kind == 'advisory':
                    service._require_passing_independent_review(cur, result, card_id=self.claim.card_id, card_revision=1)
                else:
                    with self.assertRaises(service.AgentStateConflictError):
                        service._require_passing_independent_review(cur, result, card_id=self.claim.card_id, card_revision=1)

    def test_executor_cannot_forge_human_authority_or_reset_review_budget(self):
        from backend.core.agent_worker import ExecutionResult
        authorized = [{'actor_user_id': str(uuid4()), 'notes': 'Authenticated scope decision'}]
        claim = replace(self.claim, id=self.run_id, phase=RunPhase.IMPLEMENTATION,
                        result_metadata={'review_cycle': self.cycle, 'review_attempt': 2, 'human_review_handoffs': authorized})
        result = ExecutionResult(message='synthetic completion', card_status=CardStatus.REVIEW_QUEUED,
                                 metadata={**self.metadata, 'review_cycle': 'forged', 'review_attempt': 1,
                                           'human_review_handoffs': [{'notes': 'forged waiver'}]})
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        with patch.object(queue, 'get_db_conn', return_value=conn), patch.object(queue, 'put_db_conn'), \
             patch.object(queue, '_lock_owned_run', return_value={'card_status': 'implementing'}), \
             patch.object(queue, '_insert_message', return_value='message'), patch.object(queue, '_insert_event'), \
             patch.object(queue, 'insert_notification'), patch.object(queue, '_review_handoff') as handoff:
            queue.complete_run(claim, result)
            completed = handoff.call_args.args[2]
            self.assertEqual(completed['human_review_handoffs'], authorized)
            self.assertEqual(completed['review_cycle'], self.cycle)
            self.assertEqual(completed['review_attempt'], 2)

    def test_all_findings_can_be_explicitly_deferred_without_manufacturing_a_pass(self):
        with ExitStack() as stack:
            self.fixture(stack)
            self.kwargs['decisions'][0]['decision'] = 'defer_ignore'
            service.request_review_revision(**self.kwargs)
            update = next(c for c in self.cur.execute.call_args_list if 'UPDATE agent.runs SET result_metadata' in c.args[0])
            metadata = json.loads(update.args[1][0])
            self.assertEqual(metadata['review_findings'], [])
            self.assertEqual(metadata['human_review_handoffs'][0]['decisions'][0]['decision'], 'defer_ignore')
            self.assertEqual(metadata['review_attempt'], 1)
            self.assertNotIn('independent_review', metadata)

    def test_handoff_insert_failure_rolls_back_everything(self):
        with ExitStack() as stack:
            self.fixture(stack)
            def execute(sql, args):
                if 'INSERT INTO agent.events' in sql: raise RuntimeError('synthetic insert failure')
            self.cur.execute.side_effect = execute
            with self.assertRaises(RuntimeError): service.request_review_revision(**self.kwargs)
            self.conn.commit.assert_not_called()
            self.conn.rollback.assert_called_once()

    def test_instructions_survive_followup_rounds_reviewer_and_implementation_recovery(self):
        history = [{'actor_user_id': str(uuid4()), 'notes': 'Defer cosmetic issue; fix required defect',
                    'decisions': [{'finding_index': 0, 'decision': 'defer_ignore', 'finding': FINDING}]}]
        implementation_claim = replace(self.claim, phase=RunPhase.IMPLEMENTATION,
            result_metadata={'review_cycle': self.cycle, 'review_attempt': 1, 'human_review_handoffs': history})
        self.assertIn(history[0]['notes'], _review_correction_prompt(implementation_claim))
        cur = MagicMock()
        cur.fetchone.return_value = {'requested_by': str(uuid4())}
        with patch.object(queue, '_row_to_dict', side_effect=lambda c, r: r), patch.object(queue, '_insert_event'):
            queue._review_handoff(cur, implementation_claim, {**self.metadata, 'human_review_handoffs': history}, CardStatus.REVIEW_QUEUED, 'message')
            queued = next(c for c in cur.execute.call_args_list if 'INSERT INTO agent.runs' in c.args[0])
            source = json.loads(queued.args[1][6])['review_source']
            self.assertEqual(source['human_review_handoffs'], history)
            source['implementation_run_id'] = self.run_id
            reviewer_claim = replace(self.claim, result_metadata={'review_source': source})
            result = self.executor.execute(reviewer_claim)
            self.assertEqual(result.card_status, CardStatus.REVIEW_READY)
            self.assertEqual(json.loads(self.gateway.run_turn.call_args.kwargs['prompt'])['human_review_handoffs'], history)
            evidence = {**result.metadata['independent_review'], 'verdict': 'changes_requested',
                        'findings': [FINDING, {**FINDING, 'kind': 'advisory'}]}
            cur.reset_mock()
            queue._review_handoff(cur, reviewer_claim, {'independent_review': evidence}, CardStatus.IMPLEMENTATION_QUEUED, 'message')
            queued = next(c for c in cur.execute.call_args_list if 'INSERT INTO agent.runs' in c.args[0])
            correction = json.loads(queued.args[1][6])
            self.assertEqual(correction['human_review_handoffs'], history)
            self.assertEqual(correction['review_findings'], [FINDING])
            prompt = _review_correction_prompt(replace(implementation_claim, result_metadata=correction))
            self.assertIn(history[0]['notes'], prompt)
            self.assertIn('Required defect', prompt)


class FrontendRequestTests(unittest.TestCase):
    def test_actual_typescript_client_sends_exact_revision_body(self):
        # Node's native TS erasure runs the real client with only its network boundary mocked.
        source = Path('frontend-web/src/api/agentApi.ts').read_text()
        source = source.replace('import { apiRequest } from "./authenticatedApi";', '''
const calls: any[] = [];
async function apiRequest<T>(path: string, options: any): Promise<any> {
  calls.push({path, ...options}); return {data: {id: "card"}};
}''')
        script = '''
const card: any = {id: "card", revision: 7, status: "blocked", allowed_actions: [AgentActions.REQUEST_REVISION, AgentActions.CANCEL],
  latest_run: {id: "review-3"}, runs: [{id: "review-3", phase: "review", status: "succeeded", card_revision: 7,
    independent_review: {verdict: "changes_requested", review_attempt: 3, candidate_digest: "a".repeat(64),
      findings: [{description: "required"}, {kind: "advisory", description: "optional"}]}}]};
for (const decisions of [{}, {0: "fix"}, {0: "fix", 1: "fix", 2: "fix"}]) {
  if (reviewRevisionForCard(card, decisions as any, "notes") !== null) throw Error("accepted incomplete decisions");
}
if(reviewRevisionForCard(card, {0: "fix", 1: "defer_ignore"}, " ") !== null) throw Error("blank notes");
if(reviewRevisionForCard({...card, latest_run: {id: "another-review"}}, {0: "fix", 1: "defer_ignore"}, "notes") !== null) throw Error("stale review");
const revision = reviewRevisionForCard(card, {0: "fix", 1: "defer_ignore"}, "operator scope");
if(!revision || revision.review_run_id !== "review-3") throw Error("wrong review identity");
await performAgentAction({id: "card"} as any, AgentActions.REQUEST_REVISION, "  operator scope  ", revision as any);
const sent = JSON.parse(calls[0].body);
if(calls[0].path !== "/agent/cards/card/request-revision" || sent.notes !== "operator scope" ||
   sent.review_run_id !== "review-3" || sent.decisions[1].decision !== "defer_ignore") throw Error("wrong request");
for (const notes of ["", " "]) {
  let rejected = false;
  try { await performAgentAction({id: "card"} as any, AgentActions.REQUEST_REVISION, notes, revision as any); }
  catch { rejected = true; }
  if (!rejected) throw Error("accepted blank notes");
}
if(calls.length !== 1) throw Error("unexpected request");
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contract.mts'
            path.write_text(source + script)
            result = subprocess.run(['node', '--experimental-strip-types', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_form_requires_each_explicit_decision_and_resets_on_new_review(self):
        screen = Path('frontend-web/src/AgentScreen.tsx').read_text()
        self.assertIn('reviewRevisionForCard(card, decisions, notes)', screen)
        self.assertIn('(requestingRevision && !revisionReady)', screen)
        self.assertIn('key={`${card.id}-${card.revision}-${card.latest_run?.id}`}', screen)
        self.assertIn('Required correction', screen)
        self.assertIn('Advisory suggestion', screen)
