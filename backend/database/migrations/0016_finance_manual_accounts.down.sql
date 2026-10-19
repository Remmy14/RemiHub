-- Archive state is lost on downgrade. Inspect archived rows before running.
ALTER TABLE public.finance_manual_assets
    DROP CONSTRAINT finance_manual_current_value_check;

DROP INDEX finance_manual_valuation_key_uidx;
DROP INDEX finance_manual_active_name_uidx;

ALTER TABLE public.finance_manual_assets
    DROP COLUMN classification,
    DROP COLUMN active;
