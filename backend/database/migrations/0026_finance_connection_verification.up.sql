CREATE TABLE public.finance_plaid_verification (
    plaid_item_id text PRIMARY KEY,
    attempted_at timestamptz NOT NULL,
    succeeded_at timestamptz,
    method text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('VERIFIED', 'AUTH_REQUIRED', 'TRANSIENT', 'UNKNOWN')),
    connection_status text NOT NULL CHECK (connection_status IN ('VERIFIED', 'AUTH_REQUIRED', 'UNKNOWN')),
    inventory_status text NOT NULL CHECK (inventory_status IN ('AVAILABLE', 'MISSING', 'UNKNOWN')),
    available_account_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    missing_account_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    invalid_balance_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    failure_at timestamptz,
    live_balance_tested boolean NOT NULL DEFAULT false
);

CREATE TABLE public.finance_plaid_verification_lease (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    owner text,
    expires_at timestamptz,
    started_at timestamptz
);
