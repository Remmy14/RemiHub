import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from backend.core.auth import AuthenticatedPrincipal, require_admin_principal
from backend.routers import agent
from backend.models.agent_models import AgentBacklogWrite
from backend.services import agent_backlog_service as backlog
from backend.services.agent_service import AgentConflictError
from backend.core.agent_deployment import GitBackendDeploymentManager


ITEM_ID = "f67f7c7a-70cf-42bf-a4ae-6868981cc537"
CARD_ID = "2ee985cd-78dc-4e1f-8f51-62507fe1b55a"
USER_ID = "c346f3f4-3867-4ddb-83ea-7d24db8817bc"
NOW = "2026-11-23T00:00:00+00:00"
COLUMNS = [part.strip() for part in backlog.COLUMNS.split(",")]


def item(*, promoted_card_id=None):
    return dict(id=ITEM_ID, title="Detailed request", prompt="  first line\nsecond line\n",
                priority="Medium", created_by=USER_ID, promoted_card_id=promoted_card_id,
                promoted_at=NOW if promoted_card_id else None,
                created_at=NOW, updated_at=NOW)


def row(value):
    return tuple(value[column] for column in COLUMNS)


class BacklogServiceTests(unittest.TestCase):
    def connection(self, *rows):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.description = [(name,) for name in COLUMNS]
        cur.fetchone.side_effect = rows
        return conn, cur

    @patch.object(backlog.agent_service, "put_db_conn")
    @patch.object(backlog.agent_service, "get_db_conn")
    def test_create_only_inserts_backlog_record_and_preserves_prompt(self, get_conn, _put):
        conn, cur = self.connection(row(item()))
        get_conn.return_value = conn
        result = backlog.create(title=item()["title"], prompt=item()["prompt"],
                                priority="Medium", created_by=USER_ID)
        self.assertEqual(result["prompt"], item()["prompt"])
        self.assertEqual(cur.execute.call_count, 1)
        self.assertIn("INSERT INTO agent.backlog_items", cur.execute.call_args.args[0])
        self.assertNotIn("agent.runs", cur.execute.call_args.args[0])
        conn.commit.assert_called_once()

    @patch.object(backlog.agent_service, "put_db_conn")
    @patch.object(backlog.agent_service, "get_db_conn")
    @patch.object(backlog.agent_service, "_insert_initial_card", return_value=CARD_ID)
    def test_promotion_links_the_normal_card_in_one_transaction(self, insert_card, get_conn, _put):
        conn, cur = self.connection(row(item()))
        get_conn.return_value = conn
        result = backlog.send_to_planning(ITEM_ID, created_by=USER_ID)
        self.assertEqual(result["card_id"], CARD_ID)
        insert_card.assert_called_once_with(
            cur, title=item()["title"], description=item()["prompt"], created_by=USER_ID,
        )
        self.assertIn("FOR UPDATE", cur.execute.call_args_list[0].args[0])
        self.assertIn("promoted_card_id", cur.execute.call_args_list[1].args[0])
        self.assertIn("updated_at = CURRENT_TIMESTAMP", cur.execute.call_args_list[1].args[0])
        conn.commit.assert_called_once()

    @patch.object(backlog.agent_service, "put_db_conn")
    @patch.object(backlog.agent_service, "get_db_conn")
    @patch.object(backlog.agent_service, "_insert_initial_card")
    def test_retry_returns_existing_card_without_another_insert(self, insert_card, get_conn, _put):
        conn, _cur = self.connection(row(item(promoted_card_id=CARD_ID)))
        get_conn.return_value = conn
        self.assertEqual(backlog.send_to_planning(ITEM_ID, created_by=USER_ID)["card_id"], CARD_ID)
        insert_card.assert_not_called()
        conn.commit.assert_called_once()

    @patch.object(backlog.agent_service, "put_db_conn")
    @patch.object(backlog.agent_service, "get_db_conn")
    @patch.object(backlog.agent_service, "_insert_initial_card", side_effect=AgentConflictError("open card"))
    def test_failed_promotion_rolls_back_draft_and_card(self, _insert, get_conn, _put):
        conn, _cur = self.connection(row(item()))
        get_conn.return_value = conn
        with self.assertRaises(AgentConflictError):
            backlog.send_to_planning(ITEM_ID, created_by=USER_ID)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_limits_and_blank_values(self):
        for values in (("x" * 161, "prompt", "Medium"),
                       ("title", "x" * 20001, "Medium"),
                       ("title", " \n ", "Medium"),
                       ("title", "prompt", "Urgent")):
            with self.subTest(values=values[:1]), self.assertRaises(ValueError):
                backlog._validate(*values)

    @patch.object(backlog.agent_service, "put_db_conn")
    @patch.object(backlog.agent_service, "get_db_conn")
    def test_update_and_delete_touch_only_unsubmitted_draft(self, get_conn, _put):
        original = item()
        edited = {**original, "title": "Edited", "prompt": "line one\nline two", "priority": "High"}
        conn, cur = self.connection(row(original), row(edited), row(original))
        get_conn.return_value = conn
        saved = backlog.update(ITEM_ID, title="Edited", prompt="line one\nline two", priority="High")
        self.assertEqual(saved["prompt"], "line one\nline two")
        backlog.delete(ITEM_ID)
        statements = [call.args[0] for call in cur.execute.call_args_list]
        self.assertEqual(sum("FOR UPDATE" in statement for statement in statements), 2)
        self.assertTrue(any("DELETE FROM agent.backlog_items" in statement for statement in statements))
        self.assertFalse(any("DELETE FROM agent.cards" in statement for statement in statements))
        self.assertTrue(any("updated_at = CURRENT_TIMESTAMP" in statement for statement in statements))

    @patch.object(backlog.agent_service, "put_db_conn")
    @patch.object(backlog.agent_service, "get_db_conn")
    def test_promoted_draft_cannot_be_edited_or_deleted(self, get_conn, _put):
        conn, cur = self.connection(row(item(promoted_card_id=CARD_ID)),
                                    row(item(promoted_card_id=CARD_ID)))
        get_conn.return_value = conn
        with self.assertRaises(AgentConflictError):
            backlog.update(ITEM_ID, title="Edited", prompt="prompt", priority="High")
        with self.assertRaises(AgentConflictError):
            backlog.delete(ITEM_ID)
        self.assertEqual(cur.execute.call_count, 2)

    @patch.object(backlog.agent_service, "put_db_conn")
    @patch.object(backlog.agent_service, "get_db_conn")
    def test_list_uses_priority_and_creation_order(self, get_conn, _put):
        conn, cur = self.connection()
        cur.fetchall.return_value = []
        get_conn.return_value = conn
        self.assertEqual(backlog.list_active(order="priority"), [])
        self.assertIn("CASE priority", cur.execute.call_args.args[0])
        self.assertIn("promoted_card_id IS NULL", cur.execute.call_args.args[0])
        backlog.list_active(order="created_at")
        self.assertNotIn("CASE priority", cur.execute.call_args.args[0])
        with self.assertRaises(ValueError):
            backlog.list_active(order="unknown")

    @patch.object(backlog.agent_service, "_insert_event")
    @patch.object(backlog.agent_service, "_insert_run", return_value="run-id")
    @patch.object(backlog.agent_service, "_insert_message", return_value="message-id")
    def test_shared_card_creation_still_queues_normal_planning_run(self, _message, run, _event):
        cur = MagicMock()
        backlog.agent_service._insert_initial_card(
            cur, title="Normal card", description="Plan this", created_by=USER_ID,
        )
        self.assertIn("INSERT INTO agent.cards", cur.execute.call_args.args[0])
        self.assertIn("planning_queued", cur.execute.call_args.args[1])
        self.assertEqual(run.call_args.kwargs["phase"].value, "planning")


class BacklogHttpTests(unittest.TestCase):
    def test_all_backlog_routes_require_administrator(self):
        backlog_routes = [route for route in agent.router.routes
                          if route.path.startswith("/agent/backlog")]
        self.assertEqual(len(backlog_routes), 6)
        for route in backlog_routes:
            self.assertIn(require_admin_principal,
                          [dependency.call for dependency in route.dependant.dependencies])

    @patch.object(backlog, "create")
    def test_create_uses_admin_principal_and_returns_full_prompt(self, create):
        principal = AuthenticatedPrincipal(
            id=USER_ID, firebase_uid="test", email="test@example.com", display_name="Test", role="admin")
        create.return_value = item()
        response = agent.create_backlog_item(
            AgentBacklogWrite(title=item()["title"], prompt=item()["prompt"]), principal)
        self.assertEqual(response["data"]["prompt"], item()["prompt"])
        create.assert_called_once_with(title=item()["title"], prompt=item()["prompt"],
                                       priority="Medium", created_by=USER_ID)


class BacklogMigrationTests(unittest.TestCase):
    def test_migration_is_accepted_by_deployment_policy(self):
        migration_dir = Path(__file__).resolve().parents[1] / "backend/database/migrations"
        up = migration_dir / "0028_agent_backlog.up.sql"
        down = migration_dir / "0028_agent_backlog.down.sql"
        GitBackendDeploymentManager._validate_migration_sql(up, direction="up")
        GitBackendDeploymentManager._validate_migration_sql(down, direction="down")
        GitBackendDeploymentManager._validate_migration_pair(up, down)
