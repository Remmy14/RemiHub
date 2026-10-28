-- Downgrading discards lifecycle, link inventory, processing and cleanup history.
DROP INDEX plex_download_events_request_idx;
DROP TABLE public.plex_download_events;
DROP INDEX plex_download_files_request_idx;
DROP INDEX plex_download_files_source_idx;
DROP TABLE public.plex_download_files;
DROP INDEX plex_download_links_source_idx;
DROP TABLE public.plex_download_links;
