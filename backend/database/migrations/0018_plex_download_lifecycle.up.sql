CREATE TABLE public.plex_download_links (
    request_id uuid NOT NULL REFERENCES public.plex_download_requests(id),
    link_id bigint NOT NULL,
    jd2_job_id bigint NOT NULL,
    package_id bigint,
    source_path text,
    name text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('media', 'archive', 'other')),
    finished boolean NOT NULL DEFAULT false,
    extraction_status text,
    observed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (request_id, link_id)
);

CREATE INDEX plex_download_links_source_idx ON public.plex_download_links (source_path)
    WHERE source_path IS NOT NULL;

CREATE TABLE public.plex_download_files (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid REFERENCES public.plex_download_requests(id),
    jd2_job_id bigint,
    link_id bigint,
    source_path text NOT NULL,
    source_size bigint,
    destination_path text,
    state text NOT NULL CHECK (state IN ('queued_for_plex', 'processing', 'completed', 'failed')),
    outcome text,
    error text,
    discovered_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX plex_download_files_request_idx ON public.plex_download_files (request_id);
CREATE INDEX plex_download_files_source_idx ON public.plex_download_files (source_path, discovered_at DESC);

CREATE TABLE public.plex_download_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id uuid REFERENCES public.plex_download_requests(id),
    file_id uuid REFERENCES public.plex_download_files(id),
    state text NOT NULL,
    detail text,
    occurred_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (request_id IS NOT NULL OR file_id IS NOT NULL)
);

CREATE INDEX plex_download_events_request_idx
    ON public.plex_download_events (request_id, occurred_at DESC, id DESC);
