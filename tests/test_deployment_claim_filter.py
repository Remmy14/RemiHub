"""Exercise the actual scheduler predicate with PostgreSQL NULL/JSON semantics.

Use a read-only connection to a disposable test database or PostgreSQL's postgres
maintenance database: the test uses synthetic VALUES only and creates no objects.
"""
import os
import re
import unittest
from unittest.mock import MagicMock, patch
import psycopg2
from backend.core.agent_state import RunPhase
from backend.services import agent_worker_service as service


@unittest.skipUnless(os.environ.get("REMIHUB_TEST_POSTGRES_DSN"), "Read-only PostgreSQL test connection not configured")
class DeploymentClaimFilterTests(unittest.TestCase):
    def test_qa_handoff_and_expired_execution_remain_claimable_but_permanent_block_does_not(self):
        fake = MagicMock()
        fake.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        with patch.object(service, 'get_db_conn', return_value=fake), patch.object(service, 'put_db_conn'):
            service.claim_next_run(worker_id='filter-test', lease_seconds=120,
                allowed_phases=frozenset({RunPhase.DEPLOYMENT}), deployment_environment='production')
        sql = fake.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        # Locate the recovery metadata predicate rather than duplicating it.
        clause = re.search(r"AND \(\s*(?:runs.status <> 'blocked'\s*OR\s*)?runs.phase <> 'deployment'\s*OR \(runs.result_metadata #>>.*?IS DISTINCT FROM 'false'\s*\)", sql, re.S)
        self.assertIsNotNone(clause)
        conn = psycopg2.connect(os.environ['REMIHUB_TEST_POSTGRES_DSN'])
        conn.set_session(readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("""WITH runs(label,status,phase,result_metadata) AS (VALUES
                    ('qa-handoff','queued','deployment','{"deployment_recovery":{"retryable":false}}'::jsonb),
                    ('expired-production','running','deployment','{"deployment_recovery":{"retryable":false}}'::jsonb),
                    ('permanent','blocked','deployment','{"deployment_recovery":{"retryable":false}}'::jsonb),
                    ('temporary','blocked','deployment','{"deployment_recovery":{"retryable":true}}'::jsonb),
                    ('legacy','blocked','deployment','{}'::jsonb),
                    ('unrelated','blocked','planning','{}'::jsonb))
                    SELECT label FROM runs WHERE true """ + clause.group(0) + ' ORDER BY label')
                self.assertEqual([r[0] for r in cur.fetchall()],
                    ['expired-production','legacy','qa-handoff','temporary','unrelated'])
        finally:
            conn.rollback()
            conn.close()
