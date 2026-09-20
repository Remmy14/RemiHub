DROP INDEX vehicle_maintenance_events_schedule_idx;

DROP INDEX vehicle_maintenance_events_vehicle_timeline_idx;

DROP TABLE public.vehicle_maintenance_events;

DROP INDEX vehicle_maintenance_schedules_vehicle_active_idx;

DROP TABLE public.vehicle_maintenance_schedules;

DROP INDEX vehicle_fuel_records_vehicle_latest_idx;

DROP INDEX vehicle_fuel_records_vehicle_filled_idx;

DROP TABLE public.vehicle_fuel_records;

DROP INDEX vehicle_odometer_source_record_uidx;

DROP INDEX vehicle_odometer_history_idx;

DROP TABLE public.vehicle_odometer_observations;

DROP INDEX vehicles_user_active_idx;

DROP TABLE public.vehicles;
