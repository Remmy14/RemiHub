DROP TABLE public.finance_verification_runs;
ALTER TABLE public.finance_plaid_verification_lease
    DROP COLUMN monitoring_retry_at,
    DROP COLUMN monitoring_generation,
    DROP COLUMN monitoring_pending,
    DROP COLUMN scheduled_pending,
    DROP COLUMN run_id,
    DROP COLUMN run_trigger;
