"""Exercise the API handoff and real workspace refresh as one recovery path."""
import json
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from backend.services import agent_service as service
from tests import test_agent_workspace as workspace_fixture


class DeploymentReturnRefreshTests(unittest.TestCase):
    def queue_recovery(self, *, production=False):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        card = {"id": "3d8549c4-a965-4d2e-aacf-9df7e6ccdbb4", "status": "failed",
                "revision": 2, "repository_scope": "backend"}
        failed = {"id": "old-deployment", "phase": "deployment", "status": "failed",
                  "card_revision": 2,
                  "error_message": "AgentDeploymentError: Deployment target advanced before candidate creation",
                  "result_metadata": {"environment": "production"} if production else {}}
        with patch.object(service, "get_db_conn", return_value=conn), \
             patch.object(service, "put_db_conn"), \
             patch.object(service, "_locked_card", return_value=card), \
             patch.object(service, "_row_to_dict", return_value=failed), \
             patch.object(service, "_insert_message", return_value="message"), \
             patch.object(service, "_insert_event") as event, \
             patch.object(service, "_card_detail", return_value={"revision": 3}):
            if production:
                with self.assertRaises(service.AgentStateConflictError):
                    service.return_to_implementation(card_id=card["id"], requested_by="admin", instructions="Fix migration")
                conn.rollback.assert_called_once()
                conn.commit.assert_not_called()
                self.assertFalse(any("UPDATE agent.runs" in call.args[0] for call in cur.execute.call_args_list))
                return
            service.return_to_implementation(card_id=card["id"], requested_by="admin", instructions="Fix migration")
        metadata = None
        for call in cur.execute.call_args_list:
            sql, args = call.args
            if "INSERT INTO agent.runs" in sql:
                run_id = args[0]
                self.assertEqual(args[4], 3)
                metadata = json.loads(args[9])
            elif "UPDATE agent.runs SET result_metadata" in sql:
                self.assertEqual(args[1], run_id)
                metadata.update(json.loads(args[0]))
        self.assertEqual(metadata["review_cycle"], run_id)
        self.assertEqual(metadata["review_attempt"], 1)
        self.assertTrue(event.call_args.kwargs["payload"]["refresh_base_requested"])
        conn.commit.assert_called_once()
        return run_id, metadata

    def test_return_after_baseline_advance_preserves_work_and_freezes_current_base(self):
        run_id, metadata = self.queue_recovery()
        fixture = workspace_fixture.GitImplementationWorkspaceManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        claim, old_base, original_patch, original_bytes = fixture._advanced_correction(conflict=False)
        claim = replace(claim, id=run_id, card_revision=3, result_metadata=metadata)
        with fixture.manager.locked_workspace(claim, persist_workspace=lambda *_: None) as workspace:
            self.assertNotEqual(workspace.base_commit, old_base)
            self.assertEqual(workspace.base_commit, workspace_fixture._git(fixture.source, "rev-parse", "main"))
            self.assertEqual((workspace.path / "tracked.txt").read_text(), "candidate change\n")
            self.assertEqual((workspace.path / "new.txt").read_text(), "preserve new source\n")
            self.assertTrue(workspace.base_refresh["preserved_stash"])
            fixture.manager.prepare_validation(workspace)
            snapshot = fixture.manager.capture_snapshot(claim, workspace)
            self.assertEqual(snapshot.head_commit, workspace.base_commit)
            self.assertEqual(snapshot.changed_files, ("new.txt", "tracked.txt"))
        self.assertEqual(original_patch.read_bytes(), original_bytes)

    def test_production_failure_cannot_authorize_workspace_refresh(self):
        self.queue_recovery(production=True)
