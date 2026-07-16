CREATE TABLE public.remihub_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firebase_uid text NOT NULL UNIQUE,
    email text NOT NULL,
    display_name text,
    role text NOT NULL DEFAULT 'member',
    is_active boolean NOT NULL DEFAULT TRUE,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT remihub_users_role_check
        CHECK (role IN ('admin', 'member'))
);

CREATE UNIQUE INDEX remihub_users_email_lower_uidx
    ON public.remihub_users (lower(email));

CREATE TRIGGER trg_remihub_users_updated_at
    BEFORE UPDATE ON public.remihub_users
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.device_push_tokens
    ADD COLUMN user_id uuid;

ALTER TABLE public.device_push_tokens
    ADD CONSTRAINT device_push_tokens_user_id_fkey
    FOREIGN KEY (user_id)
    REFERENCES public.remihub_users(id)
    ON DELETE CASCADE;

CREATE INDEX idx_device_push_tokens_user_id
    ON public.device_push_tokens (user_id);

ALTER TABLE public.notifications
    ADD COLUMN data jsonb NOT NULL DEFAULT '{}'::jsonb;
