"""Actual queue/lease/API-service/trigger transitions against a disposable cluster.

No production/QA configuration is loaded. The cluster has a private Unix socket,
no TCP listener, and an explicit test-only role/database.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.agent_worker import ExecutionResult, AgentLeaseLostError
from backend.core.codex_review import CodexReviewExecutor
from backend.services import agent_service as service, agent_worker_service as queue
from tests import test_review_replacement as candidates


class PostgreSQLWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='review-workflow-pg-')
        cls.root=Path(cls.tmp.name);cls.socket=cls.root/'socket';cls.socket.mkdir(mode=0o700)
        cls.bin=Path(os.environ.get('REMIHUB_TEST_POSTGRES_BIN','/usr/lib/postgresql/16/bin'))
        if not (cls.bin/'initdb').is_file():
            raise unittest.SkipTest('Disposable PostgreSQL tools unavailable')
        cls.data=cls.root/'data'
        subprocess.run([str(cls.bin/'initdb'),'-D',str(cls.data),'-U','reviewer_fixture','--auth=trust','--no-locale','--encoding=UTF8'],check=True,capture_output=True)
        subprocess.run([str(cls.bin/'pg_ctl'),'-D',str(cls.data),'-l',str(cls.root/'postgres.log'),
            '-o',f"-F -k {cls.socket} -p 55493 -c listen_addresses=''",'-w','start'],check=True,capture_output=True)
        cls.addClassCleanup(cls.stop_cluster)
        conn=psycopg2.connect(host=str(cls.socket),port=55493,user='reviewer_fixture',dbname='postgres');conn.autocommit=True
        with conn.cursor() as cur:cur.execute('CREATE DATABASE review_fixture')
        conn.close()
        with cls.connect() as conn:
            with conn.cursor() as cur:
                cur.execute('CREATE TABLE public.remihub_users(id uuid PRIMARY KEY)')
                cur.execute('CREATE FUNCTION public.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.updated_at=CURRENT_TIMESTAMP; RETURN NEW; END $$')
                migrations=Path(__file__).resolve().parents[1]/'backend/database/migrations'
                for prefix in ['0003','0004','0005','0020','0021','0022']:
                    path=next(migrations.glob(prefix+'*.up.sql'));cur.execute(path.read_text())

    @classmethod
    def stop_cluster(cls):
        subprocess.run([str(cls.bin/'pg_ctl'),'-D',str(cls.data),'-m','immediate','-w','stop'],check=True,capture_output=True)
        cls.tmp.cleanup()

    @classmethod
    def connect(cls):
        return psycopg2.connect(host=str(cls.socket),port=55493,user='reviewer_fixture',dbname='review_fixture')

    def setUp(self):
        self.fixture=candidates.ReviewCandidateTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        for target in ['backend.services.agent_service.get_db_conn','backend.services.agent_worker_service.get_db_conn']:
            p=patch(target,side_effect=self.connect);p.start();self.addCleanup(p.stop)
        for target in ['backend.services.agent_service.put_db_conn','backend.services.agent_worker_service.put_db_conn']:
            p=patch(target,side_effect=lambda conn:conn.close());p.start();self.addCleanup(p.stop)
        p=patch('backend.services.agent_worker_service.insert_notification');p.start();self.addCleanup(p.stop)
        self.user=str(uuid4());self.card=str(uuid4())
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute('TRUNCATE agent.cards CASCADE')
                cur.execute('INSERT INTO public.remihub_users VALUES (%s)',(self.user,))
                cur.execute("INSERT INTO agent.cards(id,title,description,status,repository_scope,created_by) VALUES (%s,'Fixture','Refresh dashboard','implementation_queued','backend',%s)",(self.card,self.user))
                cur.execute("INSERT INTO agent.approvals(card_id,approval_type,decision,card_revision,decided_by) VALUES (%s,'implementation','approved',1,%s)",(self.card,self.user))

    def query(self,sql,args=()):
        with self.connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql,args)
                return [dict(r) for r in cur.fetchall()] if cur.description else []

    def claim(self,phase):
        claim=queue.claim_next_run(worker_id='fixture-worker',lease_seconds=120,
            allowed_phases=frozenset({phase}),allowed_repository_scopes=frozenset({RepositoryScope.BACKEND}))
        self.assertIsNotNone(claim);queue.start_run(claim,lease_seconds=120);return claim

    def implement(self,*,fixed,initial=False):
        candidate=self.fixture.claim(fixed=fixed);meta=candidate.result_metadata['review_source']['implementation_metadata']
        if initial:
            impl=candidate.result_metadata['review_source']['implementation_run_id']
            self.query("INSERT INTO agent.runs(id,card_id,phase,status,card_revision,requested_by,review_cycle,review_attempt,result_metadata) VALUES (%s,%s,'implementation','queued',1,%s,%s,1,%s::jsonb)",
                (impl,self.card,self.user,impl,json.dumps({'review_cycle':impl,'review_attempt':1,'correction_allowance':3})))
        claim=self.claim(RunPhase.IMPLEMENTATION)
        from backend.core.codex_review import candidate_digest
        meta['candidate_digest']=candidate_digest(meta,run_id=claim.id,revision=1)
        queue.complete_run(claim,ExecutionResult(message='Synthetic implementation completed',card_status=CardStatus.REVIEW_QUEUED,metadata=meta))
        return self.claim(RunPhase.REVIEW)

    def review(self,claim):
        executor=CodexReviewExecutor(artifact_root=self.fixture.artifacts,repository_scope=RepositoryScope.BACKEND,
            gateway=candidates.InspectingReviewer(),model='synthetic-fixture')
        result=executor.execute(claim);queue.complete_run(claim,result);return result

    def detail(self):
        with self.connect() as conn:return service._card_detail(conn,self.card)

    def test_real_implementation_review_approval_and_separate_human_deployment_gate(self):
        claim=self.implement(fixed=True,initial=True);self.review(claim)
        card=self.detail();self.assertEqual(card['status'],'review_ready')
        with self.assertRaises(psycopg2.Error):self.query("UPDATE agent.cards SET status='deployment_queued' WHERE id=%s",(self.card,))
        self.query("INSERT INTO agent.approvals(card_id,approval_type,decision,card_revision,decided_by) VALUES (%s,'deployment','approved',1,%s)",(self.card,self.user))
        self.query("UPDATE agent.cards SET status='deployment_queued' WHERE id=%s",(self.card,))
        self.assertEqual(self.detail()['status'],'deployment_queued')

    def test_repeated_corrections_pause_continue_then_approve_preserves_all_history(self):
        claim=self.implement(fixed=False,initial=True)
        for attempt in range(1,5):
            result=self.review(claim)
            self.assertEqual(result.metadata['independent_review']['review_attempt'],attempt)
            if attempt<4:claim=self.implement(fixed=False)
        card=self.detail();self.assertEqual(card['status'],'review_corrections_paused')
        self.assertIn('continue_corrections',card['allowed_actions'])
        evidence=result.metadata['independent_review']
        continued=service.continue_corrections(card_id=self.card,review_run_id=claim.id,
            candidate_digest=evidence['candidate_digest'],requested_by=self.user)
        self.assertEqual(continued['status'],'implementation_queued')
        with self.assertRaises(service.AgentStateConflictError):
            service.continue_corrections(card_id=self.card,review_run_id=claim.id,
                candidate_digest=evidence['candidate_digest'],requested_by=self.user)
        self.review(self.implement(fixed=True))
        self.assertEqual(self.detail()['status'],'review_ready')
        reviews=self.query("SELECT result_metadata FROM agent.runs WHERE phase='review'")
        self.assertEqual(len(reviews),5)
        self.assertEqual(sum(r['result_metadata']['independent_review']['decision']=='request_changes' for r in reviews),4)

    def test_validation_execution_retry_preserves_correction_context(self):
        review=self.implement(fixed=False,initial=True);self.review(review)
        correction=self.claim(RunPhase.IMPLEMENTATION)
        before=correction.result_metadata.copy()
        queue.fail_run(correction,error_message='ImplementationValidationExecutionError: fixture validator interruption')
        service.retry_card(card_id=self.card,requested_by=self.user)
        resumed=self.claim(RunPhase.IMPLEMENTATION)
        self.assertEqual(resumed.result_metadata,before)
        self.assertEqual(resumed.result_metadata['review_attempt'],2)
        self.assertEqual(resumed.result_metadata['correction_allowance'],3)

    def test_operational_retries_keep_candidate_ordinal_and_allow_manual_retry_after_budget(self):
        claim=self.implement(fixed=True,initial=True)
        identity=claim.result_metadata['review_source']
        with patch.dict(os.environ,{'REMIHUB_REVIEW_EXECUTION_RETRIES':'1','REMIHUB_REVIEW_RETRY_SECONDS':'1'}):
            queue.fail_run(claim,error_message='Synthetic model timeout')
            self.query("UPDATE agent.runs SET available_at=CURRENT_TIMESTAMP WHERE id=%s",(claim.id,))
            retried=self.claim(RunPhase.REVIEW)
            self.assertEqual(retried.id,claim.id);self.assertEqual(retried.result_metadata['review_source'],identity)
            queue.fail_run(retried,error_message='Synthetic malformed output')
            card=self.detail();self.assertEqual(card['status'],'failed');self.assertIn('retry_review',card['allowed_actions'])
            service.retry_review(card_id=self.card,review_run_id=claim.id,candidate_digest=identity['implementation_metadata']['candidate_digest'],requested_by=self.user)
            resumed=self.claim(RunPhase.REVIEW)
            self.assertEqual(resumed.result_metadata['review_source'],identity)
            self.review(resumed)
        self.assertEqual(len(self.query("SELECT id FROM agent.runs WHERE phase='implementation'")),1)
        self.assertEqual(len(self.query("SELECT id FROM agent.events WHERE event_type='run.failed'")),2)

    def test_repeated_review_lease_expiry_stops_and_admin_retry_grants_new_window(self):
        from backend.core.agent_worker import AgentWorker
        from unittest.mock import Mock
        claim = self.implement(fixed=True, initial=True)
        source = claim.result_metadata['review_source']
        with patch.dict(os.environ, {'REMIHUB_REVIEW_EXECUTION_RETRIES': '1'}):
            # Simulate two abrupt deaths: neither process reaches fail_run.
            for attempt in [1, 2]:
                self.assertEqual(claim.attempt_count, attempt)
                self.query("UPDATE agent.runs SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE id=%s", (claim.id,))
                if attempt == 1:
                    claim = self.claim(RunPhase.REVIEW)
            executor = Mock(allowed_phases=frozenset({RunPhase.REVIEW}),
                            allowed_repository_scopes=frozenset({RepositoryScope.BACKEND}))
            worker = AgentWorker(queue=queue.DatabaseAgentQueue(environment='production'),
                                 executor=executor, worker_id='post-crash-worker',
                                 lease_seconds=120, heartbeat_seconds=30, max_attempts=3)
            self.assertTrue(worker.process_once())
            executor.execute.assert_not_called()
            card = self.detail()
            self.assertEqual(card['status'], 'failed')
            self.assertIn('retry_review', card['allowed_actions'])
            failed = self.query('SELECT * FROM agent.runs WHERE id=%s', (claim.id,))[0]
            self.assertEqual(failed['result_metadata']['review_source'], source)
            self.assertIn('lost its lease', failed['error_message'])
            self.assertEqual(failed['review_attempt'], 1)
            service.retry_review(card_id=self.card, review_run_id=claim.id,
                candidate_digest=source['implementation_metadata']['candidate_digest'], requested_by=self.user)
            resumed = self.claim(RunPhase.REVIEW)
            self.assertGreater(resumed.attempt_count, 3)
            self.assertGreaterEqual(resumed.result_metadata['review_execution_allowance'], resumed.attempt_count)
            self.assertEqual(resumed.result_metadata['review_source'], source)
            self.review(resumed)
        self.assertEqual(len(self.query("SELECT id FROM agent.runs WHERE phase='implementation'")), 1)
        self.assertEqual(self.detail()['status'], 'review_ready')

    def test_concurrent_claims_and_stale_lease_completion(self):
        claim=self.implement(fixed=True,initial=True)
        # Return the claimed review to queued for concurrent claim rehearsal.
        self.query("UPDATE agent.runs SET status='queued',lease_token=NULL,lease_expires_at=NULL WHERE id=%s",(claim.id,))
        self.query("UPDATE agent.cards SET status='review_queued' WHERE id=%s",(self.card,))
        def compete(i):return queue.claim_next_run(worker_id='competitor-'+str(i),lease_seconds=120,
            allowed_phases=frozenset({RunPhase.REVIEW}),allowed_repository_scopes=frozenset({RepositoryScope.BACKEND}))
        with ThreadPoolExecutor(max_workers=2) as pool:claims=list(pool.map(compete,[1,2]))
        self.assertEqual(sum(c is not None for c in claims),1)
        with self.assertRaises(AgentLeaseLostError):queue.heartbeat_run(claim,lease_seconds=120)

    def test_qa_handoff_record_survives_production_claim(self):
        review_claim=self.implement(fixed=True,initial=True);self.review(review_claim)
        source=review_claim.result_metadata['review_source'];deployment=str(uuid4());approval=str(uuid4())
        self.query("INSERT INTO agent.approvals(id,card_id,approval_type,decision,card_revision,decided_by) VALUES (%s,%s,'deployment','approved',1,%s)",(approval,self.card,self.user))
        self.query("UPDATE agent.cards SET status='deployment_queued' WHERE id=%s",(self.card,))
        self.query("INSERT INTO agent.runs(id,card_id,phase,status,card_revision,requested_by) VALUES (%s,%s,'deployment','queued',1,%s)",(deployment,self.card,self.user))
        binding={'run_id':deployment,'approval_id':approval,'implementation_run_id':source['implementation_run_id']}
        self.query("INSERT INTO agent.events(card_id,event_type,actor_type,payload) VALUES (%s,'card.deployment_approved','system',%s::jsonb)",(self.card,json.dumps(binding)))
        qa=self.claim(RunPhase.DEPLOYMENT)
        pipeline={'stage':'qa_succeeded','qa_verification':{'fixture':True,'success':True},'candidate_digest':source['implementation_metadata']['candidate_digest']}
        queue.complete_run(qa,ExecutionResult(message='Synthetic QA handoff',card_status=CardStatus.COMPLETED,metadata={'deployment_pipeline':pipeline}))
        self.assertIsNone(queue.claim_next_run(worker_id='qa',lease_seconds=120,
            allowed_phases=frozenset({RunPhase.DEPLOYMENT}),deployment_environment='qa'))
        production=queue.claim_next_run(worker_id='production',lease_seconds=120,
            allowed_phases=frozenset({RunPhase.DEPLOYMENT}),deployment_environment='production')
        self.assertEqual(production.id,deployment)
        self.assertEqual(production.result_metadata['deployment_pipeline'],pipeline)
        self.assertEqual(production.deployment_source.implementation_result_metadata['independent_review']['decision'],'approve')

    def test_historical_mixed_blocked_review_recovers_through_audited_corrections(self):
        claim=self.implement(fixed=False,initial=True);result=self.review(claim)
        # Historical rows are synthesized only in this disposable test database.
        evidence=result.metadata['independent_review'].copy();evidence.pop('decision');evidence['verdict']='blocked'
        evidence['findings']=[{'path':'backend/api.py','description':'Missing dossier context','correction':'Inspect source'},
                              {'path':'frontend-web/Dashboard.tsx','description':'Refresh defect','correction':'Refresh summary'}]
        metadata=self.query('SELECT result_metadata FROM agent.runs WHERE id=%s',(claim.id,))[0]['result_metadata']
        metadata['independent_review']=evidence
        # Old blocked reviews did not queue an implementation correction.
        self.query("DELETE FROM agent.runs WHERE card_id=%s AND phase='implementation' AND status='queued'",(self.card,))
        self.query("DELETE FROM agent.events WHERE card_id=%s AND event_type='review.correction_queued'",(self.card,))
        self.query("UPDATE agent.runs SET result_metadata=%s::jsonb WHERE id=%s",(json.dumps(metadata),claim.id))
        self.query("UPDATE agent.cards SET status='blocked',resume_status='review_queued',blocked_reason='Historical review',blocked_until='infinity' WHERE id=%s",(self.card,))
        card=self.detail();self.assertIn('continue_corrections',card['allowed_actions']);self.assertNotIn('retry_review',card['allowed_actions'])
        service.continue_corrections(card_id=self.card,review_run_id=claim.id,candidate_digest=evidence['candidate_digest'],
            requested_by=self.user,notes='Source context is now inspectable; correct the actual refresh defect.',finding_indexes=[1])
        original=self.query('SELECT result_metadata FROM agent.runs WHERE id=%s',(claim.id,))[0]['result_metadata']
        self.assertEqual(original['independent_review'],evidence)
        self.assertEqual(len(self.query('SELECT id FROM agent.approvals')),1)
        correction=self.query("SELECT result_metadata FROM agent.runs WHERE phase='implementation' AND status='queued'")[0]['result_metadata']
        self.assertEqual(correction['review_findings'],[evidence['findings'][1]])
        self.assertEqual(len(self.query("SELECT id FROM agent.events WHERE event_type='review.corrections_continued'")),1)
        # No new decision, paused status or >3 ordinal exists: the queued
        # historical continuation itself must prevent an incompatible downgrade.
        self.assertFalse(self.query("SELECT id FROM agent.runs WHERE result_metadata #>> '{independent_review,decision}' IS NOT NULL"))
        down=Path(__file__).resolve().parents[1]/'backend/database/migrations/0022_review_decisions.down.sql'
        with self.assertRaises(psycopg2.Error):self.query(down.read_text())

    def test_unused_schema_can_roll_back_without_discarding_data(self):
        down=Path(__file__).resolve().parents[1]/'backend/database/migrations/0022_review_decisions.down.sql'
        with self.connect() as conn:
            with conn.cursor() as cur:cur.execute(down.read_text())
            # Keep the fixture schema expanded for subsequent tests.
            conn.rollback()

    def test_rollback_refuses_to_discard_new_decisions(self):
        self.review(self.implement(fixed=True,initial=True))
        down=Path(__file__).resolve().parents[1]/'backend/database/migrations/0022_review_decisions.down.sql'
        with self.assertRaises(psycopg2.Error):self.query(down.read_text())

if __name__=='__main__':unittest.main()
