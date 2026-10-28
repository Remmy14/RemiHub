-- Downgrade discards JD2 job IDs and submission outcomes collected since upgrade.
ALTER TABLE public.plex_download_requests
    DROP COLUMN jd2_job_id,
    DROP COLUMN submission_outcome;
