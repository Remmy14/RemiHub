from __future__ import annotations

from typing import Generator

from fastapi import APIRouter, HTTPException, Query

from backend.database.database import get_db_conn, put_db_conn
from backend.models.spotify_models import (
    SpotifyReviewQueueResponse,
    SpotifySearchRequest,
    SpotifySearchResult,
    SpotifySummaryResponse,
    SpotifySyncResponse,
    SpotifyVoteRequest,
    SpotifyVoteResponse,
    SpotifyMobileConfigResponse,
)
from backend.services import spotify_service


router = APIRouter(prefix="/spotify", tags=["Spotify"])

@router.get("/mobile-config", response_model=SpotifyMobileConfigResponse)
def get_mobile_config() -> dict:
    try:
        return spotify_service.get_mobile_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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