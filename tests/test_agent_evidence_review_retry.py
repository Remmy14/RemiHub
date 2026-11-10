"""Admin-only Android parser evidence re-review, preserving the frozen v10 candidate."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.services import agent_service as svc

CARD = "b7c138e5-7f02-41e6-b590-7f57239e95ee"
RUN = "6f640dfb-db73-4aa8-aaa1-5d9aae13eb72"
IMPL = "ed0c04f0-9901-4c2e-abc1-a2ec44b06184"
DIGEST = "61dd1f184b0b9e470478bb9fc15776c353358d240b38aad8e607bf1f64fdcc1e"
ANDROID = "907083d48e4614d981ef5476795a378771d8f7c4"


def fixtures():
    metadata = {"candidate_digest": DIGEST, "repository_scope": "backend"}
    source = {"review_cycle": IMPL, "review_attempt": 1,
              "implementation_run_id": IMPL, "implementation_metadata": metadata}
    evidence = {
        "verdict": "blocked", "review_attempt": 1, "review_cycle": IMPL,
        "implementation_run_id": IMPL, "candidate_digest": DIGEST,
        "card_revision": 1, "boundary": "qualified-no-tools",
        "thread_id": "review-thread", "turn_id": "review-turn",
        "summary": "Insufficient Android compatibility evidence",
        "findings": [{
            "kind": "required", "severity": "medium",
            "path": "Android client stats response model/parser (not supplied)",
            "description": (
                "The approved plan requires independent confirmation. "
                "Android source and trusted compatibility-test evidence are absent."
            ),
            "correction": (
                "Supply the relevant Android response model and parsing code "
                "for reviewer inspection."
            ),
        }],
    }
    card = {"id": CARD, "status": "blocked", "resume_status": "review_queued",
            "repository_scope": "backend", "revision": 1}
    review = {"id": RUN, "phase": "review", "status": "succeeded", "card_revision": 1,
              "review_cycle": IMPL, "review_attempt": 1, "attempt_count": 2,
              "result_message_id": "prior-message", "worker_id": "review-backend",
              "started_at": "2026-11-10T07:00:00", "finished_at": "2026-11-10T07:01:00",
              "result_metadata": {"review_source": source, "independent_review": evidence}}
    impl = {"id": IMPL, "result_metadata": metadata}
    return card, review, impl


class AndroidEvidenceRetryTests(unittest.TestCase):
    def test_eligibility_is_only_missing_android_client_source(self):
        card, review, _ = fixtures()
        self.assertTrue(svc._retryable_android_client_evidence_review(card, review))
        changes = (
            (card, "status", "review_ready"),
            (card, "repository_scope", "android"),
            (review, "attempt_count", 3),
            (review, "review_attempt", 2),
        )
        for _, field, value in changes:
            with self.subTest(field=field, value=value):
                c, r, _ = fixtures()
                (c if field in c and field != "attempt_count" and field != "review_attempt" else r)[field] = value
                self.assertFalse(svc._retryable_android_client_evidence_review(c, r))
        for field, value in (("verdict", "changes_requested"),
                             ("boundary", "untrusted"),
                             ("findings", [])):
            with self.subTest(field=field):
                c, r, _ = fixtures()
                r["result_metadata"]["independent_review"][field] = value
                self.assertFalse(svc._retryable_android_client_evidence_review(c, r))

    def run_retry(self, *, card=None, review=None, impl=None, run_id=RUN,
                  digest=DIGEST, duplicate=False, bad_candidate=False,
                  android_commit=ANDROID):
        c, r, i = fixtures()
        card, review, impl = card or c, review or r, impl or i
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [review, impl, 1 if duplicate else None]
        cursor.rowcount = 1
        with (patch.object(svc, "get_db_conn", return_value=conn),
              patch.object(svc, "put_db_conn"),
              patch.object(svc, "_locked_card", return_value=card),
              patch.object(svc, "_row_to_dict", side_effect=lambda c, row: row),
              patch.object(svc, "_card_detail", return_value={"status": "review_queued"}),
              patch.object(svc, "_insert_event") as event,
              patch.object(svc, "_update_card_status") as update,
              patch.object(svc, "_verify_android_context_commit") as pin,
              patch("backend.core.codex_review.verify_candidate",
                    side_effect=(ValueError("changed") if bad_candidate else None)) as verify):
            try:
                result = svc.retry_review_with_android_evidence(
                    card_id=CARD, review_run_id=run_id,
                    candidate_digest=digest, android_commit=android_commit,
                    requested_by="admin", notes="Use committed Android client source")
                failure = None
            except Exception as exc:
                result, failure = None, exc
        return result, failure, conn, cursor, event, update, pin, verify

    def test_same_run_same_ordinal_preserves_original_verdict_and_budget(self):
        result, error, conn, cursor, event, update, pin, verify = self.run_retry()
        self.assertIsNone(error)
        self.assertEqual(result["status"], "review_queued")
        conn.commit.assert_called_once()
        verify.assert_called_once()
        pin.assert_called_once_with(ANDROID)
        self.assertEqual(event.call_args.kwargs["event_type"],
                         "review.evidence_retry_requested")
        payload = event.call_args.kwargs["payload"]
        self.assertEqual(payload["previous_execution"]["attempt_count"], 2)
        self.assertEqual(payload["previous_execution"]["result_message_id"], "prior-message")
        self.assertEqual(payload["previous_review"]["verdict"], "blocked")
        self.assertEqual(payload["evidence_correction"]["commit"], ANDROID)
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("UPDATE agent.runs SET" in query
                            and "attempt_count = 2" in query for query in sql))
        self.assertFalse(any("INSERT INTO agent.approvals" in query for query in sql))
        self.assertFalse(any("INSERT INTO agent.runs" in query for query in sql))
        self.assertFalse(any("attempt_count = 0" in query for query in sql))
        self.assertTrue(any('"evidence_correction"' in call.args[1][0]
                            for call in cursor.execute.call_args_list
                            if "UPDATE agent.runs SET" in call.args[0]))

    def test_wrong_digest_stale_review_duplicate_or_changed_candidate_block(self):
        for options in ({"digest": "a" * 64}, {"run_id": "stale"},
                        {"duplicate": True}, {"bad_candidate": True}):
            with self.subTest(options=options):
                _, error, conn, _, event, update, pin, _ = self.run_retry(**options)
                self.assertIsInstance(error, svc.AgentStateConflictError)
                conn.rollback.assert_called_once()
                event.assert_not_called()
                update.assert_not_called()

    def test_real_android_context_head_must_equal_requested_commit(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "source.kt").write_text("class Source {}\n")
            subprocess.run(["git", "-C", str(repo), "add", "source.kt"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                            "-c", "user.email=test@localhost", "commit", "-qm", "source"],
                           check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                             text=True).strip()
            with patch.object(svc, "_ANDROID_CLIENT_EVIDENCE_REPOSITORY", repo):
                svc._verify_android_context_commit(commit)
                with self.assertRaises(svc.AgentStateConflictError):
                    svc._verify_android_context_commit("0" * 40)
                with self.assertRaises(ValueError):
                    svc._verify_android_context_commit("not-a-git-commit")

    def test_other_required_finding_denies_evidence_retry(self):
        c, r, i = fixtures()
        r["result_metadata"]["independent_review"]["findings"].append({
            "kind": "required", "path": "backend/services/unsafe.py",
            "description": "Real code defect", "correction": "Fix it",
        })
        _, error, conn, _, event, _, _, _ = self.run_retry(card=c, review=r, impl=i)
        self.assertIsInstance(error, svc.AgentStateConflictError)
        conn.rollback.assert_called_once()
        event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
