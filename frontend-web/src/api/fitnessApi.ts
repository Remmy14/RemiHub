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
  created_at: string | null;
  updated_at: string | null;
};

export type FitnessScheduledWorkout = {
  id: string;
  user_id: string;
  workout_template_id: string;
  plan_instance_id: string | null;
  scheduled_date: string;
  original_scheduled_date: string;
  status: FitnessWorkoutStatus;
  replacement_scheduled_workout_id: string | null;
  planned_distance_miles: number | null;
  workout_name: string;
  type: FitnessWorkoutType;
  running_result: FitnessRunningResult | null;
  created_at: string | null;
  updated_at: string | null;
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
  scheduled_workout_ids?: string[];
  scheduled_workouts?: FitnessScheduledWorkout[];
  created_at: string | null;
  updated_at: string | null;
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
