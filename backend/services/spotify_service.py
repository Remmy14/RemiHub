from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

import spotipy
from psycopg2.extensions import connection as PgConnection
from psycopg2.extras import RealDictCursor
from spotipy.oauth2 import SpotifyOAuth


SPOTIFY_SCOPE = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-private "
    "playlist-modify-public "
    "user-read-private "
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-modify-playback-state"
)

VERSION_WORDS_RE = re.compile(
    r"\b("
    r"remaster(?:ed)?|\d{4}\s*remaster(?:ed)?|deluxe|anniversary|expanded|edition|"
    r"stereo|mono|single version|radio edit|edit|version|live|acoustic|demo|alternate|"
    r"explicit|clean|feat\.?|featuring"
    r")\b",
    re.IGNORECASE,
)
PARENS_RE = re.compile(r"[\(\[].*?[\)\]]")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
SPOTIFY_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


def require_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_good_song_options_playlist_id() -> str:
    return require_env_var("SPOTIFY_GOOD_OPTIONS_PLAYLIST_ID")


def get_good_songs_playlist_id() -> str:
    return require_env_var("SPOTIFY_GOOD_SONGS_PLAYLIST_ID")


def make_spotify_client(open_browser: bool = False) -> spotipy.Spotify:
    client_id = require_env_var("SPOTIFY_CLIENT_ID")
    client_secret = require_env_var("SPOTIFY_CLIENT_SECRET")
    redirect_uri = require_env_var("SPOTIFY_REDIRECT_URI")
    cache_path = os.environ.get("SPOTIFY_TOKEN_CACHE_PATH", ".spotify_token_cache")

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SPOTIFY_SCOPE,
        open_browser=open_browser,
        cache_path=cache_path,
    )

    return spotipy.Spotify(auth_manager=auth_manager)


def spotify_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except spotipy.SpotifyException as exc:
        if exc.http_status == 429:
            retry_after = int(exc.headers.get("Retry-After", "5"))
            time.sleep(retry_after)
            return fn(*args, **kwargs)
        raise


def normalize_for_match(value: str) -> str:
    value = value.lower().strip()
    value = value.replace("&", " and ")
    value = PARENS_RE.sub(" ", value)
    value = VERSION_WORDS_RE.sub(" ", value)
    value = NON_WORD_RE.sub(" ", value)
    return " ".join(value.split())


def similarity(a: str, b: str) -> float:
    a_norm = normalize_for_match(a)
    b_norm = normalize_for_match(b)

    if not a_norm or not b_norm:
        return 0.0

    if a_norm == b_norm:
        return 1.0

    if a_norm in b_norm or b_norm in a_norm:
        return 0.92

    return SequenceMatcher(None, a_norm, b_norm).ratio()


def split_artists(artists_value: str) -> list[str]:
    artists_value = artists_value.strip()
    if not artists_value:
        return []

    parts = [part.strip() for part in artists_value.split(";")]
    return [part for part in parts if part]


def safe_join(values: list[str], separator: str = "; ") -> str:
    return separator.join(v for v in values if v)


def title_artist_key(track_name: str, artists_value: str) -> str:
    artists = split_artists(artists_value)
    primary_artist = artists[0] if artists else artists_value
    return f"{normalize_for_match(track_name)}||{normalize_for_match(primary_artist)}"


def spotify_uri_to_track_id(uri: str) -> str:
    if uri.startswith("spotify:track:"):
        return uri.split(":")[-1]
    return ""


def spotify_url_to_track_id(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]

    if len(parts) >= 2 and parts[0] == "track" and SPOTIFY_TRACK_ID_RE.match(parts[1]):
        return parts[1]

    return ""


def get_playlist_item_object(playlist_item: dict[str, Any]) -> dict[str, Any] | None:
    return playlist_item.get("item") or playlist_item.get("track")


def extract_track_payload(track: dict[str, Any]) -> dict[str, Any]:
    artists = track.get("artists") or []
    album = track.get("album") or {}
    images = album.get("images") or []

    artist_names = [artist.get("name", "") for artist in artists]
    artists_text = safe_join(artist_names)

    spotify_track_id = track.get("id") or ""
    spotify_uri = track.get("uri") or f"spotify:track:{spotify_track_id}"
    track_name = track.get("name") or ""

    release_date = album.get("release_date") or ""
    release_year = None

    if release_date:
        try:
            release_year = int(release_date[:4])
        except ValueError:
            release_year = None

    return {
        "spotifyTrackId": spotify_track_id,
        "spotifyUri": spotify_uri,
        "trackName": track_name,
        "artists": artists_text,
        "albumName": album.get("name") or "",
        "albumId": album.get("id") or "",
        "releaseDate": release_date,
        "releaseYear": release_year,
        "durationMs": track.get("duration_ms"),
        "explicit": track.get("explicit"),
        "popularity": track.get("popularity"),
        "previewUrl": track.get("preview_url") or "",
        "artworkUrl": images[0].get("url", "") if images else "",
        "spotifyUrl": (track.get("external_urls") or {}).get("spotify", ""),
        "normalizedTitleArtistKey": title_artist_key(track_name, artists_text),
    }


def extract_device_payload(device: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": device.get("id"),
        "name": device.get("name") or "",
        "type": device.get("type") or "",
        "isActive": bool(device.get("is_active")),
        "isRestricted": bool(device.get("is_restricted")),
        "volumePercent": device.get("volume_percent"),
    }


def track_row_to_api(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "spotifyTrackId": row.get("spotify_track_id") or "",
        "spotifyUri": row.get("spotify_uri") or "",
        "trackName": row.get("track_name") or "",
        "artists": row.get("artists") or "",
        "albumName": row.get("album_name") or "",
        "albumId": row.get("album_id") or "",
        "releaseDate": row.get("release_date") or "",
        "releaseYear": row.get("release_year"),
        "durationMs": row.get("duration_ms"),
        "explicit": row.get("explicit"),
        "popularity": row.get("popularity"),
        "previewUrl": row.get("preview_url") or "",
        "artworkUrl": row.get("artwork_url") or "",
        "spotifyUrl": row.get("spotify_url") or "",
        "normalizedTitleArtistKey": row.get("normalized_title_artist_key") or "",
    }


def upsert_track(conn: PgConnection, payload: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO spotify_tracks (
                spotify_track_id,
                spotify_uri,
                track_name,
                artists,
                album_name,
                album_id,
                duration_ms,
                explicit,
                popularity,
                preview_url,
                artwork_url,
                spotify_url,
                normalized_title_artist_key,
                release_date,
                release_year,
                updated_at
            )
            VALUES (
                %(spotifyTrackId)s,
                %(spotifyUri)s,
                %(trackName)s,
                %(artists)s,
                %(albumName)s,
                %(albumId)s,
                %(durationMs)s,
                %(explicit)s,
                %(popularity)s,
                %(previewUrl)s,
                %(artworkUrl)s,
                %(spotifyUrl)s,
                %(normalizedTitleArtistKey)s,
                %(releaseDate)s,
                %(releaseYear)s,
                now()
            )
            ON CONFLICT (spotify_track_id)
            DO UPDATE SET
                spotify_uri = EXCLUDED.spotify_uri,
                track_name = EXCLUDED.track_name,
                artists = EXCLUDED.artists,
                album_name = EXCLUDED.album_name,
                album_id = EXCLUDED.album_id,
                duration_ms = EXCLUDED.duration_ms,
                explicit = EXCLUDED.explicit,
                popularity = EXCLUDED.popularity,
                preview_url = EXCLUDED.preview_url,
                artwork_url = EXCLUDED.artwork_url,
                spotify_url = EXCLUDED.spotify_url,
                normalized_title_artist_key = EXCLUDED.normalized_title_artist_key,
                release_date = EXCLUDED.release_date,
                release_year = EXCLUDED.release_year,
                updated_at = now()
            RETURNING *
            """,
            payload,
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("Failed to upsert Spotify track")

    return dict(row)


def upsert_playlist(
    conn: PgConnection,
    spotify_playlist_id: str,
    name: str,
    role: str,
    snapshot_id: str | None,
) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO spotify_playlists (
                spotify_playlist_id,
                name,
                role,
                snapshot_id,
                last_synced_at,
                updated_at
            )
            VALUES (%s, %s, %s, %s, now(), now())
            ON CONFLICT (spotify_playlist_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                role = EXCLUDED.role,
                snapshot_id = EXCLUDED.snapshot_id,
                last_synced_at = now(),
                updated_at = now()
            RETURNING *
            """,
            (spotify_playlist_id, name, role, snapshot_id),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("Failed to upsert Spotify playlist")

    return dict(row)


def get_db_playlist_by_role(conn: PgConnection, role: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM spotify_playlists
            WHERE role = %s
            """,
            (role,),
        )
        row = cur.fetchone()

    return dict(row) if row else None


def get_db_track_by_spotify_id(conn: PgConnection, spotify_track_id: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM spotify_tracks
            WHERE spotify_track_id = %s
            """,
            (spotify_track_id,),
        )
        row = cur.fetchone()

    return dict(row) if row else None


def get_db_track_by_uri(conn: PgConnection, spotify_uri: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM spotify_tracks
            WHERE spotify_uri = %s
            """,
            (spotify_uri,),
        )
        row = cur.fetchone()

    return dict(row) if row else None


def spotify_current_playback(sp: spotipy.Spotify, market: str = "US") -> dict[str, Any] | None:
    try:
        return spotify_call(sp.current_playback, market=market, additional_types="track")
    except TypeError:
        return spotify_call(sp.current_playback, market=market)


def player_state_to_api(conn: PgConnection, state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {
            "success": True,
            "isPlaying": False,
            "progressMs": None,
            "device": None,
            "track": None,
            "contextUri": "",
            "currentlyPlayingType": "",
            "hasActiveDevice": False,
            "message": "No active Spotify playback was returned.",
            "errors": [],
        }

    device = state.get("device") or {}
    item = state.get("item") or None
    currently_playing_type = state.get("currently_playing_type") or (item or {}).get("type") or ""
    context = state.get("context") or {}

    track_api = None
    message = ""

    if item and item.get("type") == "track":
        payload = extract_track_payload(item)
        if payload["spotifyTrackId"]:
            db_track = upsert_track(conn, payload)
            track_api = track_row_to_api(db_track)
    elif item:
        message = f"Spotify is currently playing a {currently_playing_type}, not a track."

    return {
        "success": True,
        "isPlaying": bool(state.get("is_playing")),
        "progressMs": state.get("progress_ms"),
        "device": extract_device_payload(device) if device else None,
        "track": track_api,
        "contextUri": context.get("uri") or "",
        "currentlyPlayingType": currently_playing_type,
        "hasActiveDevice": bool(device.get("id")),
        "message": message,
        "errors": [],
    }


def get_player_state_with_client(
    conn: PgConnection,
    sp: spotipy.Spotify,
    market: str = "US",
) -> dict[str, Any]:
    state = spotify_current_playback(sp, market=market)
    response = player_state_to_api(conn, state)
    conn.commit()
    return response


def get_player_state(conn: PgConnection, market: str = "US") -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)
    return get_player_state_with_client(conn=conn, sp=sp, market=market)


def get_devices_with_client(sp: spotipy.Spotify) -> dict[str, Any]:
    data = spotify_call(sp.devices) or {}
    devices = [extract_device_payload(device) for device in data.get("devices") or []]
    active_device_id = next((device["id"] for device in devices if device["isActive"]), None)

    return {
        "success": True,
        "devices": devices,
        "count": len(devices),
        "activeDeviceId": active_device_id,
        "errors": [],
    }


def get_devices() -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)
    return get_devices_with_client(sp)


def choose_playback_device_id(
    sp: spotipy.Spotify,
    requested_device_id: str | None = None,
    market: str = "US",
) -> str | None:
    requested_device_id = (requested_device_id or "").strip()
    if requested_device_id:
        return requested_device_id

    state = spotify_current_playback(sp, market=market)
    active_device_id = ((state or {}).get("device") or {}).get("id")
    if active_device_id:
        return active_device_id

    devices = get_devices_with_client(sp).get("devices") or []
    for device in devices:
        if device.get("id") and not device.get("isRestricted"):
            return device["id"]

    return None


def playback_command_response(
    conn: PgConnection,
    sp: spotipy.Spotify,
    action: str,
    success: bool,
    message: str = "",
    errors: list[str] | None = None,
    market: str = "US",
    refresh_player: bool = True,
) -> dict[str, Any]:
    response_errors = errors or []
    player = None

    if refresh_player:
        try:
            time.sleep(0.25)
            player = get_player_state_with_client(conn=conn, sp=sp, market=market)
        except Exception as exc:
            conn.rollback()
            response_errors.append(f"player refresh failed: {exc}")

    return {
        "success": success,
        "action": action,
        "player": player,
        "message": message,
        "errors": response_errors,
    }


def play_good_song_options_playlist(
    conn: PgConnection,
    device_id: str | None = None,
    position: int | None = None,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)
    selected_device_id = choose_playback_device_id(sp, device_id, market=market)

    if not selected_device_id:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="play_good_song_options",
            success=False,
            message="No available Spotify Connect device found. Start Spotify on a phone, desktop, or speaker, then try again.",
            refresh_player=False,
        )

    offset = {"position": position} if position is not None else None

    try:
        spotify_call(
            sp.start_playback,
            device_id=selected_device_id,
            context_uri=f"spotify:playlist:{get_good_song_options_playlist_id()}",
            offset=offset,
        )
    except Exception as exc:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="play_good_song_options",
            success=False,
            message="Spotify rejected the play request.",
            errors=[str(exc)],
        )

    return playback_command_response(
        conn=conn,
        sp=sp,
        action="play_good_song_options",
        success=True,
        message="Started the Good Song Options playlist.",
        market=market,
    )


def play_track(
    conn: PgConnection,
    spotify_track_id: str | None = None,
    spotify_uri: str | None = None,
    device_id: str | None = None,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)
    selected_device_id = choose_playback_device_id(sp, device_id, market=market)

    if not selected_device_id:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="play_track",
            success=False,
            message="No available Spotify Connect device found. Start Spotify on a phone, desktop, or speaker, then try again.",
            refresh_player=False,
        )

    try:
        db_track = find_or_fetch_track(
            conn=conn,
            sp=sp,
            spotify_track_id=spotify_track_id,
            spotify_uri=spotify_uri,
            market=market,
        )
        spotify_call(
            sp.start_playback,
            device_id=selected_device_id,
            uris=[db_track["spotify_uri"]],
        )
    except Exception as exc:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="play_track",
            success=False,
            message="Spotify rejected the play-track request.",
            errors=[str(exc)],
        )

    return playback_command_response(
        conn=conn,
        sp=sp,
        action="play_track",
        success=True,
        message="Started the requested track.",
        market=market,
    )


def resume_playback(
    conn: PgConnection,
    device_id: str | None = None,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)
    selected_device_id = choose_playback_device_id(sp, device_id, market=market)

    if not selected_device_id:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="resume",
            success=False,
            message="No available Spotify Connect device found. Start Spotify on a phone, desktop, or speaker, then try again.",
            refresh_player=False,
        )

    try:
        spotify_call(sp.start_playback, device_id=selected_device_id)
    except Exception as exc:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="resume",
            success=False,
            message="Spotify rejected the resume request.",
            errors=[str(exc)],
        )

    return playback_command_response(
        conn=conn,
        sp=sp,
        action="resume",
        success=True,
        message="Resumed Spotify playback.",
        market=market,
    )


def pause_playback(
    conn: PgConnection,
    device_id: str | None = None,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)

    try:
        spotify_call(sp.pause_playback, device_id=(device_id or None))
    except Exception as exc:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="pause",
            success=False,
            message="Spotify rejected the pause request.",
            errors=[str(exc)],
        )

    return playback_command_response(
        conn=conn,
        sp=sp,
        action="pause",
        success=True,
        message="Paused Spotify playback.",
        market=market,
    )


def next_track(
    conn: PgConnection,
    device_id: str | None = None,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)

    try:
        spotify_call(sp.next_track, device_id=(device_id or None))
    except Exception as exc:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="next",
            success=False,
            message="Spotify rejected the next-track request.",
            errors=[str(exc)],
        )

    return playback_command_response(
        conn=conn,
        sp=sp,
        action="next",
        success=True,
        message="Skipped to the next Spotify track.",
        market=market,
    )


def previous_track(
    conn: PgConnection,
    device_id: str | None = None,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)

    try:
        spotify_call(sp.previous_track, device_id=(device_id or None))
    except Exception as exc:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="previous",
            success=False,
            message="Spotify rejected the previous-track request.",
            errors=[str(exc)],
        )

    return playback_command_response(
        conn=conn,
        sp=sp,
        action="previous",
        success=True,
        message="Skipped to the previous Spotify track.",
        market=market,
    )


def transfer_playback(
    conn: PgConnection,
    device_id: str,
    force_play: bool = True,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)

    try:
        spotify_call(sp.transfer_playback, device_id=device_id, force_play=force_play)
    except Exception as exc:
        return playback_command_response(
            conn=conn,
            sp=sp,
            action="transfer",
            success=False,
            message="Spotify rejected the transfer-playback request.",
            errors=[str(exc)],
        )

    return playback_command_response(
        conn=conn,
        sp=sp,
        action="transfer",
        success=True,
        message="Transferred Spotify playback.",
        market=market,
    )


def get_current_track_for_review(
    conn: PgConnection,
    sp: spotipy.Spotify,
    market: str = "US",
) -> dict[str, Any]:
    state = spotify_current_playback(sp, market=market)
    if not state:
        raise RuntimeError("Spotify did not return an active currently-playing track.")

    item = state.get("item") or {}
    if item.get("type") != "track":
        item_type = state.get("currently_playing_type") or item.get("type") or "unknown item"
        raise RuntimeError(f"Spotify is currently playing a {item_type}, not a track.")

    payload = extract_track_payload(item)
    if not payload["spotifyTrackId"]:
        raise RuntimeError("Spotify returned a track without a Spotify track ID.")

    return upsert_track(conn, payload)


def thumbs_up_current_track(
    conn: PgConnection,
    note: str = "",
    skip_after_review: bool = True,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)
    db_track = get_current_track_for_review(conn=conn, sp=sp, market=market)

    return thumbs_up_track(
        conn=conn,
        spotify_track_id=db_track["spotify_track_id"],
        spotify_uri=db_track["spotify_uri"],
        note=note,
        market=market,
        sp=sp,
        skip_after_review=skip_after_review,
    )


def thumbs_down_current_track(
    conn: PgConnection,
    note: str = "",
    skip_after_review: bool = True,
    market: str = "US",
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)
    db_track = get_current_track_for_review(conn=conn, sp=sp, market=market)

    return thumbs_down_track(
        conn=conn,
        spotify_track_id=db_track["spotify_track_id"],
        spotify_uri=db_track["spotify_uri"],
        note=note,
        market=market,
        sp=sp,
        skip_after_review=skip_after_review,
    )


def sync_playlist(
    conn: PgConnection,
    sp: spotipy.Spotify,
    role: str,
    spotify_playlist_id: str,
    market: str = "US",
) -> dict[str, Any]:
    playlist_meta = spotify_call(
        sp.playlist,
        playlist_id=spotify_playlist_id,
        fields="id,name,snapshot_id",
    )

    db_playlist = upsert_playlist(
        conn=conn,
        spotify_playlist_id=spotify_playlist_id,
        name=playlist_meta.get("name") or role,
        role=role,
        snapshot_id=playlist_meta.get("snapshot_id"),
    )

    sync_marker = datetime.now(timezone.utc)
    total_fetched = 0
    total_stored = 0
    offset = 0
    limit = 50

    while True:
        data = spotify_call(
            sp.playlist_items,
            playlist_id=spotify_playlist_id,
            limit=limit,
            offset=offset,
            market=market,
            additional_types="track",
        )

        items = data.get("items") or []
        if not items:
            break

        for position, playlist_item in enumerate(items, start=offset + 1):
            total_fetched += 1

            item_obj = get_playlist_item_object(playlist_item)
            if not item_obj or item_obj.get("type") != "track":
                continue

            payload = extract_track_payload(item_obj)
            if not payload["spotifyTrackId"]:
                continue

            db_track = upsert_track(conn, payload)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO spotify_playlist_tracks (
                        playlist_id,
                        track_id,
                        position,
                        added_at,
                        added_by,
                        last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (playlist_id, track_id)
                    DO UPDATE SET
                        position = EXCLUDED.position,
                        added_at = EXCLUDED.added_at,
                        added_by = EXCLUDED.added_by,
                        last_seen_at = EXCLUDED.last_seen_at
                    """,
                    (
                        db_playlist["id"],
                        db_track["id"],
                        position,
                        playlist_item.get("added_at"),
                        (playlist_item.get("added_by") or {}).get("id", ""),
                        sync_marker,
                    ),
                )

            total_stored += 1

        if not data.get("next"):
            break

        offset += limit

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM spotify_playlist_tracks
            WHERE playlist_id = %s
              AND last_seen_at < %s
            """,
            (db_playlist["id"], sync_marker),
        )

    conn.commit()

    return {
        "role": role,
        "spotifyPlaylistId": spotify_playlist_id,
        "name": playlist_meta.get("name") or role,
        "totalFetched": total_fetched,
        "totalStored": total_stored,
        "snapshotId": playlist_meta.get("snapshot_id"),
    }


def sync_spotify_playlists(conn: PgConnection, market: str = "US") -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)

    playlists = []
    errors = []

    for role, playlist_id in [
        ("good_song_options", get_good_song_options_playlist_id()),
        ("good_songs", get_good_songs_playlist_id()),
    ]:
        try:
            playlists.append(
                sync_playlist(
                    conn=conn,
                    sp=sp,
                    role=role,
                    spotify_playlist_id=playlist_id,
                    market=market,
                )
            )
        except Exception as exc:
            conn.rollback()
            errors.append(f"{role}: {exc}")

    return {
        "success": not errors,
        "playlists": playlists,
        "errors": errors,
    }


def get_review_queue(conn: PgConnection, limit: int = 100) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                p.role AS playlist_role,
                pt.position,
                pt.added_at,
                t.*
            FROM spotify_playlist_tracks pt
            JOIN spotify_playlists p ON p.id = pt.playlist_id
            JOIN spotify_tracks t ON t.id = pt.track_id
            WHERE p.role = 'good_song_options'
            ORDER BY pt.position NULLS LAST, t.track_name
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    tracks = [
        {
            "playlistRole": row["playlist_role"],
            "position": row["position"],
            "addedAt": row["added_at"],
            "track": track_row_to_api(dict(row)),
        }
        for row in rows
    ]

    return {
        "success": True,
        "tracks": tracks,
        "count": len(tracks),
    }


def get_summary(conn: PgConnection) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM spotify_playlist_tracks pt
            JOIN spotify_playlists p ON p.id = pt.playlist_id
            WHERE p.role = 'good_song_options'
            """
        )
        good_song_options_count = cur.fetchone()["count"]

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM spotify_playlist_tracks pt
            JOIN spotify_playlists p ON p.id = pt.playlist_id
            WHERE p.role = 'good_songs'
            """
        )
        good_songs_count = cur.fetchone()["count"]

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM spotify_track_reviews
            WHERE decision = 'thumbs_up'
            """
        )
        thumbs_up_count = cur.fetchone()["count"]

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM spotify_track_reviews
            WHERE decision = 'thumbs_down'
            """
        )
        thumbs_down_count = cur.fetchone()["count"]

    return {
        "success": True,
        "goodSongOptionsCount": good_song_options_count,
        "goodSongsCount": good_songs_count,
        "thumbsUpCount": thumbs_up_count,
        "thumbsDownCount": thumbs_down_count,
    }


def fetch_existing_playlist_track_ids(
    sp: spotipy.Spotify,
    playlist_id: str,
    market: str = "US",
) -> set[str]:
    existing_track_ids: set[str] = set()
    offset = 0
    limit = 50

    while True:
        data = spotify_call(
            sp.playlist_items,
            playlist_id=playlist_id,
            limit=limit,
            offset=offset,
            market=market,
            additional_types="track",
        )

        items = data.get("items") or []
        if not items:
            break

        for playlist_item in items:
            item_obj = get_playlist_item_object(playlist_item)
            if not item_obj or item_obj.get("type") != "track":
                continue

            track_id = item_obj.get("id") or ""
            if track_id:
                existing_track_ids.add(track_id)

        if not data.get("next"):
            break

        offset += limit

    return existing_track_ids


def find_or_fetch_track(
    conn: PgConnection,
    sp: spotipy.Spotify,
    spotify_track_id: str | None = None,
    spotify_uri: str | None = None,
    market: str = "US",
) -> dict[str, Any]:
    spotify_track_id = (spotify_track_id or "").strip()
    spotify_uri = (spotify_uri or "").strip()

    if not spotify_track_id and spotify_uri:
        spotify_track_id = spotify_uri_to_track_id(spotify_uri)

    if not spotify_track_id and spotify_uri:
        existing = get_db_track_by_uri(conn, spotify_uri)
        if existing:
            return existing

    if spotify_track_id:
        existing = get_db_track_by_spotify_id(conn, spotify_track_id)
        if existing:
            return existing

    if not spotify_track_id:
        raise RuntimeError("A spotifyTrackId or spotifyUri is required")

    track = spotify_call(sp.track, spotify_track_id, market=market)
    payload = extract_track_payload(track)
    db_track = upsert_track(conn, payload)
    conn.commit()
    return db_track


def playlist_role_contains_track(
    conn: PgConnection,
    role: str,
    track_id: int,
) -> bool:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT 1
            FROM spotify_playlist_tracks pt
            JOIN spotify_playlists p ON p.id = pt.playlist_id
            WHERE p.role = %s
              AND pt.track_id = %s
            LIMIT 1
            """,
            (role, track_id),
        )
        row = cur.fetchone()

    return bool(row)


def insert_review(
    conn: PgConnection,
    track_id: int,
    spotify_track_id: str,
    spotify_uri: str,
    decision: str,
    source_playlist_role: str = "good_song_options",
    note: str = "",
) -> None:
    source_playlist = get_db_playlist_by_role(conn, source_playlist_role)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spotify_track_reviews (
                track_id,
                spotify_track_id,
                spotify_uri,
                decision,
                source_playlist_id,
                note
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                track_id,
                spotify_track_id,
                spotify_uri,
                decision,
                source_playlist["id"] if source_playlist else None,
                note or "",
            ),
        )


def thumbs_up_track(
    conn: PgConnection,
    spotify_track_id: str | None = None,
    spotify_uri: str | None = None,
    note: str = "",
    market: str = "US",
    sp: spotipy.Spotify | None = None,
    skip_after_review: bool = False,
    device_id: str | None = None,
) -> dict[str, Any]:
    sp = sp or make_spotify_client(open_browser=False)

    good_song_options_playlist_id = get_good_song_options_playlist_id()
    good_songs_playlist_id = get_good_songs_playlist_id()

    db_track = find_or_fetch_track(
        conn=conn,
        sp=sp,
        spotify_track_id=spotify_track_id,
        spotify_uri=spotify_uri,
        market=market,
    )

    track_uri = db_track["spotify_uri"]
    track_spotify_id = db_track["spotify_track_id"]

    added_to_good_songs = False

    if not playlist_role_contains_track(conn, "good_songs", db_track["id"]):
        spotify_call(
            sp.playlist_add_items,
            playlist_id=good_songs_playlist_id,
            items=[track_uri],
        )
        add_track_to_local_playlist(
            conn=conn,
            role="good_songs",
            track_id=db_track["id"],
        )
        added_to_good_songs = True

    spotify_call(
        sp.playlist_remove_all_occurrences_of_items,
        playlist_id=good_song_options_playlist_id,
        items=[track_uri],
    )
    remove_track_from_local_playlist(
        conn=conn,
        role="good_song_options",
        track_id=db_track["id"],
    )

    insert_review(
        conn=conn,
        track_id=db_track["id"],
        spotify_track_id=track_spotify_id,
        spotify_uri=track_uri,
        decision="thumbs_up",
        note=note,
    )

    conn.commit()

    errors: list[str] = []
    skipped_after_review = False
    if skip_after_review:
        try:
            spotify_call(sp.next_track, device_id=(device_id or None))
            skipped_after_review = True
        except Exception as exc:
            errors.append(f"review saved, but skip failed: {exc}")

    return {
        "success": True,
        "decision": "thumbs_up",
        "track": track_row_to_api(db_track),
        "addedToGoodSongs": added_to_good_songs,
        "removedFromGoodSongOptions": True,
        "skippedAfterReview": skipped_after_review,
        "errors": errors,
    }


def thumbs_down_track(
    conn: PgConnection,
    spotify_track_id: str | None = None,
    spotify_uri: str | None = None,
    note: str = "",
    market: str = "US",
    sp: spotipy.Spotify | None = None,
    skip_after_review: bool = False,
    device_id: str | None = None,
) -> dict[str, Any]:
    sp = sp or make_spotify_client(open_browser=False)

    good_song_options_playlist_id = get_good_song_options_playlist_id()

    db_track = find_or_fetch_track(
        conn=conn,
        sp=sp,
        spotify_track_id=spotify_track_id,
        spotify_uri=spotify_uri,
        market=market,
    )

    track_uri = db_track["spotify_uri"]
    track_spotify_id = db_track["spotify_track_id"]

    spotify_call(
        sp.playlist_remove_all_occurrences_of_items,
        playlist_id=good_song_options_playlist_id,
        items=[track_uri],
    )

    remove_track_from_local_playlist(
        conn=conn,
        role="good_song_options",
        track_id=db_track["id"],
    )

    insert_review(
        conn=conn,
        track_id=db_track["id"],
        spotify_track_id=track_spotify_id,
        spotify_uri=track_uri,
        decision="thumbs_down",
        note=note,
    )

    conn.commit()

    errors: list[str] = []
    skipped_after_review = False
    if skip_after_review:
        try:
            spotify_call(sp.next_track, device_id=(device_id or None))
            skipped_after_review = True
        except Exception as exc:
            errors.append(f"review saved, but skip failed: {exc}")

    return {
        "success": True,
        "decision": "thumbs_down",
        "track": track_row_to_api(db_track),
        "addedToGoodSongs": False,
        "removedFromGoodSongOptions": True,
        "skippedAfterReview": skipped_after_review,
        "errors": errors,
    }


def score_spotify_track(track_name: str, artists_value: str, track: dict[str, Any]) -> float:
    target_artists = split_artists(artists_value)

    result_title = track.get("name", "")
    result_artists = [artist.get("name", "") for artist in track.get("artists") or []]

    title_score = similarity(track_name, result_title)

    if target_artists and result_artists:
        artist_score = max(
            similarity(target_artist, result_artist)
            for target_artist in target_artists
            for result_artist in result_artists
        )
    elif not target_artists:
        artist_score = 0.75
    else:
        artist_score = 0.0

    popularity = track.get("popularity") or 0
    popularity_bonus = min(max(float(popularity), 0.0), 100.0) / 100.0

    return (0.72 * title_score) + (0.25 * artist_score) + (0.03 * popularity_bonus)


def resolve_track_by_search(
    conn: PgConnection,
    track_name: str,
    artists: str = "",
    market: str = "US",
    min_score: float = 0.78,
) -> dict[str, Any]:
    sp = make_spotify_client(open_browser=False)

    track_name = track_name.strip()
    artists = artists.strip()
    primary_artist = split_artists(artists)[0] if split_artists(artists) else ""

    if track_name and primary_artist:
        query = f'track:"{track_name}" artist:"{primary_artist}"'
    elif track_name:
        query = track_name
    else:
        return {
            "success": True,
            "resolved": False,
            "score": 0.0,
            "reason": "missing track name",
            "track": None,
        }

    search_attempts = [query]
    fallback = " ".join(part for part in [track_name, primary_artist] if part).strip()
    if fallback and fallback not in search_attempts:
        search_attempts.append(fallback)

    best_track: dict[str, Any] | None = None
    best_score = 0.0
    best_query = ""

    for attempt in search_attempts:
        results = spotify_call(
            sp.search,
            q=attempt,
            type="track",
            limit=10,
            market=market,
        )

        for candidate in (results.get("tracks") or {}).get("items") or []:
            score = score_spotify_track(track_name, artists, candidate)
            if score > best_score:
                best_score = score
                best_track = candidate
                best_query = attempt

    if not best_track:
        return {
            "success": True,
            "resolved": False,
            "score": 0.0,
            "reason": f"no Spotify result for query: {query}",
            "track": None,
        }

    if best_score < min_score:
        result_artists = safe_join(
            [artist.get("name", "") for artist in best_track.get("artists") or []]
        )
        return {
            "success": True,
            "resolved": False,
            "score": round(best_score, 3),
            "reason": (
                "best match below threshold: "
                f"{best_track.get('name', '')} by {result_artists} "
                f"via query {best_query!r}"
            ),
            "track": None,
        }

    payload = extract_track_payload(best_track)
    db_track = upsert_track(conn, payload)
    conn.commit()

    return {
        "success": True,
        "resolved": True,
        "score": round(best_score, 3),
        "reason": "resolved by search",
        "track": track_row_to_api(db_track),
    }


def add_track_to_local_playlist(
    conn: PgConnection,
    role: str,
    track_id: int,
) -> None:
    playlist = get_db_playlist_by_role(conn, role)
    if not playlist:
        raise RuntimeError(f"Spotify playlist role has not been synced yet: {role}")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spotify_playlist_tracks (
                playlist_id,
                track_id,
                position,
                added_at,
                added_by,
                last_seen_at
            )
            VALUES (%s, %s, NULL, now(), '', now())
            ON CONFLICT (playlist_id, track_id)
            DO UPDATE SET
                last_seen_at = now()
            """,
            (playlist["id"], track_id),
        )


def remove_track_from_local_playlist(
    conn: PgConnection,
    role: str,
    track_id: int,
) -> None:
    playlist = get_db_playlist_by_role(conn, role)
    if not playlist:
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM spotify_playlist_tracks
            WHERE playlist_id = %s
              AND track_id = %s
            """,
            (playlist["id"], track_id),
        )


def get_mobile_config() -> dict[str, Any]:
    good_song_options_playlist_id = get_good_song_options_playlist_id()
    android_redirect_uri = os.environ.get("SPOTIFY_ANDROID_REDIRECT_URI", "")

    return {
        "success": True,
        "clientId": require_env_var("SPOTIFY_CLIENT_ID"),
        "redirectUri": android_redirect_uri,
        "goodSongOptionsPlaylistId": good_song_options_playlist_id,
        "goodSongOptionsPlaylistUri": f"spotify:playlist:{good_song_options_playlist_id}",
        "usesBackendPlayer": True,
    }
