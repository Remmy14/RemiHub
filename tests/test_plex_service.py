import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.routers import plex
from backend.services import plex_service


class PlexCrawljobTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.watch_directory = Path(self.temporary_directory.name)
        self.category_paths = {
            "Movies": "/tmp/remihub-test-movies",
            "TV": "/tmp/remihub-test-tv",
        }
        self.category_patch = patch.object(
            plex_service,
            "CATEGORY_PATHS",
            self.category_paths,
        )
        self.crawljob_dir_patch = patch.object(
            plex_service,
            "CRAWLJOB_DIR",
            self.watch_directory,
        )
        self.category_patch.start()
        self.crawljob_dir_patch.start()

    def tearDown(self):
        self.crawljob_dir_patch.stop()
        self.category_patch.stop()
        self.temporary_directory.cleanup()

    def request(self, url: str) -> plex_service.DownloadRequest:
        return plex_service.DownloadRequest(
            url=url,
            name="Example",
            category="Movies",
        )

    def crawljob_path(self, job_id: str) -> Path:
        return self.watch_directory / f"remihub_{job_id}.crawljob"

    def test_one_url_creates_one_crawljob(self):
        logged_requests = []

        with patch.object(plex_service.uuid, "uuid4", side_effect=["job-one"]):
            with patch.object(
                plex_service,
                "log_download_request",
                side_effect=lambda req, job_id: logged_requests.append(
                    (job_id, req.url)
                ),
            ):
                result = plex_service.create_crawljob_files(
                    self.request("https://example.test/file")
                )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["job_ids"], ["job-one"])
        self.assertEqual(logged_requests, [("job-one", "https://example.test/file")])
        self.assertEqual(
            self.crawljob_path("job-one").read_text(encoding="utf-8"),
            "\n".join(
                [
                    "text=https://example.test/file",
                    "        enabled=true",
                    "        autoStart=true",
                    "        packageName=RemiHub Automated Download - Movies",
                    "        downloadFolder=/tmp/remihub-test-movies",
                ]
            ),
        )

    def test_multiple_urls_create_ordered_crawljobs(self):
        logged_requests = []

        with patch.object(
            plex_service.uuid,
            "uuid4",
            side_effect=["job-one", "job-two", "job-three"],
        ):
            with patch.object(
                plex_service,
                "log_download_request",
                side_effect=lambda req, job_id: logged_requests.append(
                    (job_id, req.url)
                ),
            ):
                result = plex_service.create_crawljob_files(
                    self.request(
                        "\n".join(
                            [
                                "https://example.test/one",
                                "https://example.test/two",
                                "https://example.test/three",
                            ]
                        )
                    )
                )

        self.assertEqual(result["job_ids"], ["job-one", "job-two", "job-three"])
        self.assertEqual(
            logged_requests,
            [
                ("job-one", "https://example.test/one"),
                ("job-two", "https://example.test/two"),
                ("job-three", "https://example.test/three"),
            ],
        )
        self.assertIn(
            "text=https://example.test/one",
            self.crawljob_path("job-one").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "text=https://example.test/two",
            self.crawljob_path("job-two").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "text=https://example.test/three",
            self.crawljob_path("job-three").read_text(encoding="utf-8"),
        )

    def test_surrounding_whitespace_is_trimmed(self):
        logged_requests = []

        with patch.object(plex_service.uuid, "uuid4", side_effect=["job-one"]):
            with patch.object(
                plex_service,
                "log_download_request",
                side_effect=lambda req, job_id: logged_requests.append(
                    (job_id, req.url)
                ),
            ):
                plex_service.create_crawljob_files(
                    self.request("  \t https://example.test/file  \t ")
                )

        self.assertEqual(logged_requests, [("job-one", "https://example.test/file")])
        self.assertIn(
            "text=https://example.test/file",
            self.crawljob_path("job-one").read_text(encoding="utf-8"),
        )

    def test_blank_lines_are_ignored(self):
        logged_requests = []

        with patch.object(plex_service.uuid, "uuid4", side_effect=["job-one", "job-two"]):
            with patch.object(
                plex_service,
                "log_download_request",
                side_effect=lambda req, job_id: logged_requests.append(
                    (job_id, req.url)
                ),
            ):
                result = plex_service.create_crawljob_files(
                    self.request(
                        "\n\nhttps://example.test/one\n   \nhttps://example.test/two\n"
                    )
                )

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            logged_requests,
            [
                ("job-one", "https://example.test/one"),
                ("job-two", "https://example.test/two"),
            ],
        )

    def test_crlf_input_is_accepted(self):
        logged_requests = []

        with patch.object(plex_service.uuid, "uuid4", side_effect=["job-one", "job-two"]):
            with patch.object(
                plex_service,
                "log_download_request",
                side_effect=lambda req, job_id: logged_requests.append(
                    (job_id, req.url)
                ),
            ):
                result = plex_service.create_crawljob_files(
                    self.request(
                        "https://example.test/one\r\nhttps://example.test/two\r\n"
                    )
                )

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            logged_requests,
            [
                ("job-one", "https://example.test/one"),
                ("job-two", "https://example.test/two"),
            ],
        )

    def test_empty_input_raises_clear_client_error(self):
        with self.assertRaises(HTTPException) as raised:
            plex.add_download(self.request(" \n\t \r\n "))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail,
            "At least one download URL is required",
        )
        self.assertEqual(list(self.watch_directory.iterdir()), [])

    def test_unique_filenames_are_used_for_each_url(self):
        with patch.object(plex_service.uuid, "uuid4", side_effect=["job-one", "job-two"]):
            with patch.object(plex_service, "log_download_request"):
                result = plex_service.create_crawljob_files(
                    self.request("https://example.test/one\nhttps://example.test/two")
                )

        self.assertEqual(result["job_ids"], ["job-one", "job-two"])
        crawljobs = sorted(path.name for path in self.watch_directory.iterdir())
        self.assertEqual(
            crawljobs,
            ["remihub_job-one.crawljob", "remihub_job-two.crawljob"],
        )

    def test_existing_crawljob_is_not_overwritten(self):
        existing = self.crawljob_path("collision")
        existing.write_text("existing content", encoding="utf-8")

        with patch.object(
            plex_service.uuid,
            "uuid4",
            side_effect=["collision", "fresh"],
        ):
            with patch.object(plex_service, "log_download_request") as log:
                result = plex_service.create_crawljob_files(
                    self.request("https://example.test/file")
                )

        self.assertEqual(existing.read_text(encoding="utf-8"), "existing content")
        self.assertEqual(result["job_ids"], ["fresh"])
        self.assertTrue(self.crawljob_path("fresh").is_file())
        log.assert_called_once()
        self.assertEqual(log.call_args.args[1], "fresh")
        self.assertEqual(list(self.watch_directory.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
