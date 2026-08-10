CREATE TABLE public.service_health_current_snapshot (
    singleton_id text PRIMARY KEY,
    checked_at timestamp with time zone NOT NULL,
    overall text NOT NULL,
    snapshot jsonb NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT service_health_current_snapshot_singleton_check
        CHECK (singleton_id = 'current'),
    CONSTRAINT service_health_current_snapshot_overall_check
        CHECK (overall IN ('healthy', 'degraded', 'unhealthy', 'idle', 'unknown')),
    CONSTRAINT service_health_current_snapshot_payload_check
        CHECK (
            jsonb_typeof(snapshot) = 'object'
            AND snapshot ? 'success'
            AND snapshot ? 'checked_at'
            AND snapshot ? 'overall'
            AND snapshot ? 'components'
            AND jsonb_typeof(snapshot->'components') = 'array'
        )
);
