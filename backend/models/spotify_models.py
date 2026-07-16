from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


SpotifyPlaylistRole = Literal["good_song_options", "good_songs"]
SpotifyReviewDecision = Literal["thumbs_up", "thumbs_down", "skipped", "imported"]


class SpotifyMobileConfigResponse(BaseModel):
    success: bool
    clientId: str
    redirectUri: str
    goodSongOptionsPlaylistId: str
    goodSongOptionsPlaylistUri: str
    usesBackendPlayer: bool = True


class SpotifyTrack(BaseModel):
    id: int | None = None
    spotifyTrackId: str
    spotifyUri: str
    trackName: str
    artists: str = ""
    albumName: str = ""
    albumId: str = ""
    releaseDate: str = ""
    releaseYear: int | None = None
    durationMs: int | None = None
    explicit: bool | None = None
    popularity: int | None = None
    previewUrl: str = ""
    artworkUrl: str = ""
    spotifyUrl: str = ""
    normalizedTitleArtistKey: str = ""


class SpotifyDevice(BaseModel):
    id: str | None = None
    name: str = ""
    type: str = ""
    isActive: bool = False
    isRestricted: bool = False
    volumePercent: int | None = None


class SpotifyPlayerStateResponse(BaseModel):
    success: bool
    isPlaying: bool = False
    progressMs: int | None = None
    device: SpotifyDevice | None = None
    track: SpotifyTrack | None = None
    contextUri: str = ""
    currentlyPlayingType: str = ""
    hasActiveDevice: bool = False
    message: str = ""
    errors: list[str] = Field(default_factory=list)


class SpotifyDevicesResponse(BaseModel):
    success: bool
    devices: list[SpotifyDevice] = Field(default_factory=list)
    count: int = 0
    activeDeviceId: str | None = None
    errors: list[str] = Field(default_factory=list)


class SpotifyPlaybackCommandRequest(BaseModel):
    deviceId: str | None = None
    position: int | None = None
    spotifyTrackId: str | None = None
    spotifyUri: str | None = None


class SpotifyPlaybackCommandResponse(BaseModel):
    success: bool
    action: str
    player: SpotifyPlayerStateResponse | None = None
    message: str = ""
    errors: list[str] = Field(default_factory=list)


class SpotifyCurrentTrackVoteRequest(BaseModel):
    note: str = ""
    skipAfterReview: bool = True


class SpotifyPlaylist(BaseModel):
    id: int | None = None
    spotifyPlaylistId: str
    name: str
    role: SpotifyPlaylistRole
    snapshotId: str | None = None
    lastSyncedAt: datetime | None = None


class SpotifyPlaylistTrack(BaseModel):
    playlistRole: SpotifyPlaylistRole
    position: int | None = None
    addedAt: datetime | None = None
    track: SpotifyTrack


class SpotifySyncPlaylistResult(BaseModel):
    role: SpotifyPlaylistRole
    spotifyPlaylistId: str
    name: str
    totalFetched: int
    totalStored: int
    snapshotId: str | None = None


class SpotifySyncResponse(BaseModel):
    success: bool
    playlists: list[SpotifySyncPlaylistResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SpotifyReviewQueueResponse(BaseModel):
    success: bool
    tracks: list[SpotifyPlaylistTrack] = Field(default_factory=list)
    count: int = 0


class SpotifyVoteRequest(BaseModel):
    spotifyTrackId: str | None = None
    spotifyUri: str | None = None
    note: str = ""
    skipAfterReview: bool = False


class SpotifyVoteResponse(BaseModel):
    success: bool
    decision: SpotifyReviewDecision
    track: SpotifyTrack | None = None
    addedToGoodSongs: bool = False
    removedFromGoodSongOptions: bool = False
    skippedAfterReview: bool = False
    errors: list[str] = Field(default_factory=list)


class SpotifySummaryResponse(BaseModel):
    success: bool
    goodSongOptionsCount: int = 0
    goodSongsCount: int = 0
    thumbsUpCount: int = 0
    thumbsDownCount: int = 0


class SpotifySearchRequest(BaseModel):
    trackName: str
    artists: str = ""
    market: str = "US"
    minScore: float = 0.78


class SpotifySearchResult(BaseModel):
    success: bool
    resolved: bool
    score: float = 0.0
    reason: str = ""
    track: SpotifyTrack | None = None
