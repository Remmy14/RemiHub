import { apiRequest } from "./authenticatedApi";

export type FitnessWorkoutType = "RUNNING" | "LIFTING";
export type FitnessWorkoutStatus =
  | "PLANNED"
  | "COMPLETED"
  | "SKIPPED"
  | "RESCHEDULED";

type FitnessResponse<T> = {
  success: true;
  data: T;
};

export type FitnessRunningResult = {
  planned_distance_miles: number | null;
  completed_distance_miles: number;
  duration_seconds: number;
  notes: string | null;
  external_provider: string | null;
  external_activity_id: string | null;
  external_activity_uuid: string | null;
  external_activity_name: string | null;
  moving_duration_seconds: number | null;
  average_speed_meters_per_second: number | null;
  average_hr: number | null;
  max_hr: number | null;
  training_load: number | null;
  aerobic_training_effect: number | null;
  anaerobic_training_effect: number | null;
  training_effect_label: string | null;
  vo2_max: number | null;
  hr_zone_1_seconds: number | null;
  hr_zone_2_seconds: number | null;
  hr_zone_3_seconds: number | null;
  hr_zone_4_seconds: number | null;
  hr_zone_5_seconds: number | null;
  average_cadence_spm: number | null;
  average_power_watts: number | null;
  average_stride_length_meters: number | null;
  elevation_gain_meters: number | null;
  elevation_loss_meters: number | null;
  calories: number | null;
  steps: number | null;
  created_at: string | null;
  updated_at: string | null;
};

export type FitnessLiftingEntry = {
  scheduled_workout_id: string;
  id: string;
  exercise_id: string;
  exercise_name: string;
  weight_unit: "lb" | "kg" | string;
  week_start: string;
  workout_day_slot: number;
  workout_date: string | null;
  weight: number;
  reps: number;
  sets: number | null;
  notes: string | null;
  completed: boolean;
  created_at: string | null;
  updated_at: string | null;
};

export type FitnessLiftingResult = {
  fitness_scheduled_workout_id: string;
  entries: FitnessLiftingEntry[];
};

export type FitnessScheduledWorkout = {
  id: string;
  user_id: string;
  workout_template_id: string;
  plan_instance_id: string | null;
  plan_template_item_id: string | null;
  recurring_series_id: string | null;
  scheduled_date: string;
  original_scheduled_date: string;
  status: FitnessWorkoutStatus;
  replacement_scheduled_workout_id: string | null;
  planned_distance_miles: number | null;
  workout_name: string;
  type: FitnessWorkoutType;
  source?: FitnessWorkoutSource;
  running_result: FitnessRunningResult | null;
  lifting_result: FitnessLiftingResult | null;
  created_at: string | null;
  updated_at: string | null;
};

export type FitnessWorkoutSource = {
  type: "INDIVIDUAL" | "RECURRING_SERIES" | "TRAINING_PLAN" | "RESCHEDULE_REPLACEMENT";
  label: string;
  recurring_series_id: string | null;
  plan_instance_id: string | null;
  plan_template_name: string | null;
  recurring_series_weekdays: number[] | null;
};

export type FitnessRecurringSeriesRequest = {
  workout_template_id: string;
  start_date: string;
  weekdays: number[];
  duration_weeks?: number | null;
  end_date?: string | null;
  idempotency_key?: string | null;
};

export type FitnessRecurringSeries = {
  id?: string;
  user_id?: string;
  workout_template_id: string;
  workout_name: string;
  type: FitnessWorkoutType;
  start_date: string;
  end_date: string;
  duration_weeks: number | null;
  weekdays: number[];
  status?: "ACTIVE" | "STOPPED";
  dates?: string[];
  count: number;
  scheduled_workout_ids?: string[];
  scheduled_workouts?: FitnessScheduledWorkout[];
};

export type FitnessWeeklySummary = {
  planned_running_miles: number;
  actual_running_miles: number;
  longest_planned_run_miles: number | null;
  longest_completed_run_miles: number | null;
  planned_mileage_change: number | null;
  actual_mileage_change: number | null;
  planned_long_run_percentage: number | null;
  completed_lifting_sessions: number;
};

export type FitnessCalendarDay = {
  date: string;
  is_today: boolean;
  workouts: FitnessScheduledWorkout[];
};

export type FitnessCalendarWeek = {
  week_start: string;
  days: FitnessCalendarDay[];
  summary: FitnessWeeklySummary;
};

export type FitnessTrainingCalendar = {
  start_date: string;
  end_date: string;
  weeks: FitnessCalendarWeek[];
};

export type FitnessLiftingTemplateExercise = {
  exercise_id: string;
  name: string;
  active: boolean;
  display_order: number;
};

export type FitnessWorkoutTemplate = {
  id: string;
  user_id: string;
  name: string;
  type: FitnessWorkoutType;
  notes: string | null;
  active: boolean;
  planned_distance_miles: number | null;
  exercises?: FitnessLiftingTemplateExercise[];
  created_at: string | null;
  updated_at: string | null;
};

export type FitnessPlanTemplateItem = {
  id?: string;
  workout_template_id: string;
  day_offset: number;
  display_order: number;
  workout_name?: string;
  type?: FitnessWorkoutType;
};

export type FitnessPlanTemplate = {
  id: string;
  user_id: string;
  name: string;
  notes: string | null;
  active: boolean;
  items?: FitnessPlanTemplateItem[];
  created_at: string | null;
  updated_at: string | null;
};

export type FitnessPlanInstance = {
  id: string;
  user_id: string;
  plan_template_id: string;
  plan_template_name: string;
  start_date: string;
  status: "ACTIVE" | "COMPLETED";
  planning_status?: "ACTIVE" | "STOPPED";
  stopped_at?: string | null;
  scheduled_workout_ids?: string[];
  scheduled_workouts?: FitnessScheduledWorkout[];
  created_at: string | null;
  updated_at: string | null;
};

export type FitnessPlanInstanceRepeatWeekResult = FitnessPlanInstance & {
  repeat_operation_id: string;
  selected_week_start: string;
  selected_week_end: string;
  repeated_week_start: string;
  repeated_week_end: string;
  repeated_scheduled_workout_ids: string[];
  repeated_count: number;
  shifted_scheduled_workout_ids: string[];
  shifted_count: number;
};

export type RunningCompletionRequest = {
  completed_distance_miles: number;
  duration_seconds: number;
  notes?: string | null;
};

export type FitnessRescheduleResult = {
  original: FitnessScheduledWorkout;
  replacement: FitnessScheduledWorkout;
};

function query(params: Record<string, string | boolean>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    search.set(key, String(value));
  });
  return search.toString();
}

export async function listTodayWorkouts(
  date: string,
): Promise<FitnessScheduledWorkout[]> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout[]>>(
    `/fitness/today?${query({ date })}`,
  );
  return response.data;
}

export async function listScheduledWorkouts(
  startDate: string,
  endDate: string,
): Promise<FitnessScheduledWorkout[]> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout[]>>(
    `/fitness/scheduled-workouts?${query({
      start_date: startDate,
      end_date: endDate,
    })}`,
  );
  return response.data;
}

export async function getTrainingCalendar(
  startDate: string,
  endDate: string,
): Promise<FitnessTrainingCalendar> {
  const response = await apiRequest<FitnessResponse<FitnessTrainingCalendar>>(
    `/fitness/training-calendar?${query({
      start_date: startDate,
      end_date: endDate,
    })}`,
  );
  return response.data;
}

export async function listWorkoutHistory(
  startDate: string,
  endDate: string,
): Promise<FitnessScheduledWorkout[]> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout[]>>(
    `/fitness/history?${query({
      start_date: startDate,
      end_date: endDate,
    })}`,
  );
  return response.data;
}

export async function getScheduledWorkout(
  workoutId: string,
): Promise<FitnessScheduledWorkout> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout>>(
    `/fitness/scheduled-workouts/${workoutId}`,
  );
  return response.data;
}

export async function createScheduledWorkout(
  workoutTemplateId: string,
  scheduledDate: string,
): Promise<FitnessScheduledWorkout> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout>>(
    "/fitness/scheduled-workouts",
    {
      method: "POST",
      body: JSON.stringify({
        workout_template_id: workoutTemplateId,
        scheduled_date: scheduledDate,
      }),
    },
  );
  return response.data;
}

export async function previewRecurringSeries(
  payload: FitnessRecurringSeriesRequest,
): Promise<FitnessRecurringSeries> {
  const response = await apiRequest<FitnessResponse<FitnessRecurringSeries>>(
    "/fitness/recurring-series/preview",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}

export async function createRecurringSeries(
  payload: FitnessRecurringSeriesRequest,
): Promise<FitnessRecurringSeries> {
  const response = await apiRequest<FitnessResponse<FitnessRecurringSeries>>(
    "/fitness/recurring-series",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}

export async function removeScheduledWorkout(
  workoutId: string,
): Promise<{ removed_scheduled_workout_id: string }> {
  const response = await apiRequest<FitnessResponse<{ removed_scheduled_workout_id: string }>>(
    `/fitness/scheduled-workouts/${workoutId}`,
    { method: "DELETE" },
  );
  return response.data;
}

export async function completeScheduledWorkout(
  workoutId: string,
  running?: RunningCompletionRequest,
): Promise<FitnessScheduledWorkout> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout>>(
    `/fitness/scheduled-workouts/${workoutId}/complete`,
    {
      method: "POST",
      body: JSON.stringify(running ? { running } : {}),
    },
  );
  return response.data;
}

export async function skipScheduledWorkout(
  workoutId: string,
): Promise<FitnessScheduledWorkout> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout>>(
    `/fitness/scheduled-workouts/${workoutId}/skip`,
    { method: "POST" },
  );
  return response.data;
}

export async function rescheduleScheduledWorkout(
  workoutId: string,
  scheduledDate: string,
): Promise<FitnessRescheduleResult> {
  const response = await apiRequest<FitnessResponse<FitnessRescheduleResult>>(
    `/fitness/scheduled-workouts/${workoutId}/reschedule`,
    {
      method: "POST",
      body: JSON.stringify({ scheduled_date: scheduledDate }),
    },
  );
  return response.data;
}

export async function replaceScheduledWorkoutTemplate(
  workoutId: string,
  workoutTemplateId: string,
): Promise<FitnessScheduledWorkout> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout>>(
    `/fitness/scheduled-workouts/${workoutId}/replace-template`,
    {
      method: "POST",
      body: JSON.stringify({ workout_template_id: workoutTemplateId }),
    },
  );
  return response.data;
}

export async function undoRescheduleScheduledWorkout(
  workoutId: string,
): Promise<{ original: FitnessScheduledWorkout; removed_replacement_scheduled_workout_id: string }> {
  const response = await apiRequest<
    FitnessResponse<{ original: FitnessScheduledWorkout; removed_replacement_scheduled_workout_id: string }>
  >(
    `/fitness/scheduled-workouts/${workoutId}/undo-reschedule`,
    { method: "POST" },
  );
  return response.data;
}

export async function removeRemainingRecurringWorkouts(
  seriesId: string,
  fromDate?: string,
): Promise<{ removed_count: number; removed_scheduled_workout_ids: string[] }> {
  const response = await apiRequest<
    FitnessResponse<{ removed_count: number; removed_scheduled_workout_ids: string[] }>
  >(
    `/fitness/recurring-series/${seriesId}/remove-remaining`,
    {
      method: "POST",
      body: JSON.stringify(fromDate ? { from_date: fromDate } : {}),
    },
  );
  return response.data;
}

export async function listWorkoutTemplates(
  includeArchived = false,
): Promise<FitnessWorkoutTemplate[]> {
  const response = await apiRequest<FitnessResponse<FitnessWorkoutTemplate[]>>(
    `/fitness/workout-templates?${query({
      include_archived: includeArchived,
    })}`,
  );
  return response.data;
}

export async function getWorkoutTemplate(
  templateId: string,
): Promise<FitnessWorkoutTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessWorkoutTemplate>>(
    `/fitness/workout-templates/${templateId}`,
  );
  return response.data;
}

export async function listCompletedWorkoutsForTemplate(
  templateId: string,
): Promise<FitnessScheduledWorkout[]> {
  const response = await apiRequest<FitnessResponse<FitnessScheduledWorkout[]>>(
    `/fitness/workout-templates/${templateId}/completed-workouts`,
  );
  return response.data;
}

export type WorkoutTemplatePayload = {
  name: string;
  type: FitnessWorkoutType;
  notes: string | null;
  planned_distance_miles?: number | null;
  exercises?: Array<{ exercise_id: string; display_order: number }>;
};

export async function createWorkoutTemplate(
  payload: WorkoutTemplatePayload,
): Promise<FitnessWorkoutTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessWorkoutTemplate>>(
    "/fitness/workout-templates",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}

export async function updateWorkoutTemplate(
  templateId: string,
  payload: {
    name?: string;
    notes?: string | null;
    planned_distance_miles?: number | null;
  },
): Promise<FitnessWorkoutTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessWorkoutTemplate>>(
    `/fitness/workout-templates/${templateId}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}

export async function replaceLiftingTemplateExercises(
  templateId: string,
  exercises: Array<{ exercise_id: string; display_order: number }>,
): Promise<FitnessWorkoutTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessWorkoutTemplate>>(
    `/fitness/workout-templates/${templateId}/lifting-exercises`,
    {
      method: "PUT",
      body: JSON.stringify({ exercises }),
    },
  );
  return response.data;
}

export async function archiveWorkoutTemplate(
  templateId: string,
): Promise<FitnessWorkoutTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessWorkoutTemplate>>(
    `/fitness/workout-templates/${templateId}/archive`,
    { method: "POST" },
  );
  return response.data;
}

export async function restoreWorkoutTemplate(
  templateId: string,
): Promise<FitnessWorkoutTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessWorkoutTemplate>>(
    `/fitness/workout-templates/${templateId}/restore`,
    { method: "POST" },
  );
  return response.data;
}

export async function listPlanTemplates(
  includeArchived = false,
): Promise<FitnessPlanTemplate[]> {
  const response = await apiRequest<FitnessResponse<FitnessPlanTemplate[]>>(
    `/fitness/plan-templates?${query({ include_archived: includeArchived })}`,
  );
  return response.data;
}

export async function getPlanTemplate(
  planTemplateId: string,
): Promise<FitnessPlanTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessPlanTemplate>>(
    `/fitness/plan-templates/${planTemplateId}`,
  );
  return response.data;
}

export async function createPlanTemplate(payload: {
  name: string;
  notes: string | null;
  items: FitnessPlanTemplateItem[];
}): Promise<FitnessPlanTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessPlanTemplate>>(
    "/fitness/plan-templates",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}

export async function updatePlanTemplate(
  planTemplateId: string,
  payload: { name?: string; notes?: string | null },
): Promise<FitnessPlanTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessPlanTemplate>>(
    `/fitness/plan-templates/${planTemplateId}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}

export async function replacePlanTemplateItems(
  planTemplateId: string,
  items: FitnessPlanTemplateItem[],
): Promise<FitnessPlanTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessPlanTemplate>>(
    `/fitness/plan-templates/${planTemplateId}/items`,
    {
      method: "PUT",
      body: JSON.stringify({ items }),
    },
  );
  return response.data;
}

export async function archivePlanTemplate(
  planTemplateId: string,
): Promise<FitnessPlanTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessPlanTemplate>>(
    `/fitness/plan-templates/${planTemplateId}/archive`,
    { method: "POST" },
  );
  return response.data;
}

export async function restorePlanTemplate(
  planTemplateId: string,
): Promise<FitnessPlanTemplate> {
  const response = await apiRequest<FitnessResponse<FitnessPlanTemplate>>(
    `/fitness/plan-templates/${planTemplateId}/restore`,
    { method: "POST" },
  );
  return response.data;
}

export async function instantiatePlanTemplate(
  planTemplateId: string,
  startDate: string,
): Promise<FitnessPlanInstance> {
  const response = await apiRequest<FitnessResponse<FitnessPlanInstance>>(
    `/fitness/plan-templates/${planTemplateId}/instances`,
    {
      method: "POST",
      body: JSON.stringify({ start_date: startDate }),
    },
  );
  return response.data;
}

export async function listPlanInstances(): Promise<FitnessPlanInstance[]> {
  const response = await apiRequest<FitnessResponse<FitnessPlanInstance[]>>(
    "/fitness/plan-instances",
  );
  return response.data;
}

export async function getCurrentPlanInstance(): Promise<FitnessPlanInstance | null> {
  const response = await apiRequest<FitnessResponse<FitnessPlanInstance | null>>(
    "/fitness/plan-instances/current",
  );
  return response.data;
}

export async function getPlanInstance(
  planInstanceId: string,
): Promise<FitnessPlanInstance> {
  const response = await apiRequest<FitnessResponse<FitnessPlanInstance>>(
    `/fitness/plan-instances/${planInstanceId}`,
  );
  return response.data;
}

export async function removeUnstartedPlanInstance(
  planInstanceId: string,
): Promise<{ removed_plan_instance_id: string; removed_count: number }> {
  const response = await apiRequest<
    FitnessResponse<{ removed_plan_instance_id: string; removed_count: number }>
  >(
    `/fitness/plan-instances/${planInstanceId}/remove-unstarted`,
    { method: "POST" },
  );
  return response.data;
}

export async function removeRemainingPlanWorkouts(
  planInstanceId: string,
  fromDate?: string,
): Promise<FitnessPlanInstance & { removed_count: number }> {
  const response = await apiRequest<FitnessResponse<FitnessPlanInstance & { removed_count: number }>>(
    `/fitness/plan-instances/${planInstanceId}/remove-remaining`,
    {
      method: "POST",
      body: JSON.stringify(fromDate ? { from_date: fromDate } : {}),
    },
  );
  return response.data;
}

export async function repeatPlanInstanceWeek(
  planInstanceId: string,
  payload: { week_start: string; idempotency_key?: string | null },
): Promise<FitnessPlanInstanceRepeatWeekResult> {
  const response = await apiRequest<FitnessResponse<FitnessPlanInstanceRepeatWeekResult>>(
    `/fitness/plan-instances/${planInstanceId}/repeat-week`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return response.data;
}
