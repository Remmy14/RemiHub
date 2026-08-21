DROP INDEX mead_tasks_batch_status_due_idx;

DROP INDEX mead_tasks_due_pending_idx;

DROP INDEX mead_tasks_generated_source_uidx;

DROP TABLE public.mead_tasks;

DROP INDEX mead_events_batch_gravity_latest_idx;

DROP INDEX mead_events_batch_timeline_idx;

DROP TABLE public.mead_events;

DROP INDEX mead_recipe_items_batch_order_idx;

DROP TABLE public.mead_recipe_items;

DROP INDEX mead_batches_user_stage_start_idx;

DROP TABLE public.mead_batches;
