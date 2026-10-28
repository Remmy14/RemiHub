"""Small, localhost-only client for JDownloader's Direct API."""

import os
import math

import requests


API_ROOT = "http://127.0.0.1:3128"
MAX_TIMEOUT = 10.0
MAX_QUERY_RESULTS = 200


class JDownloaderError(Exception):
    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


def _timeout() -> float:
    try:
        value = float(os.getenv("REMIHUB_JD2_TIMEOUT_SECONDS", "3"))
    except ValueError:
        value = 3.0
    if not math.isfinite(value):
        value = 3.0
    return min(max(value, 0.25), MAX_TIMEOUT)


def _call(path: str, *params):
    # The endpoint and path are code constants; no caller-supplied host is accepted.
    try:
        with requests.Session() as session:
            session.trust_env = False  # Never route localhost through an environment proxy.
            response = session.post(
                f"{API_ROOT}/{path}", json={"params": list(params)},
                timeout=_timeout(), allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise JDownloaderError("JDownloader API redirected unexpectedly")
            response.raise_for_status()
            payload = response.json()
    except requests.Timeout as exc:
        raise JDownloaderError("JDownloader API timed out", ambiguous=True) from exc
    except requests.ConnectionError as exc:
        raise JDownloaderError("JDownloader API connection failed", ambiguous=True) from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        raise JDownloaderError(
            "JDownloader API returned an HTTP error", ambiguous=status is None or status >= 500
        ) from exc
    except (requests.RequestException, ValueError) as exc:
        raise JDownloaderError("JDownloader API response was invalid", ambiguous=True) from exc
    if not isinstance(payload, dict) or payload.get("error") is not None:
        raise JDownloaderError("JDownloader API returned an error")
    if "data" not in payload:
        raise JDownloaderError("JDownloader API response omitted data", ambiguous=True)
    return payload["data"]


def add_links(url: str, destination: str, category: str) -> int:
    data = _call("linkgrabberv2/addLinks", {
        "links": url,
        "destinationFolder": destination,
        "packageName": f"RemiHub Automated Download - {category}",
        "autostart": True,
        "assignJobID": True,
    })
    job_id = next((data[key] for key in ("id", "jobID", "jobId") if key in data), None) if isinstance(data, dict) else data
    if not isinstance(job_id, int) or isinstance(job_id, bool) or not 0 < job_id <= 2**63 - 1:
        raise JDownloaderError("JDownloader did not confirm a valid job ID", ambiguous=True)
    return job_id


def query_links(list_name: str, job_ids: list[int], start_at: int = 0, max_results: int = 200):
    if list_name not in {"linkgrabberv2", "downloadsV2"}:
        raise ValueError("Unsupported JDownloader list")
    if not job_ids or len(job_ids) > 50 or not 0 <= start_at or not 1 <= max_results <= MAX_QUERY_RESULTS:
        raise ValueError("JDownloader query exceeds bounds")
    data = _call(f"{list_name}/queryLinks", {
        "jobUUIDs": job_ids,
        "jobUUID": True,
        "startAt": start_at,
        "maxResults": max_results,
        "uuid": True,
        "name": True,
        "bytesLoaded": True,
        "bytesTotal": True,
        "speed": True,
        "eta": True,
        "status": True,
        "finished": True,
        "extractionStatus": True,
    })
    if not isinstance(data, list):
        raise JDownloaderError("JDownloader link query returned invalid data")
    return data


def query_packages(package_ids: list[int], start_at: int = 0, max_results: int = 200):
    if not package_ids or len(package_ids) > 50 or not 0 <= start_at or not 1 <= max_results <= MAX_QUERY_RESULTS:
        raise ValueError("JDownloader query exceeds bounds")
    data = _call("downloadsV2/queryPackages", {
        "packageUUIDs": package_ids, "startAt": start_at, "maxResults": max_results,
        "status": True, "bytesLoaded": True, "bytesTotal": True,
        "extractionStatus": True,
    })
    if not isinstance(data, list):
        raise JDownloaderError("JDownloader package query returned invalid data")
    return data


def get_current_state():
    return _call("downloadcontroller/getCurrentState")


def get_version():
    version = _call("jd/getVersion")
    if version is None or version == "":
        raise JDownloaderError("JDownloader API did not report a version")
    return version
