ALTER TABLE public.notifications
    DROP COLUMN data;

DROP INDEX public.idx_device_push_tokens_user_id;

ALTER TABLE public.device_push_tokens
    DROP CONSTRAINT device_push_tokens_user_id_fkey;

ALTER TABLE public.device_push_tokens
    DROP COLUMN user_id;

DROP TRIGGER trg_remihub_users_updated_at ON public.remihub_users;

DROP INDEX public.remihub_users_email_lower_uidx;

DROP TABLE public.remihub_users;
