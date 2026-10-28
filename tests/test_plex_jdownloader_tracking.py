import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import requests
from fastapi import Depends, FastAPI, HTTPException

from backend.core.auth import get_current_principal, require_current_principal
from backend.routers import plex
from backend.services import jdownloader_api_service as jd2, plex_service


class JDownloaderClientTests(unittest.TestCase):
    def test_extraction_calls_use_local_contract(self):
        with patch.object(jd2, "_call", side_effect=[[], []]) as call:
            self.assertEqual(jd2.get_extraction_queue(), [])
            self.assertEqual(jd2.get_archive_info([11], [22]), [])
        self.assertEqual(call.call_args_list[0].args, ("extraction/getQueue",))
        self.assertEqual(call.call_args_list[1].args,
                         ("extraction/getArchiveInfo", [11], [22]))

    def test_submission_uses_local_json_api_and_returns_job_id(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"data": {"id": 42}}
        session = MagicMock()
        session.post.return_value = response
        session.__enter__.return_value = session
        with patch.object(requests, "Session", return_value=session):
            result = jd2.add_links("https://example.test/secret", "/mnt/plex-pool/Temp/TV", "TV")
        self.assertEqual(result, 42)
        self.assertFalse(session.trust_env)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:3128/linkgrabberv2/addLinks")
        self.assertTrue(kwargs["json"]["params"][0]["assignJobID"])
        self.assertTrue(kwargs["json"]["params"][0]["autostart"])
        self.assertEqual(kwargs["json"]["params"][0]["destinationFolder"], "/mnt/plex-pool/Temp/TV")
        self.assertFalse(kwargs["allow_redirects"])

    def test_redirect_is_rejected_without_following(self):
        response = Mock(status_code=302)
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value = response
        with patch.object(requests, "Session", return_value=session):
            with self.assertRaises(jd2.JDownloaderError):
                jd2.get_version()
        self.assertEqual(session.post.call_count, 1)

    def test_timeout_and_connection_failure_are_ambiguous(self):
        for failure in (requests.Timeout(), requests.ConnectionError()):
            session = MagicMock()
            session.post.side_effect = failure
            session.__enter__.return_value = session
            with patch.object(requests, "Session", return_value=session):
                with self.assertRaises(jd2.JDownloaderError) as caught:
                    jd2.add_links("https://example.test/secret", "/tmp", "TV")
            self.assertTrue(caught.exception.ambiguous)
            self.assertNotIn("secret", str(caught.exception))

    def test_api_error_and_invalid_id_are_not_success(self):
        for data in ({"error": "bad"}, {"data": {"id": "bad"}}):
            response = Mock()
            response.status_code = 200
            response.json.return_value = data
            session = MagicMock()
            session.post.return_value = response
            session.__enter__.return_value = session
            with patch.object(requests, "Session", return_value=session):
                with self.assertRaises(jd2.JDownloaderError):
                    jd2.add_links("https://example.test/secret", "/tmp", "TV")


class SubmissionTests(unittest.TestCase):
    def request(self, url):
        return plex_service.DownloadRequest(url=url, name="Show", category="TV")

    @patch.object(plex_service, "_execute_write")
    @patch.object(plex_service.jd2, "add_links", side_effect=[101, 202])
    def test_multiline_submission_persists_independent_ids(self, add_links, write):
        result = plex_service.submit_downloads(self.request("https://one.test\n\n https://two.test "))
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "Download job added.")
        self.assertEqual(len(result["requests"]), 2)
        self.assertNotEqual(result["requests"][0]["request_id"], result["requests"][1]["request_id"])
        self.assertEqual([item["jd2_job_id"] for item in result["requests"]], [101, 202])
        self.assertEqual(write.call_count, 4)
        self.assertEqual(add_links.call_args_list[1].args[0], "https://two.test")

    @patch.object(plex_service, "_execute_write")
    @patch.object(plex_service.jd2, "add_links", side_effect=[101, jd2.JDownloaderError("timeout", ambiguous=True)])
    def test_partial_failure_is_not_reported_as_success(self, _add, write):
        result = plex_service.submit_downloads(self.request("https://one.test\nhttps://two.test"))
        self.assertFalse(result["success"])
        self.assertEqual([item["submission_outcome"] for item in result["requests"]], ["succeeded", "unknown"])
        self.assertEqual(write.call_args_list[-1].args[1][1], "unknown")

    @patch.object(plex_service, "_execute_write")
    @patch.object(plex_service.jd2, "add_links", side_effect=jd2.JDownloaderError("API rejected request"))
    def test_definite_api_failure_is_recorded(self, add, write):
        result = plex_service.submit_downloads(self.request("https://one.test"))
        self.assertFalse(result["success"])
        self.assertEqual(result["requests"][0]["submission_outcome"], "failed")
        self.assertEqual(write.call_args_list[-1].args[1][1], "failed")
        add.assert_called_once()

    @patch.object(plex_service, "_execute_write", side_effect=RuntimeError("database unavailable"))
    @patch.object(plex_service.jd2, "add_links")
    def test_persistence_failure_prevents_submission(self, add, _write):
        result = plex_service.submit_downloads(self.request("https://one.test"))
        self.assertFalse(result["success"])
        add.assert_not_called()

    @patch.object(plex_service, "_execute_write")
    @patch.object(plex_service.jd2, "add_links", return_value=101)
    def test_android_response_fields_and_no_folderwatch(self, _add, _write):
        with patch.object(plex_service, "_write_crawljob_file") as legacy:
            response = plex.add_download(self.request("https://one.test"))
        payload = json.loads(response.body)
        self.assertTrue(payload["success"])
        self.assertIn("message", payload)
        legacy.assert_not_called()


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tracking_patch = patch.object(
            plex_service, "read_tracking",
            return_value={"links": [], "processed": [], "event": None},
        )
        self.sync_patch = patch.object(plex_service, "sync_link_inventory")
        self.packages_patch = patch.object(plex_service.jd2, "query_packages", return_value=[])
        self.lifecycle_patch = patch.object(plex_service, "record_request_lifecycle")
        self.tracking_patch.start()
        self.sync_patch.start()
        self.packages_patch.start()
        self.lifecycle_patch.start()

    def tearDown(self):
        self.lifecycle_patch.stop()
        self.packages_patch.stop()
        self.sync_patch.stop()
        self.tracking_patch.stop()

    def row(self, job_id=42, outcome="succeeded"):
        return {"id": uuid.uuid4(), "name": "Show", "category": "TV",
                "requested_at": datetime(2026, 10, 28), "jd2_job_id": job_id,
                "submission_outcome": outcome}

    @patch.object(plex_service, "_read_requests")
    @patch.object(plex_service.jd2, "query_links")
    def test_multiple_files_follow_job_when_package_changes(self, query, read):
        read.return_value = [self.row()]
        def fetch(source, ids, start):
            if start:
                return []
            if source == "linkgrabberv2":
                return []  # Auto-confirmed links have left LinkGrabber.
            return [
                {"uuid": 1, "jobUUID": 42, "packageUUID": 900, "name": "one.mkv",
                 "bytesLoaded": 50, "bytesTotal": 100, "speed": 10, "eta": 5, "finished": False},
                {"uuid": 2, "jobUUID": 42, "packageUUID": 901, "name": "two.mkv",
                 "bytesLoaded": 100, "bytesTotal": 100, "speed": 0, "finished": True},
            ]
        query.side_effect = fetch
        item = plex_service.get_downloads()["data"][0]
        self.assertEqual(item["state"], "downloading")
        self.assertEqual(item["discovered_files"], 2)
        self.assertEqual(item["finished_files"], 1)
        self.assertEqual(item["bytes_downloaded"], 150)
        self.assertEqual(item["total_bytes"], 200)
        self.assertEqual(item["percentage_complete"], 75.0)
        self.assertEqual(item["download_speed"], 10)
        self.assertEqual(item["eta"], 5)
        self.assertEqual({f["id"] for f in item["files"]}, {1, 2})

    @patch.object(plex_service, "_read_requests")
    @patch.object(plex_service.jd2, "query_links")
    def test_job_correlation_survives_linkgrabber_transition(self, query, read):
        read.return_value = [self.row()]
        query.side_effect = lambda source, *_: (
            [{"uuid": 7, "jobUUID": 42, "packageUUID": 11, "name": "show.mkv"}]
            if source == "linkgrabberv2" else []
        )
        self.assertEqual(plex_service.get_downloads()["data"][0]["state"], "discovering")
        query.side_effect = lambda source, *_: (
            [] if source == "linkgrabberv2" else
            [{"uuid": 8, "jobUUID": 42, "packageUUID": 99, "name": "show.mkv",
              "bytesLoaded": 100, "bytesTotal": 100, "finished": True}]
        )
        moved = plex_service.get_downloads()["data"][0]
        self.assertEqual(moved["state"], "download_complete")
        self.assertEqual(moved["jd2_job_id"], 42)
        self.assertEqual(moved["files"][0]["id"], 8)

    @patch.object(plex_service, "_read_requests")
    @patch.object(plex_service.jd2, "query_links")
    def test_unknown_size_and_eta(self, query, read):
        read.return_value = [self.row()]
        query.side_effect = lambda source, *_: [] if source == "linkgrabberv2" else [
            {"uuid": 1, "jobUUID": 42, "name": "unknown.mkv", "bytesLoaded": 10, "finished": False}
        ]
        item = plex_service.get_downloads()["data"][0]
        self.assertIsNone(item["total_bytes"])
        self.assertIsNone(item["percentage_complete"])
        self.assertIsNone(item["eta"])

    @patch.object(plex_service, "_read_requests")
    @patch.object(plex_service.jd2, "query_packages")
    @patch.object(plex_service.jd2, "query_links")
    def test_detail_uses_package_extraction_status(self, query, packages, read):
        row = self.row()
        read.return_value = [row]
        query.side_effect = lambda source, *_: [] if source == "linkgrabberv2" else [
            {"uuid": 1, "jobUUID": 42, "packageUUID": 900, "name": "one.mkv", "finished": True}
        ]
        packages.return_value = [{"uuid": 900, "extractionStatus": "running"}]
        item = plex_service.get_downloads(limit=1, request_id=str(row["id"]))["data"][0]
        self.assertEqual(item["extraction_status"], ["running"])
        packages.assert_called_once_with([900])

    @patch.object(plex_service, "_read_requests")
    @patch.object(plex_service.jd2, "query_links", side_effect=jd2.JDownloaderError("offline"))
    def test_outage_preserves_history(self, _query, read):
        read.return_value = [self.row()]
        item = plex_service.get_downloads()["data"][0]
        self.assertEqual(item["state"], "unavailable")
        self.assertEqual(item["name"], "Show")

    @patch.object(plex_service, "_read_requests")
    @patch.object(plex_service.jd2, "query_links")
    def test_legacy_row_is_readable_without_jd_query(self, query, read):
        read.return_value = [self.row(None, None)]
        item = plex_service.get_downloads()["data"][0]
        self.assertEqual(item["state"], "submitted")
        self.assertTrue(item["legacy"])
        query.assert_not_called()

    def test_durable_completion_survives_jd_outage(self):
        row = self.row()
        with patch.object(plex_service, "_read_requests", return_value=[row]):
            with patch.object(plex_service.jd2, "query_links",
                              side_effect=jd2.JDownloaderError("offline")):
                with patch.object(plex_service, "read_tracking", return_value={
                    "links": [{"kind": "media", "finished": True}],
                    "processed": [{"state": "completed", "link_id": 1,
                                   "destination_path": "/plex/one.mkv",
                                   "completed_at": datetime(2026, 10, 28), "error": None}],
                    "event": {"state": "completed"},
                }):
                    item = plex_service.get_downloads()["data"][0]
        self.assertEqual(item["state"], "completed")
        self.assertEqual(item["completed_file_count"], 1)

    def test_archive_remains_unresolved_without_output_manifest(self):
        row = self.row()
        item = plex_service._summary(row, [{"finished": True, "total_bytes": 10,
            "bytes_downloaded": 10, "download_speed": 0, "eta": 0,
            "extraction_status": None}], tracking={
                "links": [{"kind": "archive", "finished": True, "extraction_status": None}],
                "processed": [], "event": None})
        self.assertEqual(item["state"], "waiting_for_extraction")
        self.assertIsNone(item["file_count"])
        self.assertFalse(item["cleanup_eligible"])

    def test_lifecycle_summary_transitions(self):
        row = self.row()
        def summary(files=(), links=(), processed=(), event=None, unavailable=False):
            return plex_service._summary(row, list(files), unavailable=unavailable,
                tracking={"links": list(links), "processed": [
                    {"completed_at": None, "error": None, "destination_path": None,
                     "link_id": None, **item} for item in processed], "event": event})["state"]

        queued = {"source": "downloads", "finished": False, "download_speed": 0,
                  "total_bytes": 10, "bytes_downloaded": 0, "eta": 10,
                  "extraction_status": None}
        finished = {**queued, "finished": True, "bytes_downloaded": 10}
        self.assertEqual(summary(), "discovering")
        self.assertEqual(summary([queued]), "queued")
        self.assertEqual(summary([{**queued, "download_speed": 2}]), "downloading")
        self.assertEqual(summary([finished]), "download_complete")
        self.assertEqual(summary([finished], processed=[{"state": "queued_for_plex"}]),
                         "queued_for_plex")
        self.assertEqual(summary([finished], processed=[{"state": "processing"}]), "processing")
        self.assertEqual(summary([finished], processed=[{"state": "failed"}]), "failed")
        self.assertEqual(summary([finished], event={"state": "completed"}), "completed")
        self.assertEqual(summary(unavailable=True), "unavailable")
        self.assertEqual(summary([finished], links=[{"kind": "archive", "finished": True,
            "extraction_status": "running"}]), "extracting")

    def test_exact_package_path_must_stay_inside_category_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TV"
            root.mkdir()
            with patch.dict(plex_service.CATEGORY_PATHS, {"TV": str(root)}):
                row = {"category": "TV"}
                self.assertEqual(plex_service._source_path(row, {
                    "name": "episode.mkv", "save_to": str(root / "Package")}),
                    str(root / "Package" / "episode.mkv"))
                self.assertIsNone(plex_service._source_path(row, {
                    "name": "../episode.mkv", "save_to": str(root)}))
                self.assertIsNone(plex_service._source_path(row, {
                    "name": "episode.mkv", "save_to": str(Path(directory) / "Other")}))

    def test_multifile_completion_requires_every_link(self):
        from unittest.mock import MagicMock
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        cursor.fetchall.side_effect = [
            [("media", True, "/stage/one.mkv", 1),
             ("media", True, "/stage/two.mkv", 2)],
            [("/stage/one.mkv", 1, "completed")],
        ]
        with patch.object(plex_service, "_append_request_event") as event:
            plex_service._maybe_complete(cursor, "request")
        event.assert_not_called()
        cursor.fetchall.side_effect = [
            [("media", True, "/stage/one.mkv", 1),
             ("media", True, "/stage/two.mkv", 2)],
            [("/stage/one.mkv", 1, "completed"),
             ("/stage/two.mkv", 2, "completed")],
        ]
        with patch.object(plex_service, "_append_request_event") as event:
            plex_service._maybe_complete(cursor, "request")
        event.assert_called_once_with(cursor, "request", "completed")

    def test_cleanup_is_disabled_without_mutating_jd2(self):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        cursor = MagicMock()
        cursor.fetchone.return_value = (uuid.uuid4(),)

        @contextmanager
        def transaction():
            yield cursor

        with patch.object(plex_service, "_transaction", transaction):
            with patch.object(jd2, "_call") as call:
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    plex_service.cleanup_download(str(uuid.uuid4()))
        call.assert_not_called()

    def test_restart_recovery_requires_destination_and_original_size(self):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing.mkv"
            destination = Path(directory) / "placed.mkv"
            destination.write_bytes(b"media")
            cursor = MagicMock()
            cursor.fetchall.return_value = [(str(source), str(destination), 5)]

            @contextmanager
            def transaction():
                yield cursor

            with patch.object(plex_service, "_transaction", transaction):
                with patch.object(plex_service, "record_file_state") as record:
                    plex_service.recover_processing_files()
            record.assert_called_once_with(str(source), "completed",
                                           destination=str(destination))
            cursor.fetchall.return_value = [(str(source), str(destination), 6)]
            with patch.object(plex_service, "_transaction", transaction):
                with patch.object(plex_service, "record_file_state") as record:
                    plex_service.recover_processing_files()
            record.assert_called_once_with(str(source), "failed",
                error="Source missing and destination could not be verified")


class EndpointCompatibilityTests(unittest.TestCase):
    def test_download_endpoints_require_authentication(self):
        app = FastAPI()
        app.include_router(plex.router, dependencies=[Depends(get_current_principal)])
        paths = ("/plex/downloads", "/plex/downloads/{request_id}",
                 "/plex/downloads/{request_id}/cleanup")
        for route in app.routes:
            if getattr(route, "path", None) in paths:
                dependencies = [dep.call for dep in route.dependant.dependencies]
                self.assertIn(get_current_principal, dependencies)
                self.assertIn(require_current_principal, dependencies)
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}):
            with self.assertRaises(HTTPException) as raised:
                require_current_principal(None)
        self.assertEqual(raised.exception.status_code, 401)
        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "transition"}):
            with self.assertRaises(HTTPException) as raised:
                require_current_principal(None)
        self.assertEqual(raised.exception.status_code, 401)

    @patch.object(plex_service, "get_recent_download_requests")
    def test_recent_history_contract_is_unchanged(self, read):
        expected = {"success": True, "data": [{"id": str(uuid.uuid4()),
                    "url": "https://example.test", "name": "Show", "category": "TV",
                    "requested_at": "2026-10-28T00:00:00"}]}
        read.return_value = expected
        self.assertEqual(plex.getRecentRequests(), expected)


class JDownloaderHealthTests(unittest.TestCase):
    @patch("backend.services.service_health_service.jd2.get_version", return_value="2.0")
    def test_api_reachable(self, _get_version):
        from backend.services import service_health_service as health
        self.assertEqual(health.inspect_jdownloader_api().status.value, "healthy")

    @patch("backend.services.service_health_service.jd2.get_version",
           side_effect=jd2.JDownloaderError("offline"))
    def test_api_unavailable(self, _get_version):
        from backend.services import service_health_service as health
        self.assertEqual(health.inspect_jdownloader_api().status.value, "degraded")

    def test_current_paths_and_enablement_target(self):
        from backend.services import service_health_service as health
        self.assertTrue(all("/srv/remihub/Temp" not in item.path for item in health.JDOWNLOADER_PATHS))
        self.assertIn("/mnt/plex-pool/Temp/JDownloaderWatch", [item.path for item in health.JDOWNLOADER_PATHS])
        with patch.object(health.pwd, "getpwnam", return_value=Mock(pw_uid=1000, pw_dir="/home/alex")):
            runtime = health._jdownloader_user_runtime()
        self.assertIn("default.target.wants", str(runtime.enabled_link))


if __name__ == "__main__":
    unittest.main()
