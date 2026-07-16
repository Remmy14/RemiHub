from __future__ import annotations
from dotenv import load_dotenv

import os
import sys
from spotipy.oauth2 import SpotifyOAuth

from backend.config import resolve_environment_file_path


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


def require_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    client_id = require_env_var("SPOTIFY_CLIENT_ID")
    client_secret = require_env_var("SPOTIFY_CLIENT_SECRET")
    redirect_uri = require_env_var("SPOTIFY_REDIRECT_URI")
    cache_path = os.environ.get("SPOTIFY_TOKEN_CACHE_PATH", ".spotify_token_cache")

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SPOTIFY_SCOPE,
        open_browser=False,
        cache_path=cache_path,
    )

    token_info = auth_manager.get_cached_token()
    if token_info:
        print("Spotify token cache already exists and is readable.")
        print(f"Cache path: {os.path.abspath(cache_path)}")
        return 0

    auth_url = auth_manager.get_authorize_url()

    print()
    print("Open this URL in your browser:")
    print()
    print(auth_url)
    print()
    print("After approving, Spotify will redirect you to your redirect URI.")
    print("Copy the full redirected URL and paste it here.")
    print()

    redirected_url = input("Redirected URL: ").strip()
    code = auth_manager.parse_response_code(redirected_url)

    if not code:
        print("Could not parse auth code from redirected URL.", file=sys.stderr)
        return 1

    token_info = auth_manager.get_access_token(code, as_dict=True)

    if not token_info:
        print("Failed to create Spotify token cache.", file=sys.stderr)
        return 1

    print()
    print("Spotify auth setup complete.")
    print(f"Cache path: {os.path.abspath(cache_path)}")
    return 0


if __name__ == "__main__":
    # Load environment variables
    _ENV_PATH = resolve_environment_file_path()
    print(f'Environment variables: {_ENV_PATH}')
    print(f'Current Directory: {os.getcwd()}')
    load_dotenv(dotenv_path=_ENV_PATH, override=False)

    raise SystemExit(main())