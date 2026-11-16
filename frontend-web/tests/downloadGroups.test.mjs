import assert from "node:assert/strict";
import test from "node:test";

import { groupDownloads } from "../src/downloadGroups.ts";

function request(job, files, overrides = {}) {
  return {
    request_id: `request-${job}`, jd2_job_id: job, name: "Batch", category: "Movies",
    state: "downloading", submission_outcome: "succeeded", truncated: false,
    completed_at: null, files, ...overrides,
  };
}

function file(id, name, stem, loaded = 50, total = 100, overrides = {}) {
  return {
    id, name, logical_group_key: `package:900:archive:${stem}`,
    logical_group_name: stem, package_id: 900, package_name: "Mixed JD2 package",
    status: "Downloading", finished: false, enabled: null,
    bytes_downloaded: loaded, total_bytes: total, download_speed: 10,
    ...overrides,
  };
}

test("18 archive parts across five jobs form one card and preserve provenance", () => {
  const requests = Array.from({ length: 5 }, (_, job) => request(job + 1,
    Array.from({ length: job < 3 ? 4 : 3 }, (_, part) => {
      const n = job < 3 ? job * 4 + part + 1 : 12 + (job - 3) * 3 + part + 1;
      return file(n, `Movie.part${String(n).padStart(2, "0")}.rar`, "movie", 25, 100);
    })));
  const groups = groupDownloads(requests);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].entries.length, 18);
  assert.equal(groups[0].requests.length, 5);
  assert.equal(groups[0].percentage_complete, 25);
  assert.equal(groups[0].download_speed, 180);
});

test("five submitted archive URLs in distinct jobs and packages form one card", () => {
  const requests = Array.from({ length: 5 }, (_, index) => request(index + 1, [
    file(index + 1, `Film.part${String(index + 1).padStart(2, "0")}.rar`, "film", 20, 100, {
      package_id: 900 + index, job_id: index + 1,
      logical_group_key: `package:${900 + index}:archive:film`,
      save_to: "/mnt/plex-pool/Temp/Movies",
    }),
  ], { requested_at: `2026-11-15T01:0${index}:00Z` }));
  const groups = groupDownloads(requests);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].entries.length, 5);
  assert.equal(groups[0].requests.length, 5);
  assert.equal(groups[0].percentage_complete, 20);
});

test("cross-package archive grouping rejects duplicate parts and distant submissions", () => {
  const makeRequest = (job, part, packageId, at) => request(job, [
    file(job, `Film.part${String(part).padStart(2, "0")}.rar`, "film", 20, 100, {
      package_id: packageId, job_id: job,
      logical_group_key: `package:${packageId}:archive:film`,
      save_to: "/mnt/plex-pool/Temp/Movies",
    }),
  ], { requested_at: at });
  assert.equal(groupDownloads([
    makeRequest(1, 1, 901, "2026-11-15T01:00:00Z"),
    makeRequest(2, 1, 902, "2026-11-15T01:01:00Z"),
    makeRequest(3, 2, 903, "2026-11-15T01:02:00Z"),
  ]).length, 3);
  assert.equal(groupDownloads([
    makeRequest(1, 1, 901, "2026-11-15T01:00:00Z"),
    makeRequest(2, 2, 902, "2026-11-15T01:11:00Z"),
  ]).length, 2);
  const differentDestination = makeRequest(2, 2, 902, "2026-11-15T01:01:00Z");
  differentDestination.files[0].save_to = "/mnt/plex-pool/Other";
  assert.equal(groupDownloads([
    makeRequest(1, 1, 901, "2026-11-15T01:00:00Z"), differentDestination,
  ]).length, 2);
  const differentTitle = makeRequest(2, 2, 902, "2026-11-15T01:01:00Z");
  differentTitle.files[0].name = "Another.part02.rar";
  assert.equal(groupDownloads([
    makeRequest(1, 1, 901, "2026-11-15T01:00:00Z"), differentTitle,
  ]).length, 2);
});

test("distinct archives and episodes stay separate within a JD2 package", () => {
  const groups = groupDownloads([request(1, [
    file(1, "Movie A.part01.rar", "movie a"),
    file(2, "Movie B.part01.rar", "movie b"),
    file(3, "Show.S01E01.mkv", "show s01e01", 50, 100, { logical_group_key: "package:900:episode:show s01e01" }),
    file(4, "Show.S01E02.mkv", "show s01e02", 50, 100, { logical_group_key: "package:900:episode:show s01e02" }),
  ])]);
  assert.equal(groups.length, 4);
});

test("contradictory bytes, truncation, and paused files do not claim progress or ETA", () => {
  const [bad] = groupDownloads([request(1, [file(1, "A.part01.rar", "a", 10, 0),
    file(2, "A.part02.rar", "a", 20, 10)])]);
  assert.equal(bad.percentage_complete, null);
  assert.equal(bad.eta, null);
  const [paused] = groupDownloads([request(2, [file(3, "B.part01.rar", "b", 20, 100, { enabled: false, download_speed: 0 })])]);
  assert.equal(paused.status, "Paused / disabled");
  assert.equal(paused.eta, null);
  const [truncated] = groupDownloads([request(3, [file(4, "C.part01.rar", "c")], { truncated: true })]);
  assert.equal(truncated.bytes_downloaded, null);
  assert.equal(truncated.percentage_complete, null);
  assert.equal(truncated.section, "attention");
  const [unavailable] = groupDownloads([request(4, [file(5, "D.part01.rar", "d")],
    { state: "unavailable", jd2_unavailable: true })]);
  assert.equal(unavailable.bytes_downloaded, null);
  assert.equal(unavailable.total_bytes, null);
});

test("a missing link affects only its logical card when the JD2 query completed", () => {
  const groups = groupDownloads([request(1, [
    file(1, "Missing.part01.rar", "missing", null, null,
      { source: "persisted", status: "Live status unknown" }),
    file(2, "Healthy.mkv", "healthy", 50, 100),
  ], { state: "unavailable", inventory_incomplete: true, jd2_unavailable: false })]);
  const missing = groups.find((group) => group.name === "missing");
  const healthy = groups.find((group) => group.name === "healthy");
  assert.equal(missing?.status, "Status incomplete");
  assert.equal(missing?.percentage_complete, null);
  assert.equal(healthy?.section, "active");
  assert.equal(healthy?.percentage_complete, 50);
  assert.equal(healthy?.bytes_downloaded, 50);
});

test("paused JD2 status is honored when enabled is absent", () => {
  const [paused] = groupDownloads([request(1, [file(1, "Movie.part01.rar", "movie", 20, 100,
    { status: "Paused by user", enabled: null, download_speed: 0 })])]);
  assert.equal(paused.status, "Paused / disabled");
  assert.equal(paused.eta, null);
  const [unknown] = groupDownloads([request(2, [file(2, "Other.part01.rar", "other", 20, 100,
    { status: "Waiting for slot", enabled: null, download_speed: 0 })])]);
  assert.equal(unknown.status, "Queued");
});

test("request failure affects only logical groups with attributable failed files", () => {
  const groups = groupDownloads([request(1, [
    file(1, "Failed movie.mkv", "failed movie"),
    file(2, "Other movie.mkv", "other movie"),
  ], { state: "failed", failure: "Plex could not place one file", processing_files: [
    { id: 10, link_id: 1, state: "failed", error: "Placement failed" },
  ] })]);
  assert.equal(groups.find((group) => group.name === "failed movie")?.section, "attention");
  const other = groups.find((group) => group.name === "other movie");
  assert.equal(other?.section, "active");
  assert.equal(other?.requests[0].failure, "Plex could not place one file");
  const unassigned = groupDownloads([request(2, [file(3, "Third movie.mkv", "third movie")],
    { state: "failed", failure: "Request failed", processing_files: [
      { id: 11, link_id: null, state: "failed", error: "Unknown file" },
    ] })]);
  assert.equal(unassigned[0].section, "active");
});

test("mixed request completes only the package with attributable Plex results", () => {
  const finishedAt = "2026-11-15T01:00:00Z";
  const groups = groupDownloads([request(1, [
    file(1, "Finished movie.mkv", "finished movie", 100, 100,
      { finished: true, download_speed: 0 }),
    file(2, "Pending movie.mkv", "pending movie", 25, 100),
  ], { processing_files: [
    { id: 10, link_id: 1, state: "completed", completed_at: finishedAt },
  ] })]);
  const finished = groups.find((group) => group.name === "finished movie");
  const pending = groups.find((group) => group.name === "pending movie");
  assert.equal(finished?.section, "completed");
  assert.equal(finished?.completed_at, finishedAt);
  assert.equal(pending?.section, "active");
  assert.equal(pending?.completed_at, null);
});

test("mixed request completion event does not complete a package without its own result", () => {
  const groups = groupDownloads([request(1, [
    file(1, "First movie.mkv", "first movie", 100, 100, { finished: true }),
    file(2, "Second movie.mkv", "second movie", 100, 100, { finished: true }),
  ], { state: "completed", completed_at: "2026-11-15T01:00:00Z", processing_files: [] })]);
  assert.ok(groups.every((group) => group.section === "processing"));
  assert.ok(groups.every((group) => group.completed_at === null));
});

test("historical links without job UUID keep separate request associations", () => {
  const groups = groupDownloads([request(1, [file(1, "Movie.part01.rar", "movie", 100, 100, { job_id: null, finished: true })], { state: "completed", completed_at: "2026-11-15T00:00:00Z" }),
    request(2, [file(2, "Movie.part02.rar", "movie", 100, 100, { job_id: null, finished: true })], { state: "completed", completed_at: "2026-11-15T00:01:00Z" })]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].section, "completed");
  assert.equal(groups[0].requests.length, 2);
});

test("failures stay visible and unknown metadata does not imply completion", () => {
  const [failed] = groupDownloads([request(1, [file(1, "Bad.part01.rar", "bad", null, null,
    { status: "Download failed", download_speed: null })])]);
  assert.equal(failed.section, "attention");
  assert.equal(failed.percentage_complete, null);
  assert.equal(failed.eta, null);
  const [unknown] = groupDownloads([request(2, [file(2, "Unknown.part01.rar", "unknown", 20, null)])]);
  assert.equal(unknown.section, "active");
  assert.equal(unknown.percentage_complete, null);
});

test("a repeated JD2 link counts once but retains both RemiHub request associations", () => {
  const groups = groupDownloads([
    request(1, [file(7, "Movie.part01.rar", "movie")]),
    request(2, [file(7, "Movie.part01.rar", "movie")]),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].entries.length, 1);
  assert.equal(groups[0].entries[0].requests.length, 2);
  assert.equal(groups[0].percentage_complete, 50);
});

test("missing link IDs do not cause distinct same-name files to disappear", () => {
  const [group] = groupDownloads([request(1, [
    file(null, "Movie.part01.rar", "movie"), file(null, "Movie.part01.rar", "movie"),
  ])]);
  assert.equal(group.entries.length, 2);
});

test("legacy files remain historical even beside modern parts of one archive", () => {
  const groups = groupDownloads([
    request(1, [file(1, "Movie.part01.rar", "movie", 100, 100, { finished: true })],
      { legacy: true, state: "submitted", submission_outcome: null }),
    request(2, [file(2, "Movie.part02.rar", "movie")], { legacy: false }),
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups.find((group) => group.section === "legacy")?.status, "Historical outcome unknown");
  assert.equal(groups.find((group) => group.section === "active")?.entries.length, 1);
});

test("missing persisted parts suppress aggregate progress even with a completed live part", () => {
  const [group] = groupDownloads([request(1, [
    file(1, "Movie.part01.rar", "movie", 100, 100, { finished: true }),
    file(2, "Movie.part02.rar", "movie", null, null,
      { source: "persisted", status: "Last observed complete", finished: true }),
  ], { inventory_incomplete: true })]);
  assert.equal(group.entries.length, 2);
  assert.equal(group.bytes_downloaded, null);
  assert.equal(group.percentage_complete, null);
  assert.equal(group.section, "attention");
  assert.equal(group.status, "Status incomplete");
  assert.equal(group.download_speed, null);
});

test("verified Plex completion stays in history when live JD2 inventory is incomplete", () => {
  const [group] = groupDownloads([request(1, [
    file(1, "Movie.part01.rar", "movie", null, null,
      { source: "persisted", status: "Last observed complete", finished: true }),
  ], { state: "completed", completed_at: "2026-11-15T00:00:00Z", inventory_incomplete: true })]);
  assert.equal(group.section, "completed");
  assert.equal(group.percentage_complete, null);
});
