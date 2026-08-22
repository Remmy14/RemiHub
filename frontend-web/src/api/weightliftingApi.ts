import { apiRequest } from "./authenticatedApi";

type WeightliftingResponse<T> = {
  success: true;
  data: T;
};

export type WeightliftingExercise = {
  id: string;
  name: string;
  display_order: number;
  active: boolean;
  notes: string | null;
  target_reps: number;
  target_sets: number | null;
  weight_increment: number;
  weight_unit: "lb" | "kg" | string;
};

export async function listWeightliftingExercises(
  includeArchived = false,
): Promise<WeightliftingExercise[]> {
  const params = new URLSearchParams({
    include_archived: String(includeArchived),
  });
  const response = await apiRequest<WeightliftingResponse<WeightliftingExercise[]>>(
    `/weightlifting/exercises?${params.toString()}`,
  );
  return response.data;
}
