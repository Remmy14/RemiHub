ALTER TABLE public.plex_download_requests
    ADD COLUMN jd2_job_id BIGINT NULL,
    ADD COLUMN submission_outcome TEXT NULL;

-- NULL outcome denotes a legacy FolderWatch request.
