-- Preflight on QA: inspect the table definition and resolve duplicate normalized
-- active labels or non-null valuation keys before applying this migration.
ALTER TABLE public.finance_manual_assets
    ADD COLUMN active boolean NOT NULL DEFAULT true,
    ADD COLUMN classification text GENERATED ALWAYS AS (
        CASE WHEN asset_category = 'liability' THEN 'liability' ELSE 'asset' END
    ) STORED;

CREATE UNIQUE INDEX finance_manual_active_name_uidx
    ON public.finance_manual_assets (lower(btrim(label)))
    WHERE active;

-- Keep keys reserved even when a fallback is archived.
CREATE UNIQUE INDEX finance_manual_valuation_key_uidx
    ON public.finance_manual_assets (valuation_key)
    WHERE valuation_key IS NOT NULL;

ALTER TABLE public.finance_manual_assets
    ADD CONSTRAINT finance_manual_current_value_check
        CHECK (
            current_value::text NOT IN ('NaN', 'Infinity', '-Infinity')
            AND current_value >= 0
            AND current_value::numeric = round(current_value::numeric, 2)
        );
