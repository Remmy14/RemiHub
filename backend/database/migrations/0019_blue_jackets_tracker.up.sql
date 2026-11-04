CREATE TABLE public.blue_jackets_games (
    nhl_game_id bigint PRIMARY KEY,
    season integer NOT NULL CHECK (season >= 20212022),
    game_type smallint NOT NULL CHECK (game_type IN (2, 3)),
    game_date date NOT NULL,
    start_time_utc timestamptz,
    opponent_abbrev text NOT NULL,
    cbj_home boolean NOT NULL,
    game_state text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (opponent_abbrev <> 'CBJ')
);

CREATE INDEX blue_jackets_games_season_date_idx
    ON public.blue_jackets_games (season, game_date, nhl_game_id);

CREATE TABLE public.blue_jackets_attendance (
    user_id uuid NOT NULL REFERENCES public.remihub_users(id) ON DELETE CASCADE,
    nhl_game_id bigint NOT NULL REFERENCES public.blue_jackets_games(nhl_game_id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('going', 'attended')),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, nhl_game_id)
);

CREATE INDEX blue_jackets_attendance_game_status_idx
    ON public.blue_jackets_attendance (nhl_game_id, status);

CREATE TABLE public.blue_jackets_game_stats (
    nhl_game_id bigint PRIMARY KEY REFERENCES public.blue_jackets_games(nhl_game_id) ON DELETE CASCADE,
    game_state text NOT NULL DEFAULT 'pending',
    goals_for smallint,
    goals_against smallint,
    shots_for smallint,
    shots_against smallint,
    result text CHECK (result IN ('W', 'L', 'OTL')),
    fetched_at timestamptz,
    next_retry_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (goals_for IS NULL OR goals_for >= 0),
    CHECK (goals_against IS NULL OR goals_against >= 0),
    CHECK (shots_for IS NULL OR shots_for >= 0),
    CHECK (shots_against IS NULL OR shots_against >= 0)
);

CREATE INDEX blue_jackets_game_stats_retry_idx
    ON public.blue_jackets_game_stats (next_retry_at)
    WHERE next_retry_at IS NOT NULL;
