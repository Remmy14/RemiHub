CREATE TABLE agent.codex_execution_usage (
    run_id uuid NOT NULL REFERENCES agent.runs(id) ON DELETE CASCADE,
    attempt_count integer NOT NULL CHECK (attempt_count > 0),
    turn_id text NOT NULL DEFAULT '',
    card_id uuid NOT NULL REFERENCES agent.cards(id) ON DELETE CASCADE,
    card_revision integer NOT NULL CHECK (card_revision > 0),
    stage text NOT NULL CHECK (stage IN ('planning','implementation','review','deployment')),
    review_attempt integer,
    configured_model text,
    actual_model text,
    reasoning_effort text,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    cached_input_tokens bigint CHECK (cached_input_tokens >= 0),
    reasoning_tokens bigint CHECK (reasoning_tokens >= 0),
    total_tokens bigint CHECK (total_tokens >= 0),
    usage_status text NOT NULL CHECK (usage_status IN ('available','unavailable','unsupported')),
    usage_source text NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, attempt_count, turn_id)
);
CREATE INDEX codex_execution_usage_period_idx ON agent.codex_execution_usage (recorded_at, stage, card_id);

CREATE TABLE agent.codex_quota_snapshots (
    slot text PRIMARY KEY,
    source text NOT NULL,
    status text NOT NULL CHECK (status IN ('available','collection_failed')),
    payload jsonb,
    collected_at timestamptz,
    last_attempt_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error_code text
);

CREATE TABLE agent.codex_stage_configuration (
    stage text NOT NULL CHECK (stage IN ('planning','implementation','review')),
    worker_id text NOT NULL,
    configured_model text,
    configured_reasoning_effort text,
    reported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stage, worker_id)
);
CREATE INDEX codex_stage_configuration_recent_idx ON agent.codex_stage_configuration (stage, reported_at DESC);
