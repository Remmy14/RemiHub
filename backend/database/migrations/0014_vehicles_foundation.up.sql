CREATE TABLE public.vehicles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    year integer NOT NULL,
    make text NOT NULL,
    model text NOT NULL,
    trim text,
    engine text,
    fuel_type text NOT NULL,
    tank_capacity_gallons numeric(8, 3),
    vin text,
    license_plate text,
    license_state text,
    purchase_date date,
    purchase_odometer_miles numeric(10, 1),
    active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT vehicles_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES public.remihub_users(id)
        ON DELETE CASCADE,
    CONSTRAINT vehicles_year_check
        CHECK (year BETWEEN 1886 AND 3000),
    CONSTRAINT vehicles_make_check
        CHECK (char_length(btrim(make)) BETWEEN 1 AND 120),
    CONSTRAINT vehicles_model_check
        CHECK (char_length(btrim(model)) BETWEEN 1 AND 120),
    CONSTRAINT vehicles_trim_check
        CHECK (trim IS NULL OR char_length(btrim(trim)) <= 120),
    CONSTRAINT vehicles_engine_check
        CHECK (engine IS NULL OR char_length(btrim(engine)) <= 120),
    CONSTRAINT vehicles_fuel_type_check
        CHECK (char_length(btrim(fuel_type)) BETWEEN 1 AND 80),
    CONSTRAINT vehicles_tank_capacity_check
        CHECK (tank_capacity_gallons IS NULL OR tank_capacity_gallons > 0),
    CONSTRAINT vehicles_vin_check
        CHECK (vin IS NULL OR char_length(btrim(vin)) <= 32),
    CONSTRAINT vehicles_license_plate_check
        CHECK (license_plate IS NULL OR char_length(btrim(license_plate)) <= 32),
    CONSTRAINT vehicles_license_state_check
        CHECK (license_state IS NULL OR char_length(btrim(license_state)) <= 32),
    CONSTRAINT vehicles_purchase_odometer_check
        CHECK (purchase_odometer_miles IS NULL OR purchase_odometer_miles >= 0)
);

CREATE INDEX vehicles_user_active_idx
    ON public.vehicles (user_id, active, make, model);

CREATE TABLE public.vehicle_odometer_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id uuid NOT NULL,
    odometer_miles numeric(10, 1) NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    source text NOT NULL,
    source_record_type text,
    source_record_id uuid,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT vehicle_odometer_vehicle_id_fkey
        FOREIGN KEY (vehicle_id)
        REFERENCES public.vehicles(id)
        ON DELETE CASCADE,
    CONSTRAINT vehicle_odometer_miles_check
        CHECK (odometer_miles >= 0),
    CONSTRAINT vehicle_odometer_source_check
        CHECK (source IN ('MANUAL', 'FUEL', 'MAINTENANCE')),
    CONSTRAINT vehicle_odometer_source_record_check
        CHECK (
            (source = 'MANUAL' AND source_record_type IS NULL AND source_record_id IS NULL)
            OR (source <> 'MANUAL' AND source_record_type IS NOT NULL AND source_record_id IS NOT NULL)
        )
);

CREATE INDEX vehicle_odometer_history_idx
    ON public.vehicle_odometer_observations (
        vehicle_id,
        observed_at DESC,
        created_at DESC,
        id DESC
    );

CREATE UNIQUE INDEX vehicle_odometer_source_record_uidx
    ON public.vehicle_odometer_observations (
        vehicle_id,
        source,
        source_record_type,
        source_record_id
    )
    WHERE source_record_id IS NOT NULL;

CREATE TABLE public.vehicle_fuel_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id uuid NOT NULL,
    filled_at timestamp with time zone NOT NULL,
    odometer_miles numeric(10, 1) NOT NULL,
    gallons numeric(10, 3) NOT NULL,
    total_cost numeric(12, 2) NOT NULL,
    price_per_gallon numeric(8, 3) NOT NULL,
    is_full_fill boolean NOT NULL DEFAULT true,
    driving_context text NOT NULL DEFAULT 'NORMAL',
    fuel_grade text,
    station_name text,
    notes text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT vehicle_fuel_records_vehicle_id_fkey
        FOREIGN KEY (vehicle_id)
        REFERENCES public.vehicles(id)
        ON DELETE CASCADE,
    CONSTRAINT vehicle_fuel_records_odometer_check
        CHECK (odometer_miles >= 0),
    CONSTRAINT vehicle_fuel_records_gallons_check
        CHECK (gallons > 0),
    CONSTRAINT vehicle_fuel_records_total_cost_check
        CHECK (total_cost > 0),
    CONSTRAINT vehicle_fuel_records_price_check
        CHECK (price_per_gallon > 0),
    CONSTRAINT vehicle_fuel_records_context_check
        CHECK (driving_context IN ('NORMAL', 'TOWING', 'MIXED')),
    CONSTRAINT vehicle_fuel_records_grade_check
        CHECK (fuel_grade IS NULL OR char_length(btrim(fuel_grade)) <= 80),
    CONSTRAINT vehicle_fuel_records_station_check
        CHECK (station_name IS NULL OR char_length(btrim(station_name)) <= 160)
);

CREATE INDEX vehicle_fuel_records_vehicle_filled_idx
    ON public.vehicle_fuel_records (vehicle_id, filled_at, created_at, id);

CREATE INDEX vehicle_fuel_records_vehicle_latest_idx
    ON public.vehicle_fuel_records (vehicle_id, filled_at DESC, created_at DESC, id DESC);

CREATE TABLE public.vehicle_maintenance_schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id uuid NOT NULL,
    name text NOT NULL,
    category text NOT NULL,
    description text,
    mileage_interval_miles numeric(10, 1),
    time_interval_days integer,
    last_service_odometer_miles numeric(10, 1),
    last_service_date date,
    due_soon_miles numeric(10, 1) NOT NULL DEFAULT 500,
    due_soon_days integer NOT NULL DEFAULT 14,
    overdue_miles numeric(10, 1) NOT NULL DEFAULT 500,
    overdue_days integer NOT NULL DEFAULT 14,
    active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT vehicle_maintenance_schedules_vehicle_id_fkey
        FOREIGN KEY (vehicle_id)
        REFERENCES public.vehicles(id)
        ON DELETE CASCADE,
    CONSTRAINT vehicle_maintenance_schedules_name_check
        CHECK (char_length(btrim(name)) BETWEEN 1 AND 160),
    CONSTRAINT vehicle_maintenance_schedules_category_check
        CHECK (char_length(btrim(category)) BETWEEN 1 AND 120),
    CONSTRAINT vehicle_maintenance_schedules_interval_check
        CHECK (mileage_interval_miles IS NOT NULL OR time_interval_days IS NOT NULL),
    CONSTRAINT vehicle_maintenance_schedules_mileage_interval_check
        CHECK (mileage_interval_miles IS NULL OR mileage_interval_miles > 0),
    CONSTRAINT vehicle_maintenance_schedules_time_interval_check
        CHECK (time_interval_days IS NULL OR time_interval_days > 0),
    CONSTRAINT vehicle_maintenance_schedules_last_mileage_check
        CHECK (last_service_odometer_miles IS NULL OR last_service_odometer_miles >= 0),
    CONSTRAINT vehicle_maintenance_schedules_due_soon_miles_check
        CHECK (due_soon_miles >= 0),
    CONSTRAINT vehicle_maintenance_schedules_due_soon_days_check
        CHECK (due_soon_days >= 0),
    CONSTRAINT vehicle_maintenance_schedules_overdue_miles_check
        CHECK (overdue_miles >= 0),
    CONSTRAINT vehicle_maintenance_schedules_overdue_days_check
        CHECK (overdue_days >= 0)
);

CREATE INDEX vehicle_maintenance_schedules_vehicle_active_idx
    ON public.vehicle_maintenance_schedules (vehicle_id, active, category, name);

CREATE TABLE public.vehicle_maintenance_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id uuid NOT NULL,
    maintenance_schedule_id uuid,
    performed_at timestamp with time zone NOT NULL,
    odometer_miles numeric(10, 1) NOT NULL,
    category text NOT NULL,
    action text NOT NULL,
    description text NOT NULL,
    cost numeric(12, 2),
    service_provider text,
    notes text,
    created_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT vehicle_maintenance_events_vehicle_id_fkey
        FOREIGN KEY (vehicle_id)
        REFERENCES public.vehicles(id)
        ON DELETE CASCADE,
    CONSTRAINT vehicle_maintenance_events_schedule_id_fkey
        FOREIGN KEY (maintenance_schedule_id)
        REFERENCES public.vehicle_maintenance_schedules(id)
        ON DELETE SET NULL,
    CONSTRAINT vehicle_maintenance_events_odometer_check
        CHECK (odometer_miles >= 0),
    CONSTRAINT vehicle_maintenance_events_category_check
        CHECK (char_length(btrim(category)) BETWEEN 1 AND 120),
    CONSTRAINT vehicle_maintenance_events_action_check
        CHECK (action IN ('REPLACED', 'SERVICED', 'INSPECTED', 'ROTATED', 'REPAIRED', 'OTHER')),
    CONSTRAINT vehicle_maintenance_events_description_check
        CHECK (char_length(btrim(description)) BETWEEN 1 AND 4000),
    CONSTRAINT vehicle_maintenance_events_cost_check
        CHECK (cost IS NULL OR cost >= 0),
    CONSTRAINT vehicle_maintenance_events_provider_check
        CHECK (service_provider IS NULL OR char_length(btrim(service_provider)) <= 160)
);

CREATE INDEX vehicle_maintenance_events_vehicle_timeline_idx
    ON public.vehicle_maintenance_events (vehicle_id, performed_at DESC, created_at DESC, id DESC);

CREATE INDEX vehicle_maintenance_events_schedule_idx
    ON public.vehicle_maintenance_events (maintenance_schedule_id, performed_at DESC)
    WHERE maintenance_schedule_id IS NOT NULL;
