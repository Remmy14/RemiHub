# Blue Jackets attendance backend API

All paths require a Firebase bearer token for an active RemiHub user, including
when the wider application uses transition auth mode. Responses use the usual
`{"success": true, "data": ...}` envelope. The caller's RemiHub user ID is
derived from the token; clients cannot supply a user ID.

## Games

`GET /blue-jackets/games?season=20212022` lists eligible CBJ games, ordered by
date. `season` is required and must be NHL's `YYYYyyyy` format from `20212022`
onward. Optional filters: `date_from` and `date_to` (ISO dates, inclusive),
`opponent` (three-letter abbreviation), and `attendance` (`none`, `going`, or
`attended`). A missing season is fetched as lightweight schedule metadata on
first use; later requests use the database. Preseason games are excluded.

Each game includes `nhl_game_id`, `season`, `game_type` (2 regular, 3 playoff),
`game_date`, nullable `start_time_utc`, `opponent_abbrev`, `cbj_home`,
`game_state`, nullable `attendance_status`, nullable `detail_state`, and
`stats_available`. Schedule rows contain no detailed statistics.

## Attendance

`PUT /blue-jackets/games/{nhl_game_id}/attendance` accepts
`{"status":"going"}` or `{"status":"attended"}`. Repeating a request updates
the same user/game record. On the first request for an unknown NHL game ID,
the service loads that season's lightweight schedule and checks eligibility.
Marking a completed historical game Attended initiates detail retrieval. If the
NHL API is unavailable, the attendance record remains saved and retrieval is
retried later. Going never initiates detail retrieval.

`DELETE /blue-jackets/games/{nhl_game_id}/attendance` returns HTTP 204. It
removes only the caller's record. When no Attended user remains, cached details
for that game are deleted.

## Game and aggregate statistics

`GET /blue-jackets/games/{nhl_game_id}/stats` is available only for the caller's
Attended games. Before official completion it returns `game_state: "pending"`
or the latest observed state and nullable `goals_for`, `goals_against`,
`shots_for`, `shots_against`, `result`, and `fetched_at`. The response also
includes `next_retry_at`, `attempt_count`, and nullable `last_error`.

`POST /blue-jackets/games/{nhl_game_id}/stats/refresh` forces one landing-page
request for an Attended game. The service stores final results only when the
landing response has state `OFF`, scores, and a classifiable W/L/OTL outcome.
Schedule state `FINAL` is not sufficient. Missing shot values remain null.

`GET /blue-jackets/stats` returns lifetime totals; `?season=20212022` restricts
to one season. The response has `games_attended` (completed games),
`pending_attended_games`, `wins`, `regulation_losses`, `ot_shootout_losses`,
`win_percentage` (0–100), `goals_for`, `goals_against`, `goal_differential`,
`shots_for`, `shots_against`, `averages`, `available_game_counts`,
`by_opponent`, `by_opponent_regular_season`, `by_opponent_playoffs`,
`regular_season`, and `playoffs`. `by_opponent` remains the combined map of
opponent abbreviations to the same W–L–OTL summary shape as the overall totals.
The two new maps use that same shape, restricted to game type 2 (regular season)
or 3 (playoffs). They include opponents with pending attended games and are
empty objects when there is no attendance in that competition. The optional
season filter applies to all three opponent maps and all totals. Totals and
averages for a metric are null if any completed attended game lacks that metric;
the count reports coverage. Going games never affect these totals.

## Errors

- 401: missing or invalid bearer token.
- 403: token belongs to an inactive or unauthorized user.
- 404: game is ineligible, absent from schedule, or not Attended by the caller.
- 400/422: invalid season, filter, ID, or attendance body.
- 503: NHL API unavailable for a first-time schedule import or manual refresh.

## Synchronization and operational validation

The worker imports season schedules from 2021–22 through the current season,
deduplicates entries by NHL game ID, and logs duplicate IDs. It checks only
Attended games at least three hours past scheduled start when they have no
finalized cache and are due for retry. Failed or incomplete retrievals receive
bounded exponential backoff. A completed cache prevents repeat detail calls.

The supplied external check on November 3, 2026 reported 90, 90, 90, 90,
89, and 88 total schedule entries for the six seasons through 2026–27;
regular-season entry counts were 82, 82, 82, 82, 82, and 84. These are
observations, not permanent cardinality requirements. The worker reports
distinct eligible game IDs and any duplicates at each import.

The approved sandbox has no NHL network access or database migration authority.
RemiHub's QA verification must capture real playoff, OT/shootout, and away
landing responses, inspect their period descriptors, test authenticated calls
and user isolation, run the migration pair against QA, and confirm background
completion before production promotion. The included unit tests use verified
field/value observations plus synthetic edge cases; they are not raw NHL
response fixtures. Production deployment and rollback are RemiHub-owned.
