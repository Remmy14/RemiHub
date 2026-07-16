from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from backend.database.database import get_db_conn, put_db_conn
from backend.models.spotify_models import (
    SpotifyCurrentTrackVoteRequest,
    SpotifyDevicesResponse,
    SpotifyMobileConfigResponse,
    SpotifyPlaybackCommandRequest,
    SpotifyPlaybackCommandResponse,
    SpotifyPlayerStateResponse,
    SpotifyReviewQueueResponse,
    SpotifySearchRequest,
    SpotifySearchResult,
    SpotifySummaryResponse,
    SpotifySyncResponse,
    SpotifyVoteRequest,
    SpotifyVoteResponse,
)
from backend.services import spotify_service


router = APIRouter(prefix="/spotify", tags=["Spotify"])


@router.get("/mobile-config", response_model=SpotifyMobileConfigResponse)
def get_mobile_config() -> dict:
    try:
        return spotify_service.get_mobile_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/player", response_model=SpotifyPlayerStateResponse)
def get_player_state(
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.get_player_state(conn=conn, market=market)
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.get("/player/devices", response_model=SpotifyDevicesResponse)
def get_devices() -> dict:
    try:
        return spotify_service.get_devices()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/player/play-good-song-options", response_model=SpotifyPlaybackCommandResponse)
def play_good_song_options(
    request: SpotifyPlaybackCommandRequest | None = None,
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    conn = get_db_conn()
    try:
        request = request or SpotifyPlaybackCommandRequest()
        return spotify_service.play_good_song_options_playlist(
            conn=conn,
            device_id=request.deviceId,
            position=request.position,
            market=market,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/player/play-track", response_model=SpotifyPlaybackCommandResponse)
def play_track(
    request: SpotifyPlaybackCommandRequest,
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.play_track(
            conn=conn,
            spotify_track_id=request.spotifyTrackId,
            spotify_uri=request.spotifyUri,
            device_id=request.deviceId,
            market=market,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/player/resume", response_model=SpotifyPlaybackCommandResponse)
def resume_playback(
    request: SpotifyPlaybackCommandRequest | None = None,
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    conn = get_db_conn()
    try:
        request = request or SpotifyPlaybackCommandRequest()
        return spotify_service.resume_playback(
            conn=conn,
            device_id=request.deviceId,
            market=market,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/player/pause", response_model=SpotifyPlaybackCommandResponse)
def pause_playback(
    request: SpotifyPlaybackCommandRequest | None = None,
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    conn = get_db_conn()
    try:
        request = request or SpotifyPlaybackCommandRequest()
        return spotify_service.pause_playback(
            conn=conn,
            device_id=request.deviceId,
            market=market,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/player/next", response_model=SpotifyPlaybackCommandResponse)
def next_track(
    request: SpotifyPlaybackCommandRequest | None = None,
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    conn = get_db_conn()
    try:
        request = request or SpotifyPlaybackCommandRequest()
        return spotify_service.next_track(
            conn=conn,
            device_id=request.deviceId,
            market=market,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/player/previous", response_model=SpotifyPlaybackCommandResponse)
def previous_track(
    request: SpotifyPlaybackCommandRequest | None = None,
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    conn = get_db_conn()
    try:
        request = request or SpotifyPlaybackCommandRequest()
        return spotify_service.previous_track(
            conn=conn,
            device_id=request.deviceId,
            market=market,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/player/transfer", response_model=SpotifyPlaybackCommandResponse)
def transfer_playback(
    request: SpotifyPlaybackCommandRequest,
    market: str = Query(default="US", min_length=2, max_length=2),
) -> dict:
    if not request.deviceId:
        raise HTTPException(status_code=400, detail="deviceId is required")

    conn = get_db_conn()
    try:
        return spotify_service.transfer_playback(
            conn=conn,
            device_id=request.deviceId,
            market=market,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/sync", response_model=SpotifySyncResponse)
def sync_spotify() -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.sync_spotify_playlists(conn)
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.get("/review-queue", response_model=SpotifyReviewQueueResponse)
def get_review_queue(
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.get_review_queue(conn, limit=limit)
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.get("/summary", response_model=SpotifySummaryResponse)
def get_summary() -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.get_summary(conn)
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/thumbs-up", response_model=SpotifyVoteResponse)
def thumbs_up(request: SpotifyVoteRequest) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.thumbs_up_track(
            conn=conn,
            spotify_track_id=request.spotifyTrackId,
            spotify_uri=request.spotifyUri,
            note=request.note,
            skip_after_review=request.skipAfterReview,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/thumbs-down", response_model=SpotifyVoteResponse)
def thumbs_down(request: SpotifyVoteRequest) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.thumbs_down_track(
            conn=conn,
            spotify_track_id=request.spotifyTrackId,
            spotify_uri=request.spotifyUri,
            note=request.note,
            skip_after_review=request.skipAfterReview,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/current/thumbs-up", response_model=SpotifyVoteResponse)
def thumbs_up_current(request: SpotifyCurrentTrackVoteRequest) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.thumbs_up_current_track(
            conn=conn,
            note=request.note,
            skip_after_review=request.skipAfterReview,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/current/thumbs-down", response_model=SpotifyVoteResponse)
def thumbs_down_current(request: SpotifyCurrentTrackVoteRequest) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.thumbs_down_current_track(
            conn=conn,
            note=request.note,
            skip_after_review=request.skipAfterReview,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)


@router.post("/search", response_model=SpotifySearchResult)
def search_track(request: SpotifySearchRequest) -> dict:
    conn = get_db_conn()
    try:
        return spotify_service.resolve_track_by_search(
            conn=conn,
            track_name=request.trackName,
            artists=request.artists,
            market=request.market,
            min_score=request.minScore,
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        put_db_conn(conn)
