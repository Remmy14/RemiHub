import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent, KeyboardEvent, MouseEvent, ReactNode } from "react";

import {
  archivePlanTemplate,
  archiveWorkoutTemplate,
  completeScheduledWorkout,
  createPlanTemplate,
  createRecurringSeries,
  createScheduledWorkout,
  createWorkoutTemplate,
  getTrainingCalendar,
  getPlanTemplate,
  getScheduledWorkout,
  getWorkoutTemplate,
  instantiatePlanTemplate,
  listPlanInstances,
  listPlanTemplates,
  listScheduledWorkouts,
  listTodayWorkouts,
  listWorkoutTemplates,
  previewRecurringSeries,
  replaceLiftingTemplateExercises,
  replacePlanTemplateItems,
  replaceScheduledWorkoutTemplate,
  removeRemainingPlanWorkouts,
  removeRemainingRecurringWorkouts,
  removeScheduledWorkout,
  removeUnstartedPlanInstance,
  rescheduleScheduledWorkout,
  restorePlanTemplate,
  restoreWorkoutTemplate,
  skipScheduledWorkout,
  undoRescheduleScheduledWorkout,
  updatePlanTemplate,
  updateWorkoutTemplate,
} from "./api/fitnessApi";
import type {
  FitnessPlanTemplate,
  FitnessPlanInstance,
  FitnessRecurringSeries,
  FitnessTrainingCalendar,
  FitnessPlanTemplateItem,
  FitnessScheduledWorkout,
  FitnessWorkoutTemplate,
  FitnessWorkoutType,
  RunningCompletionRequest,
} from "./api/fitnessApi";
import {
  listWeightliftingExercises,
} from "./api/weightliftingApi";
import type { WeightliftingExercise } from "./api/weightliftingApi";

type FitnessTab = "today" | "schedule" | "calendar" | "templates" | "plans" | "weightlifting";
type LoadState = "idle" | "loading" | "refreshing";

const tabs: Array<{ id: FitnessTab; label: string; href: string }> = [
  { id: "today", label: "Today", href: "/portal/fitness" },
  { id: "schedule", label: "Schedule", href: "/portal/fitness/schedule" },
  { id: "calendar", label: "Calendar", href: "/portal/fitness/calendar" },
  {
    id: "templates",
    label: "Workout Templates",
    href: "/portal/fitness/templates",
  },
  { id: "plans", label: "Training Plans", href: "/portal/fitness/plans" },
  { id: "weightlifting", label: "Weightlifting", href: "/portal/fitness/weightlifting" },
];

const statusStyles = {
  PLANNED: "border-blue-200 bg-blue-50 text-blue-700",
  COMPLETED: "border-emerald-200 bg-emerald-50 text-emerald-700",
  SKIPPED: "border-amber-200 bg-amber-50 text-amber-800",
  RESCHEDULED: "border-slate-300 bg-slate-100 text-slate-600",
};

const typeStyles = {
  RUNNING: "border-cyan-200 bg-cyan-50 text-cyan-800",
  LIFTING: "border-violet-200 bg-violet-50 text-violet-800",
};

const isoWeekdays = [
  { value: 1, label: "Mon" },
  { value: 2, label: "Tue" },
  { value: 3, label: "Wed" },
  { value: 4, label: "Thu" },
  { value: 5, label: "Fri" },
  { value: 6, label: "Sat" },
  { value: 7, label: "Sun" },
];

function localDateInputValue(date = new Date()): string {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function addDays(dateValue: string, days: number): string {
  const [year, month, day] = dateValue.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day + days));
  return date.toISOString().slice(0, 10);
}

function startOfIsoWeek(dateValue: string): string {
  const [year, month, day] = dateValue.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  const isoDay = date.getUTCDay() === 0 ? 7 : date.getUTCDay();
  date.setUTCDate(date.getUTCDate() - (isoDay - 1));
  return date.toISOString().slice(0, 10);
}

function newIdempotencyKey(): string {
  return window.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

function normalizedPlanItems(
  items: FitnessPlanTemplateItem[],
): Array<{
  workout_template_id: string;
  day_offset: number;
  display_order: number;
}> {
  return items
    .map((item, index) => ({
      workout_template_id: item.workout_template_id,
      day_offset: Number(item.day_offset),
      display_order: Number(item.display_order || index + 1),
    }))
    .sort(
      (left, right) =>
        left.day_offset - right.day_offset ||
        left.display_order - right.display_order ||
        left.workout_template_id.localeCompare(right.workout_template_id),
    );
}

function planItemsSignature(items: FitnessPlanTemplateItem[]): string {
  return JSON.stringify(normalizedPlanItems(items));
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "Unknown date";
  }
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) {
    return value;
  }
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "No duration";
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = seconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${remaining}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${remaining}s`;
  }
  return `${remaining}s`;
}

function distanceLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "No distance";
  }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} mi`;
}

function messageFromError(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function initialTabFromPath(path: string): FitnessTab {
  if (path.startsWith("/portal/fitness/schedule")) {
    return "schedule";
  }
  if (path.startsWith("/portal/fitness/calendar")) {
    return "calendar";
  }
  if (path.startsWith("/portal/fitness/templates")) {
    return "templates";
  }
  if (path.startsWith("/portal/fitness/plans")) {
    return "plans";
  }
  if (path.startsWith("/portal/fitness/weightlifting")) {
    return "weightlifting";
  }
  return "today";
}

function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white p-4 shadow-sm ${className}`}>
      {children}
    </section>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 px-4 py-6 text-sm font-semibold text-slate-600">
      {children}
    </div>
  );
}

function ErrorState({ message }: { message: string | null }) {
  if (!message) {
    return null;
  }
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm font-semibold text-red-700">
      {message}
    </div>
  );
}

function Pill({ children, className }: { children: ReactNode; className: string }) {
  return (
    <span className={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-black ${className}`}>
      {children}
    </span>
  );
}

function Field({
  children,
  label,
}: {
  children: ReactNode;
  label: string;
}) {
  return (
    <label className="block">
      <span className="text-xs font-bold uppercase text-slate-500">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

const inputClasses =
  "w-full rounded-md border border-slate-300 px-3 py-2 text-base text-slate-950 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100";
const buttonClasses =
  "rounded-md bg-slate-950 px-3 py-2 text-sm font-bold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400";
const secondaryButtonClasses =
  "rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400";

function WorkoutSummary({
  workout,
  onComplete,
  onOpen,
  onRemove,
  onReschedule,
  onSkip,
  onUndoReschedule,
  pendingAction,
}: {
  workout: FitnessScheduledWorkout;
  onComplete: (workout: FitnessScheduledWorkout) => void;
  onOpen?: (workout: FitnessScheduledWorkout) => void;
  onRemove: (workout: FitnessScheduledWorkout) => void;
  onReschedule: (workout: FitnessScheduledWorkout) => void;
  onSkip: (workout: FitnessScheduledWorkout) => void;
  onUndoReschedule: (workout: FitnessScheduledWorkout) => void;
  pendingAction: string | null;
}) {
  const isPlanned = workout.status === "PLANNED";
  const pending = pendingAction?.endsWith(workout.id) ?? false;
  const muted = workout.status !== "PLANNED";

  return (
    <article
      className={`rounded-lg border p-4 shadow-sm ${
        muted ? "border-slate-200 bg-slate-50" : "border-slate-200 bg-white"
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="break-words text-lg font-black text-slate-950">
            {workout.workout_name}
          </h3>
          <div className="mt-2 flex flex-wrap gap-2">
            <Pill className={typeStyles[workout.type]}>
              {workout.type === "RUNNING" ? "Running" : "Lifting"}
            </Pill>
            <Pill className={statusStyles[workout.status]}>
              {workout.status.charAt(0) + workout.status.slice(1).toLowerCase()}
            </Pill>
            {workout.source?.label && (
              <Pill className="border-slate-200 bg-slate-50 text-slate-600">
                {workout.source.label}
              </Pill>
            )}
          </div>
        </div>
        <div className="text-right text-sm font-semibold text-slate-600">
          {formatDate(workout.scheduled_date)}
        </div>
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Scheduled" value={formatDate(workout.scheduled_date)} />
        {workout.scheduled_date !== workout.original_scheduled_date && (
          <Metric label="Original" value={formatDate(workout.original_scheduled_date)} />
        )}
        {workout.type === "RUNNING" && (
          <Metric label="Planned" value={distanceLabel(workout.planned_distance_miles)} />
        )}
        {workout.running_result && (
          <>
            <Metric
              label="Completed"
              value={distanceLabel(workout.running_result.completed_distance_miles)}
            />
            <Metric
              label="Duration"
              value={formatDuration(workout.running_result.duration_seconds)}
            />
          </>
        )}
        {workout.type === "LIFTING" && (
          <Metric
            label="Entry"
            value="Use Android Weightlifting for sets, reps, and weights"
          />
        )}
      </div>

      {workout.running_result?.notes && (
        <p className="mt-3 rounded-md bg-slate-100 px-3 py-2 text-sm text-slate-700">
          {workout.running_result.notes}
        </p>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        {onOpen && (
          <button
            className={secondaryButtonClasses}
            onClick={() => onOpen(workout)}
            type="button"
          >
            Open
          </button>
        )}
        {isPlanned && (
          <>
            <button
              className={buttonClasses}
              disabled={pending}
              onClick={() => onComplete(workout)}
              type="button"
            >
              {workout.type === "RUNNING" ? "Complete" : "Mark complete"}
            </button>
            <button
              className={secondaryButtonClasses}
              disabled={pending}
              onClick={() => onSkip(workout)}
              type="button"
            >
              Skip
            </button>
            <button
              className={secondaryButtonClasses}
              disabled={pending}
              onClick={() => onReschedule(workout)}
              type="button"
            >
              Reschedule
            </button>
            <button
              className={secondaryButtonClasses}
              disabled={pending}
              onClick={() => onRemove(workout)}
              type="button"
            >
              Remove
            </button>
          </>
        )}
        {workout.status === "RESCHEDULED" && workout.replacement_scheduled_workout_id && (
          <button
            className={secondaryButtonClasses}
            disabled={pending}
            onClick={() => onUndoReschedule(workout)}
            type="button"
          >
            Undo reschedule
          </button>
        )}
      </div>
    </article>
  );
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-md bg-slate-50 px-3 py-2">
      <div className="text-xs font-bold uppercase text-slate-500">{label}</div>
      <div className="mt-1 break-words text-sm font-semibold text-slate-900">
        {value}
      </div>
    </div>
  );
}

function RunningCompletionDialog({
  onClose,
  onSubmit,
  submitting,
  workout,
}: {
  onClose: () => void;
  onSubmit: (payload: RunningCompletionRequest) => Promise<void>;
  submitting: boolean;
  workout: FitnessScheduledWorkout;
}) {
  const [completedDistance, setCompletedDistance] = useState(
    workout.planned_distance_miles?.toString() ?? "",
  );
  const [durationMinutes, setDurationMinutes] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    const distance = Number(completedDistance);
    const minutes = Number(durationMinutes);
    if (!Number.isFinite(distance) || distance < 0) {
      setError("Completed distance must be zero or more.");
      return;
    }
    if (!Number.isFinite(minutes) || minutes < 0) {
      setError("Duration must be zero minutes or more.");
      return;
    }
    try {
      await onSubmit({
        completed_distance_miles: distance,
        duration_seconds: Math.round(minutes * 60),
        notes: notes.trim() || null,
      });
    } catch (caught) {
      setError(messageFromError(caught, "Unable to complete workout."));
    }
  };

  return (
    <Dialog onClose={onClose} title={`Complete ${workout.workout_name}`}>
      <form className="space-y-4" onSubmit={submit}>
        <Metric label="Planned distance" value={distanceLabel(workout.planned_distance_miles)} />
        <Field label="Completed distance">
          <input
            className={inputClasses}
            disabled={submitting}
            min="0"
            onChange={(event) => setCompletedDistance(event.target.value)}
            required
            step="0.01"
            type="number"
            value={completedDistance}
          />
        </Field>
        <Field label="Duration minutes">
          <input
            className={inputClasses}
            disabled={submitting}
            min="0"
            onChange={(event) => setDurationMinutes(event.target.value)}
            required
            step="0.1"
            type="number"
            value={durationMinutes}
          />
        </Field>
        <Field label="Notes">
          <textarea
            className={`${inputClasses} min-h-24`}
            disabled={submitting}
            onChange={(event) => setNotes(event.target.value)}
            value={notes}
          />
        </Field>
        <ErrorState message={error} />
        <DialogActions onClose={onClose} submitting={submitting} submitLabel="Complete run" />
      </form>
    </Dialog>
  );
}

function RescheduleDialog({
  onClose,
  onSubmit,
  submitting,
  workout,
}: {
  onClose: () => void;
  onSubmit: (date: string) => Promise<void>;
  submitting: boolean;
  workout: FitnessScheduledWorkout;
}) {
  const [date, setDate] = useState(workout.scheduled_date);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await onSubmit(date);
    } catch (caught) {
      setError(messageFromError(caught, "Unable to reschedule workout."));
    }
  };

  return (
    <Dialog onClose={onClose} title={`Reschedule ${workout.workout_name}`}>
      <form className="space-y-4" onSubmit={submit}>
        <Field label="New scheduled date">
          <input
            className={inputClasses}
            disabled={submitting}
            onChange={(event) => setDate(event.target.value)}
            required
            type="date"
            value={date}
          />
        </Field>
        <p className="text-sm leading-6 text-slate-600">
          This changes only this scheduled workout. Other workouts from the same
          plan remain on their current dates.
        </p>
        <ErrorState message={error} />
        <DialogActions onClose={onClose} submitting={submitting} submitLabel="Reschedule" />
      </form>
    </Dialog>
  );
}

function WorkoutTemplateReplaceDialog({
  onClose,
  onSubmit,
  submitting,
  workout,
}: {
  onClose: () => void;
  onSubmit: (workoutTemplateId: string) => Promise<void>;
  submitting: boolean;
  workout: FitnessScheduledWorkout;
}) {
  const [templates, setTemplates] = useState<FitnessWorkoutTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    const load = async () => {
      setState("loading");
      setError(null);
      try {
        const templateList = (await listWorkoutTemplates(false)).filter(
          (template) => template.type === workout.type && template.id !== workout.workout_template_id,
        );
        if (!ignore) {
          setTemplates(templateList);
          setSelectedTemplateId("");
        }
      } catch (caught) {
        if (!ignore) {
          setError(messageFromError(caught, "Unable to load workout templates."));
        }
      } finally {
        if (!ignore) {
          setState("idle");
        }
      }
    };
    void load();
    return () => {
      ignore = true;
    };
  }, [workout.type, workout.workout_template_id]);

  const selectedTemplate = templates.find((template) => template.id === selectedTemplateId);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!selectedTemplateId) {
      setError("Choose a compatible template.");
      return;
    }
    try {
      await onSubmit(selectedTemplateId);
    } catch (caught) {
      setError(messageFromError(caught, "Unable to update workout template."));
    }
  };

  return (
    <Dialog onClose={onClose} title={`Edit ${workout.workout_name}`}>
      <form className="space-y-4" onSubmit={submit}>
        <div className="grid gap-2 sm:grid-cols-2">
          <Metric label="Scheduled" value={formatDate(workout.scheduled_date)} />
          <Metric label="Current template" value={workout.workout_name} />
          {workout.type === "RUNNING" && (
            <Metric label="Current planned" value={distanceLabel(workout.planned_distance_miles)} />
          )}
          {workout.source?.label && <Metric label="From" value={workout.source.label} />}
        </div>
        <Field label="Replacement template">
          <select
            className={inputClasses}
            disabled={submitting || state === "loading" || templates.length === 0}
            onChange={(event) => setSelectedTemplateId(event.target.value)}
            required
            value={selectedTemplateId}
          >
            <option value="">Choose replacement template...</option>
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
                {template.type === "RUNNING" ? ` (${distanceLabel(template.planned_distance_miles)})` : " (Lifting)"}
              </option>
            ))}
          </select>
        </Field>
        {selectedTemplate && selectedTemplate.type === "RUNNING" && (
          <Metric label="New planned" value={distanceLabel(selectedTemplate.planned_distance_miles)} />
        )}
        {templates.length === 0 && state !== "loading" && (
          <EmptyState>No compatible active templates are available.</EmptyState>
        )}
        <ErrorState message={error} />
        <DialogActions
          onClose={onClose}
          submitDisabled={state === "loading" || templates.length === 0 || !selectedTemplateId}
          submitting={submitting}
          submitLabel="Update workout"
        />
      </form>
    </Dialog>
  );
}

function Dialog({
  children,
  onClose,
  title,
}: {
  children: ReactNode;
  onClose: () => void;
  title: string;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-950/40 px-4 py-8">
      <section className="w-full max-w-xl rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <div className="mb-4 flex items-start justify-between gap-3">
          <h2 className="text-xl font-black text-slate-950">{title}</h2>
          <button
            aria-label="Close"
            className="rounded-md border border-slate-300 px-2 py-1 text-sm font-black text-slate-600 hover:bg-slate-50"
            onClick={onClose}
            type="button"
          >
            X
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

function DialogActions({
  onClose,
  submitDisabled = false,
  submitLabel,
  submitting,
}: {
  onClose: () => void;
  submitDisabled?: boolean;
  submitLabel: string;
  submitting: boolean;
}) {
  return (
    <div className="flex flex-wrap justify-end gap-2">
      <button
        className={secondaryButtonClasses}
        disabled={submitting}
        onClick={onClose}
        type="button"
      >
        Cancel
      </button>
      <button className={buttonClasses} disabled={submitting || submitDisabled} type="submit">
        {submitting ? "Saving..." : submitLabel}
      </button>
    </div>
  );
}

function TodayView({
  onComplete,
  onRemove,
  onReschedule,
  onSkip,
  onUndoReschedule,
  pendingAction,
  refreshToken,
}: {
  onComplete: (workout: FitnessScheduledWorkout) => void;
  onRemove: (workout: FitnessScheduledWorkout) => void;
  onReschedule: (workout: FitnessScheduledWorkout) => void;
  onSkip: (workout: FitnessScheduledWorkout) => void;
  onUndoReschedule: (workout: FitnessScheduledWorkout) => void;
  pendingAction: string | null;
  refreshToken: number;
}) {
  const [date, setDate] = useState(localDateInputValue());
  const [workouts, setWorkouts] = useState<FitnessScheduledWorkout[]>([]);
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === "idle" ? "loading" : "refreshing"));
    setError(null);
    try {
      setWorkouts(await listTodayWorkouts(date));
    } catch (caught) {
      setError(messageFromError(caught, "Unable to load today's workouts."));
    } finally {
      setState("idle");
    }
  }, [date, refreshToken]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-4">
      <Panel>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-2xl font-black text-slate-950">Today</h2>
            <p className="mt-1 text-sm text-slate-600">
              Scheduled workouts for {formatDate(date)}.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <input
              className={inputClasses}
              onChange={(event) => setDate(event.target.value)}
              type="date"
              value={date}
            />
            <button className={secondaryButtonClasses} onClick={() => void load()} type="button">
              Refresh
            </button>
          </div>
        </div>
      </Panel>
      <ErrorState message={error} />
      {state === "loading" && <EmptyState>Loading workouts...</EmptyState>}
      {state !== "loading" && workouts.length === 0 && (
        <EmptyState>No workouts scheduled today.</EmptyState>
      )}
      <div className="grid gap-3">
        {workouts.map((workout) => (
          <WorkoutSummary
            key={workout.id}
            onComplete={onComplete}
            onRemove={onRemove}
            onReschedule={onReschedule}
            onSkip={onSkip}
            onUndoReschedule={onUndoReschedule}
            pendingAction={pendingAction}
            workout={workout}
          />
        ))}
      </div>
      {state === "refreshing" && (
        <div className="text-sm font-semibold text-slate-500">Refreshing...</div>
      )}
    </div>
  );
}

function ScheduleView({
  onComplete,
  onRemove,
  onRemoveRemainingSeries,
  onReschedule,
  onSkip,
  onUndoReschedule,
  pendingAction,
  refreshToken,
}: {
  onComplete: (workout: FitnessScheduledWorkout) => void;
  onRemove: (workout: FitnessScheduledWorkout) => void;
  onRemoveRemainingSeries: (workout: FitnessScheduledWorkout) => void;
  onReschedule: (workout: FitnessScheduledWorkout) => void;
  onSkip: (workout: FitnessScheduledWorkout) => void;
  onUndoReschedule: (workout: FitnessScheduledWorkout) => void;
  pendingAction: string | null;
  refreshToken: number;
}) {
  const today = localDateInputValue();
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(addDays(today, 13));
  const [workouts, setWorkouts] = useState<FitnessScheduledWorkout[]>([]);
  const [templates, setTemplates] = useState<FitnessWorkoutTemplate[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const [scheduleDate, setScheduleDate] = useState(today);
  const [repeatWeekly, setRepeatWeekly] = useState(false);
  const [weekdays, setWeekdays] = useState<number[]>([1, 3, 5]);
  const [durationWeeks, setDurationWeeks] = useState(5);
  const [recurrenceKey, setRecurrenceKey] = useState(newIdempotencyKey);
  const [recurrencePreview, setRecurrencePreview] = useState<FitnessRecurringSeries | null>(null);
  const [recurrencePreviewSignature, setRecurrencePreviewSignature] = useState("");
  const [selectedWorkout, setSelectedWorkout] = useState<FitnessScheduledWorkout | null>(null);
  const [state, setState] = useState<LoadState>("idle");
  const [mutating, setMutating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === "idle" ? "loading" : "refreshing"));
    setError(null);
    try {
      const [scheduled, templateList] = await Promise.all([
        listScheduledWorkouts(startDate, endDate),
        listWorkoutTemplates(false),
      ]);
      setWorkouts(scheduled);
      setTemplates(templateList);
      setSelectedTemplate((current) => current || templateList[0]?.id || "");
    } catch (caught) {
      setError(messageFromError(caught, "Unable to load the schedule."));
    } finally {
      setState("idle");
    }
  }, [endDate, refreshToken, startDate]);

  useEffect(() => {
    void load();
  }, [load]);

  const grouped = useMemo(() => {
    const dates = new Map<string, FitnessScheduledWorkout[]>();
    workouts.forEach((workout) => {
      const list = dates.get(workout.scheduled_date) ?? [];
      list.push(workout);
      dates.set(workout.scheduled_date, list);
    });
    return [...dates.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [workouts]);

  const submitSchedule = async (event: FormEvent) => {
    event.preventDefault();
    if (!selectedTemplate) {
      setError("Choose a template before scheduling a workout.");
      return;
    }
    setMutating(true);
    setError(null);
    try {
      if (repeatWeekly) {
        const payload = {
          workout_template_id: selectedTemplate,
          start_date: scheduleDate,
          weekdays,
          duration_weeks: durationWeeks,
        };
        const signature = JSON.stringify(payload);
        if (!recurrencePreview || recurrencePreviewSignature !== signature || recurrencePreview.count === 0) {
          throw new Error("Preview this recurrence before creating it.");
        }
        await createRecurringSeries({ ...payload, idempotency_key: recurrenceKey });
        setRecurrencePreview(null);
        setRecurrencePreviewSignature("");
        setRecurrenceKey(newIdempotencyKey());
      } else {
        await createScheduledWorkout(selectedTemplate, scheduleDate);
      }
      await load();
    } catch (caught) {
      setError(messageFromError(caught, "Unable to schedule workout."));
    } finally {
      setMutating(false);
    }
  };

  const previewRecurrence = async () => {
    if (!selectedTemplate) {
      setError("Choose a template before previewing recurrence.");
      return;
    }
    setMutating(true);
    setError(null);
    try {
      const payload = {
        workout_template_id: selectedTemplate,
        start_date: scheduleDate,
        weekdays,
        duration_weeks: durationWeeks,
      };
      setRecurrencePreview(
        await previewRecurringSeries(payload),
      );
      setRecurrencePreviewSignature(JSON.stringify(payload));
    } catch (caught) {
      setError(messageFromError(caught, "Unable to preview recurrence."));
    } finally {
      setMutating(false);
    }
  };

  const toggleWeekday = (weekday: number) => {
    setWeekdays((current) => {
      const next = current.includes(weekday)
        ? current.filter((item) => item !== weekday)
        : [...current, weekday];
      return next.sort((left, right) => left - right);
    });
    setRecurrencePreview(null);
    setRecurrencePreviewSignature("");
    setRecurrenceKey(newIdempotencyKey());
  };

  const currentRecurrenceSignature = JSON.stringify({
    workout_template_id: selectedTemplate,
    start_date: scheduleDate,
    weekdays,
    duration_weeks: durationWeeks,
  });
  const recurrenceReady =
    !repeatWeekly ||
    (recurrencePreview !== null &&
      recurrencePreviewSignature === currentRecurrenceSignature &&
      recurrencePreview.count > 0);

  const openWorkout = async (workout: FitnessScheduledWorkout) => {
    setSelectedWorkout(workout);
    setError(null);
    try {
      setSelectedWorkout(await getScheduledWorkout(workout.id));
    } catch (caught) {
      setError(messageFromError(caught, "Unable to open workout."));
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="space-y-4">
        <Panel>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Start">
              <input className={inputClasses} onChange={(event) => setStartDate(event.target.value)} type="date" value={startDate} />
            </Field>
            <Field label="End">
              <input className={inputClasses} onChange={(event) => setEndDate(event.target.value)} type="date" value={endDate} />
            </Field>
            <div className="flex items-end">
              <button className={secondaryButtonClasses} onClick={() => void load()} type="button">
                Refresh
              </button>
            </div>
          </div>
        </Panel>
        <ErrorState message={error} />
        {state === "loading" && <EmptyState>Loading schedule...</EmptyState>}
        {state !== "loading" && grouped.length === 0 && (
          <EmptyState>No workouts found in this date range.</EmptyState>
        )}
        {grouped.map(([date, dayWorkouts]) => (
          <Panel key={date}>
            <h2 className="text-lg font-black text-slate-950">{formatDate(date)}</h2>
            <div className="mt-3 grid gap-3">
              {dayWorkouts.map((workout) => (
                <WorkoutSummary
                  key={workout.id}
                  onComplete={onComplete}
                  onOpen={(item) => void openWorkout(item)}
                  onRemove={onRemove}
                  onReschedule={onReschedule}
                  onSkip={onSkip}
                  onUndoReschedule={onUndoReschedule}
                  pendingAction={pendingAction}
                  workout={workout}
                />
              ))}
            </div>
          </Panel>
        ))}
      </div>
      <aside className="space-y-4">
        <Panel>
          <h2 className="text-lg font-black text-slate-950">Schedule from template</h2>
          <form className="mt-4 space-y-3" onSubmit={submitSchedule}>
            <Field label="Template">
              <select
                className={inputClasses}
                onChange={(event) => {
                  setSelectedTemplate(event.target.value);
                  setRecurrencePreview(null);
                  setRecurrencePreviewSignature("");
                  setRecurrenceKey(newIdempotencyKey());
                }}
                required
                value={selectedTemplate}
              >
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.name} ({template.type === "RUNNING" ? "Running" : "Lifting"})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Date">
              <input
                className={inputClasses}
                onChange={(event) => {
                  setScheduleDate(event.target.value);
                  setRecurrencePreview(null);
                  setRecurrencePreviewSignature("");
                  setRecurrenceKey(newIdempotencyKey());
                }}
                required
                type="date"
                value={scheduleDate}
              />
            </Field>
            <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
              <input
                checked={repeatWeekly}
                onChange={(event) => {
                  setRepeatWeekly(event.target.checked);
                  setRecurrencePreview(null);
                  setRecurrencePreviewSignature("");
                }}
                type="checkbox"
              />
              Repeat weekly
            </label>
            {repeatWeekly && (
              <div className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3">
                <Field label="Weekdays">
                  <div className="flex flex-wrap gap-2">
                    {isoWeekdays.map((weekday) => (
                      <label className="flex items-center gap-1 text-sm font-bold text-slate-700" key={weekday.value}>
                        <input
                          checked={weekdays.includes(weekday.value)}
                          onChange={() => toggleWeekday(weekday.value)}
                          type="checkbox"
                        />
                        {weekday.label}
                      </label>
                    ))}
                  </div>
                </Field>
                <Field label="Duration weeks">
                  <input
                    className={inputClasses}
                    min="1"
                    onChange={(event) => {
                      setDurationWeeks(Number(event.target.value));
                      setRecurrencePreview(null);
                      setRecurrencePreviewSignature("");
                      setRecurrenceKey(newIdempotencyKey());
                    }}
                    required
                    type="number"
                    value={durationWeeks}
                  />
                </Field>
                <button className={secondaryButtonClasses} disabled={mutating || weekdays.length === 0} onClick={() => void previewRecurrence()} type="button">
                  Preview recurrence
                </button>
                {recurrencePreview && (
                  <div className="rounded-md bg-white p-3 text-sm text-slate-700">
                    <div className="font-black text-slate-950">
                      {recurrencePreview.count} workouts will be scheduled
                    </div>
                    <ol className="mt-2 max-h-44 space-y-1 overflow-y-auto">
                      {(recurrencePreview.dates ?? []).map((date) => (
                        <li key={date}>{formatDate(date)}</li>
                      ))}
                    </ol>
                  </div>
                )}
              </div>
            )}
            <button className={buttonClasses} disabled={mutating || templates.length === 0 || !recurrenceReady} type="submit">
              {repeatWeekly ? "Create series" : "Schedule workout"}
            </button>
          </form>
        </Panel>
        <Panel>
          <h2 className="text-lg font-black text-slate-950">Selected workout</h2>
          {selectedWorkout ? (
            <div className="mt-3 space-y-2 text-sm text-slate-700">
              <div className="font-black text-slate-950">{selectedWorkout.workout_name}</div>
              <div>ID: {selectedWorkout.id}</div>
              <div>Template: {selectedWorkout.workout_template_id}</div>
              {selectedWorkout.source?.label && (
                <div>From: {selectedWorkout.source.label}</div>
              )}
              {selectedWorkout.plan_instance_id && (
                <div>Plan instance: {selectedWorkout.plan_instance_id}</div>
              )}
              {selectedWorkout.recurring_series_id && (
                <div>Recurring series: {selectedWorkout.recurring_series_id}</div>
              )}
              {selectedWorkout.replacement_scheduled_workout_id && (
                <div>Replacement: {selectedWorkout.replacement_scheduled_workout_id}</div>
              )}
              {selectedWorkout.recurring_series_id && (
                <button
                  className={secondaryButtonClasses}
                  onClick={() => onRemoveRemainingSeries(selectedWorkout)}
                  type="button"
                >
                  Remove remaining series workouts
                </button>
              )}
            </div>
          ) : (
            <p className="mt-2 text-sm text-slate-600">Open a workout to inspect its schedule linkage.</p>
          )}
        </Panel>
      </aside>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0 rounded border border-slate-200 bg-slate-50 px-2 py-1.5">
      <div className="truncate text-[0.65rem] font-black uppercase text-slate-500">{label}</div>
      <div className="mt-0.5 truncate text-xs font-black text-slate-950">{value}</div>
    </div>
  );
}

function WeeklyMiniSummary({ summary }: { summary: FitnessTrainingCalendar["weeks"][number]["summary"] }) {
  return (
    <div className="grid grid-cols-2 gap-1.5 border-l border-slate-200 p-2 text-xs text-slate-700">
      <MiniStat label="Run plan" value={distanceLabel(summary.planned_running_miles)} />
      <MiniStat label="Run actual" value={distanceLabel(summary.actual_running_miles)} />
      <MiniStat label="Long plan" value={distanceLabel(summary.longest_planned_run_miles)} />
      <MiniStat label="Long actual" value={distanceLabel(summary.longest_completed_run_miles)} />
      <MiniStat
        label="Plan delta"
        value={summary.planned_mileage_change === null ? "N/A" : distanceLabel(summary.planned_mileage_change)}
      />
      <MiniStat
        label="Actual delta"
        value={summary.actual_mileage_change === null ? "N/A" : distanceLabel(summary.actual_mileage_change)}
      />
      <MiniStat
        label="Long %"
        value={summary.planned_long_run_percentage === null ? "N/A" : `${summary.planned_long_run_percentage.toFixed(0)}%`}
      />
      <MiniStat label="Lifts" value={summary.completed_lifting_sessions} />
    </div>
  );
}

function CalendarWorkoutCard({
  expanded,
  onComplete,
  onEdit,
  onRemove,
  onReschedule,
  onSelect,
  onSkip,
  onUndoReschedule,
  overflowOpen,
  pending,
  setOverflowOpen,
  workout,
}: {
  expanded: boolean;
  onComplete: (workout: FitnessScheduledWorkout) => void;
  onEdit: (workout: FitnessScheduledWorkout) => void;
  onRemove: (workout: FitnessScheduledWorkout) => void;
  onReschedule: (workout: FitnessScheduledWorkout) => void;
  onSelect: (workout: FitnessScheduledWorkout) => void;
  onSkip: (workout: FitnessScheduledWorkout) => void;
  onUndoReschedule: (workout: FitnessScheduledWorkout) => void;
  overflowOpen: boolean;
  pending: boolean;
  setOverflowOpen: (open: boolean) => void;
  workout: FitnessScheduledWorkout;
}) {
  const stopAction = (event: KeyboardEvent | MouseEvent) => {
    event.stopPropagation();
  };
  const compactButtonClasses =
    "rounded border border-slate-300 bg-white px-2 py-1 text-[0.7rem] font-black text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400";

  return (
    <article
      aria-expanded={expanded}
      className={`w-full cursor-pointer rounded-md border p-2 text-left text-xs shadow-sm ${
        expanded ? "border-blue-400 bg-blue-50/40 ring-2 ring-blue-100" : "border-slate-200 bg-white"
      }`}
      onClick={() => onSelect(workout)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(workout);
        }
      }}
      role="button"
      tabIndex={0}
    >
      <div className="font-black text-slate-950">{workout.workout_name}</div>
      <div className="mt-1 flex flex-wrap gap-1">
        <Pill className={typeStyles[workout.type]}>{workout.type === "RUNNING" ? "Run" : "Lift"}</Pill>
        <Pill className={statusStyles[workout.status]}>{workout.status}</Pill>
      </div>
      {workout.type === "RUNNING" && (
        <div className="mt-1 font-semibold text-slate-600">{distanceLabel(workout.planned_distance_miles)}</div>
      )}
      {workout.source?.label && (
        <div className="mt-1 text-slate-500">{workout.source.label}</div>
      )}
      {expanded && (
        <div className="mt-2 flex flex-wrap gap-1" onClick={stopAction} onKeyDown={stopAction}>
          {workout.status === "PLANNED" && (
            <>
              <button className={compactButtonClasses} disabled={pending} onClick={() => onComplete(workout)} type="button">
                Complete
              </button>
              <button className={compactButtonClasses} disabled={pending} onClick={() => onEdit(workout)} type="button">
                Edit
              </button>
              <button className={compactButtonClasses} disabled={pending} onClick={() => onReschedule(workout)} type="button">
                Move
              </button>
              <div className="relative">
                <button
                  aria-label="More actions"
                  className={compactButtonClasses}
                  disabled={pending}
                  onClick={() => setOverflowOpen(!overflowOpen)}
                  type="button"
                >
                  ...
                </button>
                {overflowOpen && (
                  <div className="absolute right-0 z-10 mt-1 w-24 rounded-md border border-slate-200 bg-white p-1 shadow-lg">
                    <button
                      className="block w-full rounded px-2 py-1 text-left text-[0.7rem] font-bold text-slate-700 hover:bg-slate-50"
                      disabled={pending}
                      onClick={() => onSkip(workout)}
                      type="button"
                    >
                      Skip
                    </button>
                    <button
                      className="block w-full rounded px-2 py-1 text-left text-[0.7rem] font-bold text-red-700 hover:bg-red-50"
                      disabled={pending}
                      onClick={() => onRemove(workout)}
                      type="button"
                    >
                      Remove
                    </button>
                  </div>
                )}
              </div>
            </>
          )}
          {workout.status === "RESCHEDULED" && workout.replacement_scheduled_workout_id && (
            <button className={compactButtonClasses} disabled={pending} onClick={() => onUndoReschedule(workout)} type="button">
              Undo
            </button>
          )}
        </div>
      )}
    </article>
  );
}

function TrainingCalendarView({
  onComplete,
  onEdit,
  onRemove,
  onReschedule,
  onSkip,
  onUndoReschedule,
  pendingAction,
  refreshToken,
}: {
  onComplete: (workout: FitnessScheduledWorkout) => void;
  onEdit: (workout: FitnessScheduledWorkout) => void;
  onRemove: (workout: FitnessScheduledWorkout) => void;
  onReschedule: (workout: FitnessScheduledWorkout) => void;
  onSkip: (workout: FitnessScheduledWorkout) => void;
  onUndoReschedule: (workout: FitnessScheduledWorkout) => void;
  pendingAction: string | null;
  refreshToken: number;
}) {
  const today = localDateInputValue();
  const [startDate, setStartDate] = useState(today);
  const [weeks, setWeeks] = useState(8);
  const [calendar, setCalendar] = useState<FitnessTrainingCalendar | null>(null);
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [selectedWorkoutId, setSelectedWorkoutId] = useState<string | null>(null);
  const [overflowWorkoutId, setOverflowWorkoutId] = useState<string | null>(null);

  const normalizedStartDate = startOfIsoWeek(startDate);
  const endDate = addDays(normalizedStartDate, weeks * 7 - 1);

  const load = useCallback(async () => {
    setState((current) => (current === "idle" ? "loading" : "refreshing"));
    setError(null);
    try {
      setCalendar(await getTrainingCalendar(normalizedStartDate, endDate));
    } catch (caught) {
      setError(messageFromError(caught, "Unable to load training calendar."));
    } finally {
      setState("idle");
    }
  }, [endDate, normalizedStartDate, refreshToken]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setSelectedWorkoutId(null);
    setOverflowWorkoutId(null);
  }, [refreshToken, normalizedStartDate, weeks]);

  return (
    <div className="space-y-4">
      <Panel>
        <div className="grid gap-3 sm:grid-cols-[1fr_10rem_auto]">
          <Field label="Start">
            <input className={inputClasses} onChange={(event) => setStartDate(event.target.value)} type="date" value={startDate} />
          </Field>
          <Field label="Weeks">
            <select className={inputClasses} onChange={(event) => setWeeks(Number(event.target.value))} value={weeks}>
              {[5, 6, 8, 10, 12].map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </Field>
          <div className="flex items-end">
            <button className={secondaryButtonClasses} onClick={() => void load()} type="button">
              Refresh
            </button>
          </div>
        </div>
      </Panel>
      <ErrorState message={error} />
      {state === "loading" && <EmptyState>Loading calendar...</EmptyState>}
      {calendar && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
          <div className="min-w-[72rem]">
            <div className="grid grid-cols-[7rem_repeat(7,minmax(7rem,1fr))_13rem] border-b border-slate-200 bg-slate-50 text-xs font-black uppercase text-slate-500">
              <div className="p-3">Week</div>
              {isoWeekdays.map((day) => (
                <div className="border-l border-slate-200 p-3" key={day.value}>{day.label}</div>
              ))}
              <div className="border-l border-slate-200 p-3">Weekly</div>
            </div>
            {calendar.weeks.map((week) => (
              <div className="grid grid-cols-[7rem_repeat(7,minmax(7rem,1fr))_13rem] border-b border-slate-200 last:border-b-0" key={week.week_start}>
                <div className="p-3 text-sm font-black text-slate-900">{formatDate(week.week_start)}</div>
                {week.days.map((day) => (
                  <div className={`min-h-36 space-y-2 border-l border-slate-200 p-2 ${day.is_today ? "bg-blue-50" : ""}`} key={day.date}>
                    <div className="text-xs font-bold text-slate-500">{formatDate(day.date)}</div>
                    {day.workouts.map((workout) => {
                      const pending = pendingAction?.endsWith(workout.id) ?? false;
                      const expanded = selectedWorkoutId === workout.id;
                      return (
                        <CalendarWorkoutCard
                          expanded={expanded}
                          key={workout.id}
                          onComplete={onComplete}
                          onEdit={onEdit}
                          onRemove={onRemove}
                          onReschedule={onReschedule}
                          onSelect={(item) => {
                            setSelectedWorkoutId((current) => (current === item.id ? null : item.id));
                            setOverflowWorkoutId(null);
                          }}
                          onSkip={onSkip}
                          onUndoReschedule={onUndoReschedule}
                          overflowOpen={overflowWorkoutId === workout.id}
                          pending={pending}
                          setOverflowOpen={(open) => setOverflowWorkoutId(open ? workout.id : null)}
                          workout={workout}
                        />
                      );
                    })}
                  </div>
                ))}
                <WeeklyMiniSummary summary={week.summary} />
              </div>
            ))}
          </div>
        </div>
      )}
      {state === "refreshing" && (
        <div className="text-sm font-semibold text-slate-500">Refreshing...</div>
      )}
    </div>
  );
}

function ExercisePicker({
  exercises,
  onChange,
  selectedIds,
}: {
  exercises: WeightliftingExercise[];
  onChange: (ids: string[]) => void;
  selectedIds: string[];
}) {
  const addExercise = (exerciseId: string) => {
    if (exerciseId && !selectedIds.includes(exerciseId)) {
      onChange([...selectedIds, exerciseId]);
    }
  };
  const move = (index: number, direction: -1 | 1) => {
    const next = [...selectedIds];
    const target = index + direction;
    if (target < 0 || target >= next.length) {
      return;
    }
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };

  return (
    <div className="space-y-3">
      <select
        className={inputClasses}
        onChange={(event) => {
          addExercise(event.target.value);
          event.target.value = "";
        }}
        value=""
      >
        <option value="">Add exercise</option>
        {exercises
          .filter((exercise) => !selectedIds.includes(exercise.id))
          .map((exercise) => (
            <option key={exercise.id} value={exercise.id}>
              {exercise.name}
            </option>
          ))}
      </select>
      {selectedIds.length === 0 ? (
        <EmptyState>Select at least one existing Weightlifting exercise.</EmptyState>
      ) : (
        <ol className="space-y-2">
          {selectedIds.map((exerciseId, index) => {
            const exercise = exercises.find((item) => item.id === exerciseId);
            return (
              <li
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2"
                key={exerciseId}
              >
                <span className="text-sm font-bold text-slate-800">
                  {index + 1}. {exercise?.name ?? exerciseId}
                </span>
                <div className="flex gap-2">
                  <button className={secondaryButtonClasses} onClick={() => move(index, -1)} type="button">
                    Up
                  </button>
                  <button className={secondaryButtonClasses} onClick={() => move(index, 1)} type="button">
                    Down
                  </button>
                  <button
                    className={secondaryButtonClasses}
                    onClick={() => onChange(selectedIds.filter((id) => id !== exerciseId))}
                    type="button"
                  >
                    Remove
                  </button>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function TemplatesView() {
  const [templates, setTemplates] = useState<FitnessWorkoutTemplate[]>([]);
  const [exercises, setExercises] = useState<WeightliftingExercise[]>([]);
  const [selected, setSelected] = useState<FitnessWorkoutTemplate | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [type, setType] = useState<FitnessWorkoutType>("RUNNING");
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [plannedDistance, setPlannedDistance] = useState("");
  const [selectedExercises, setSelectedExercises] = useState<string[]>([]);
  const [state, setState] = useState<LoadState>("idle");
  const [mutating, setMutating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === "idle" ? "loading" : "refreshing"));
    setError(null);
    try {
      const [templateList, exerciseList] = await Promise.all([
        listWorkoutTemplates(includeArchived),
        listWeightliftingExercises(false),
      ]);
      setTemplates(templateList);
      setExercises(exerciseList);
    } catch (caught) {
      setError(messageFromError(caught, "Unable to load templates."));
    } finally {
      setState("idle");
    }
  }, [includeArchived]);

  useEffect(() => {
    void load();
  }, [load]);

  const editTemplate = async (template: FitnessWorkoutTemplate) => {
    setError(null);
    try {
      const detail = await getWorkoutTemplate(template.id);
      setSelected(detail);
      setType(detail.type);
      setName(detail.name);
      setNotes(detail.notes ?? "");
      setPlannedDistance(detail.planned_distance_miles?.toString() ?? "");
      setSelectedExercises(
        [...(detail.exercises ?? [])]
          .sort((left, right) => left.display_order - right.display_order)
          .map((exercise) => exercise.exercise_id),
      );
    } catch (caught) {
      setError(messageFromError(caught, "Unable to load template."));
    }
  };

  const resetForm = () => {
    setSelected(null);
    setType("RUNNING");
    setName("");
    setNotes("");
    setPlannedDistance("");
    setSelectedExercises([]);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setMutating(true);
    setError(null);
    try {
      if (type === "RUNNING" && plannedDistance.trim() === "") {
        throw new Error("Running templates require planned distance.");
      }
      if (type === "LIFTING" && selectedExercises.length === 0) {
        throw new Error("Lifting templates require at least one exercise.");
      }
      const exercisePayload = selectedExercises.map((exerciseId, index) => ({
        exercise_id: exerciseId,
        display_order: index + 1,
      }));
      if (selected) {
        const updated = await updateWorkoutTemplate(selected.id, {
          name: name.trim(),
          notes: notes.trim() || null,
          ...(type === "RUNNING"
            ? { planned_distance_miles: Number(plannedDistance) }
            : {}),
        });
        const detailed =
          type === "LIFTING"
            ? await replaceLiftingTemplateExercises(selected.id, exercisePayload)
            : updated;
        setSelected(detailed);
      } else {
        await createWorkoutTemplate({
          name: name.trim(),
          type,
          notes: notes.trim() || null,
          ...(type === "RUNNING"
            ? { planned_distance_miles: Number(plannedDistance) }
            : { exercises: exercisePayload }),
        });
        resetForm();
      }
      await load();
    } catch (caught) {
      setError(messageFromError(caught, "Unable to save template."));
    } finally {
      setMutating(false);
    }
  };

  const setTemplateActive = async (template: FitnessWorkoutTemplate, active: boolean) => {
    setMutating(true);
    setError(null);
    try {
      await (active ? restoreWorkoutTemplate(template.id) : archiveWorkoutTemplate(template.id));
      await load();
      if (selected?.id === template.id) {
        setSelected(await getWorkoutTemplate(template.id));
      }
    } catch (caught) {
      setError(messageFromError(caught, "Unable to update template status."));
    } finally {
      setMutating(false);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
      <div className="space-y-4">
        <Panel>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-2xl font-black text-slate-950">Workout Templates</h2>
            <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
              <input
                checked={includeArchived}
                onChange={(event) => setIncludeArchived(event.target.checked)}
                type="checkbox"
              />
              Include archived
            </label>
          </div>
        </Panel>
        <ErrorState message={error} />
        {state === "loading" && <EmptyState>Loading templates...</EmptyState>}
        {state !== "loading" && templates.length === 0 && (
          <EmptyState>No workout templates yet.</EmptyState>
        )}
        <div className="grid gap-3 md:grid-cols-2">
          {templates.map((template) => (
            <article className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm" key={template.id}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <h3 className="break-words text-lg font-black text-slate-950">{template.name}</h3>
                <Pill className={typeStyles[template.type]}>
                  {template.type === "RUNNING" ? "Running" : "Lifting"}
                </Pill>
              </div>
              <div className="mt-3 flex flex-wrap gap-2 text-sm text-slate-600">
                <span>{template.active ? "Active" : "Archived"}</span>
                {template.type === "RUNNING" && (
                  <span>{distanceLabel(template.planned_distance_miles)}</span>
                )}
              </div>
              {template.notes && <p className="mt-3 text-sm leading-6 text-slate-600">{template.notes}</p>}
              <div className="mt-4 flex flex-wrap gap-2">
                <button className={secondaryButtonClasses} onClick={() => void editTemplate(template)} type="button">
                  Edit
                </button>
                <button
                  className={secondaryButtonClasses}
                  disabled={mutating}
                  onClick={() => void setTemplateActive(template, !template.active)}
                  type="button"
                >
                  {template.active ? "Archive" : "Restore"}
                </button>
              </div>
            </article>
          ))}
        </div>
      </div>
      <Panel>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-black text-slate-950">
            {selected ? "Update Template" : "Create Template"}
          </h2>
          <button className={secondaryButtonClasses} onClick={resetForm} type="button">
            New Template
          </button>
        </div>
        {selected && (
          <p className="mt-2 text-sm font-semibold text-slate-600">
            Editing {selected.name}
          </p>
        )}
        <form className="mt-4 space-y-4" onSubmit={submit}>
          <Field label="Type">
            <select
              className={inputClasses}
              disabled={Boolean(selected)}
              onChange={(event) => setType(event.target.value as FitnessWorkoutType)}
              value={type}
            >
              <option value="RUNNING">Running</option>
              <option value="LIFTING">Lifting</option>
            </select>
          </Field>
          <Field label="Name">
            <input className={inputClasses} onChange={(event) => setName(event.target.value)} required value={name} />
          </Field>
          <Field label="Notes">
            <textarea className={`${inputClasses} min-h-20`} onChange={(event) => setNotes(event.target.value)} value={notes} />
          </Field>
          {type === "RUNNING" ? (
            <Field label="Planned distance">
              <input
                className={inputClasses}
                min="0"
                onChange={(event) => setPlannedDistance(event.target.value)}
                required
                step="0.01"
                type="number"
                value={plannedDistance}
              />
            </Field>
          ) : (
            <Field label="Existing Weightlifting exercises">
              <ExercisePicker
                exercises={exercises}
                onChange={setSelectedExercises}
                selectedIds={selectedExercises}
              />
            </Field>
          )}
          <button className={buttonClasses} disabled={mutating} type="submit">
            {mutating ? "Saving..." : selected ? "Update Template" : "Create Template"}
          </button>
        </form>
      </Panel>
    </div>
  );
}

function PlanItemEditor({
  items,
  onChange,
  templates,
}: {
  items: FitnessPlanTemplateItem[];
  onChange: (items: FitnessPlanTemplateItem[]) => void;
  templates: FitnessWorkoutTemplate[];
}) {
  const addItem = () => {
    const template = templates[0];
    if (!template) {
      return;
    }
    onChange([
      ...items,
      {
        workout_template_id: template.id,
        day_offset: 0,
        display_order: items.length + 1,
      },
    ]);
  };
  const updateItem = (
    index: number,
    updates: Partial<FitnessPlanTemplateItem>,
  ) => {
    onChange(
      items.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...updates } : item,
      ),
    );
  };

  return (
    <div className="space-y-3">
      {items.length === 0 && <EmptyState>No workouts in this plan yet.</EmptyState>}
      {items.map((item, index) => (
        <div className="grid gap-2 rounded-md border border-slate-200 bg-slate-50 p-3 sm:grid-cols-[1fr_6rem_6rem_auto]" key={`${item.workout_template_id}-${index}`}>
          <select
            className={inputClasses}
            onChange={(event) => updateItem(index, { workout_template_id: event.target.value })}
            value={item.workout_template_id}
          >
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </select>
          <input
            aria-label="Day offset"
            className={inputClasses}
            min="0"
            onChange={(event) => updateItem(index, { day_offset: Number(event.target.value) })}
            type="number"
            value={item.day_offset}
          />
          <input
            aria-label="Display order"
            className={inputClasses}
            min="0"
            onChange={(event) => updateItem(index, { display_order: Number(event.target.value) })}
            type="number"
            value={item.display_order}
          />
          <button
            className={secondaryButtonClasses}
            onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}
            type="button"
          >
            Remove
          </button>
        </div>
      ))}
      <button className={secondaryButtonClasses} disabled={templates.length === 0} onClick={addItem} type="button">
        Add workout
      </button>
    </div>
  );
}

function PlansView() {
  const [plans, setPlans] = useState<FitnessPlanTemplate[]>([]);
  const [instances, setInstances] = useState<FitnessPlanInstance[]>([]);
  const [templates, setTemplates] = useState<FitnessWorkoutTemplate[]>([]);
  const [selected, setSelected] = useState<FitnessPlanTemplate | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [items, setItems] = useState<FitnessPlanTemplateItem[]>([]);
  const [startDate, setStartDate] = useState(localDateInputValue());
  const [state, setState] = useState<LoadState>("idle");
  const [mutating, setMutating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === "idle" ? "loading" : "refreshing"));
    setError(null);
    try {
      const [planList, templateList] = await Promise.all([
        listPlanTemplates(includeArchived),
        listWorkoutTemplates(false),
      ]);
      const instanceList = await listPlanInstances();
      setPlans(planList);
      setInstances(instanceList);
      setTemplates(templateList);
    } catch (caught) {
      setError(messageFromError(caught, "Unable to load training plans."));
    } finally {
      setState("idle");
    }
  }, [includeArchived]);

  useEffect(() => {
    void load();
  }, [load]);

  const editPlan = async (plan: FitnessPlanTemplate) => {
    setError(null);
    setSuccess(null);
    try {
      const detail = await getPlanTemplate(plan.id);
      setSelected(detail);
      setName(detail.name);
      setNotes(detail.notes ?? "");
      setItems(detail.items ?? []);
    } catch (caught) {
      setError(messageFromError(caught, "Unable to load plan."));
    }
  };

  const resetForm = () => {
    setSelected(null);
    setName("");
    setNotes("");
    setItems([]);
    setSuccess(null);
  };

  const normalizedItems = normalizedPlanItems(items);
  const persistedItems = selected ? normalizedPlanItems(selected.items ?? []) : [];
  const hasUnsavedChanges =
    !selected ||
    name.trim() !== selected.name ||
    (notes.trim() || null) !== (selected.notes ?? null) ||
    planItemsSignature(items) !== planItemsSignature(selected.items ?? []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setMutating(true);
    setError(null);
    setSuccess(null);
    try {
      let savedPlan: FitnessPlanTemplate;
      if (selected) {
        await updatePlanTemplate(selected.id, {
          name: name.trim(),
          notes: notes.trim() || null,
        });
        savedPlan = await replacePlanTemplateItems(selected.id, normalizedItems);
      } else {
        savedPlan = await createPlanTemplate({
          name: name.trim(),
          notes: notes.trim() || null,
          items: normalizedItems,
        });
      }
      setSelected(savedPlan);
      setName(savedPlan.name);
      setNotes(savedPlan.notes ?? "");
      setItems(savedPlan.items ?? []);
      await load();
      setSuccess("Training plan saved.");
    } catch (caught) {
      setError(messageFromError(caught, "Unable to save training plan."));
    } finally {
      setMutating(false);
    }
  };

  const setPlanActive = async (plan: FitnessPlanTemplate, active: boolean) => {
    setMutating(true);
    setError(null);
    setSuccess(null);
    try {
      await (active ? restorePlanTemplate(plan.id) : archivePlanTemplate(plan.id));
      await load();
      if (selected?.id === plan.id) {
        const detail = await getPlanTemplate(plan.id);
        setSelected(detail);
        setName(detail.name);
        setNotes(detail.notes ?? "");
        setItems(detail.items ?? []);
      }
    } catch (caught) {
      setError(messageFromError(caught, "Unable to update plan status."));
    } finally {
      setMutating(false);
    }
  };

  const instantiate = async () => {
    if (!selected) {
      setError("Select or save a training plan before instantiating it.");
      return;
    }
    if (hasUnsavedChanges) {
      setError("Save plan changes before instantiating.");
      return;
    }
    setMutating(true);
    setError(null);
    setSuccess(null);
    try {
      const instance = await instantiatePlanTemplate(selected.id, startDate);
      await load();
      setSuccess(
        `Instantiated ${selected.name} from ${formatDate(startDate)} with ${instance.scheduled_workout_ids?.length ?? 0} scheduled workouts.`,
      );
    } catch (caught) {
      setError(messageFromError(caught, "Unable to instantiate training plan."));
    } finally {
      setMutating(false);
    }
  };

  const cleanupInstance = async (instance: FitnessPlanInstance, mode: "unstarted" | "remaining") => {
    const message =
      mode === "unstarted"
        ? `Remove unstarted ${instance.plan_template_name} instance and its scheduled workouts?`
        : `Remove remaining planned workouts from ${instance.plan_template_name}?`;
    if (!window.confirm(message)) {
      return;
    }
    setMutating(true);
    setError(null);
    setSuccess(null);
    try {
      const result =
        mode === "unstarted"
          ? await removeUnstartedPlanInstance(instance.id)
          : await removeRemainingPlanWorkouts(instance.id, localDateInputValue());
      await load();
      setSuccess(`Removed ${result.removed_count} scheduled workouts.`);
    } catch (caught) {
      setError(messageFromError(caught, "Unable to clean up plan instance."));
    } finally {
      setMutating(false);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_26rem]">
      <div className="space-y-4">
        <Panel>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-2xl font-black text-slate-950">Training Plans</h2>
            <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
              <input
                checked={includeArchived}
                onChange={(event) => setIncludeArchived(event.target.checked)}
                type="checkbox"
              />
              Include archived
            </label>
          </div>
        </Panel>
        <ErrorState message={error} />
        {success && (
          <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm font-semibold text-emerald-700">
            {success}
          </div>
        )}
        {state === "loading" && <EmptyState>Loading training plans...</EmptyState>}
        {state !== "loading" && plans.length === 0 && (
          <EmptyState>No training plans yet.</EmptyState>
        )}
        <div className="grid gap-3 md:grid-cols-2">
          {plans.map((plan) => (
            <article className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm" key={plan.id}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <h3 className="break-words text-lg font-black text-slate-950">{plan.name}</h3>
                <Pill className={plan.active ? statusStyles.PLANNED : statusStyles.RESCHEDULED}>
                  {plan.active ? "Active" : "Archived"}
                </Pill>
              </div>
              {plan.notes && <p className="mt-3 text-sm leading-6 text-slate-600">{plan.notes}</p>}
              <div className="mt-4 flex flex-wrap gap-2">
                <button className={secondaryButtonClasses} onClick={() => void editPlan(plan)} type="button">
                  Edit
                </button>
                <button
                  className={secondaryButtonClasses}
                  disabled={mutating}
                  onClick={() => void setPlanActive(plan, !plan.active)}
                  type="button"
                >
                  {plan.active ? "Archive" : "Restore"}
                </button>
              </div>
            </article>
          ))}
        </div>
        <Panel>
          <h2 className="text-lg font-black text-slate-950">Plan instances</h2>
          <div className="mt-3 grid gap-3">
            {instances.length === 0 && <EmptyState>No plan instances yet.</EmptyState>}
            {instances.map((instance) => (
              <article className="rounded-md border border-slate-200 bg-slate-50 p-3" key={instance.id}>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-black text-slate-950">{instance.plan_template_name}</div>
                    <div className="text-sm text-slate-600">Started {formatDate(instance.start_date)}</div>
                  </div>
                  <Pill className={instance.status === "ACTIVE" && instance.planning_status !== "STOPPED" ? statusStyles.PLANNED : statusStyles.RESCHEDULED}>
                    {instance.status}{instance.planning_status === "STOPPED" ? " / STOPPED" : ""}
                  </Pill>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button className={secondaryButtonClasses} disabled={mutating} onClick={() => void cleanupInstance(instance, "unstarted")} type="button">
                    Remove unstarted
                  </button>
                  <button className={secondaryButtonClasses} disabled={mutating} onClick={() => void cleanupInstance(instance, "remaining")} type="button">
                    Remove remaining
                  </button>
                </div>
              </article>
            ))}
          </div>
        </Panel>
      </div>
      <Panel>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-black text-slate-950">
            {selected ? "Edit plan" : "Create plan"}
          </h2>
          <button className={secondaryButtonClasses} onClick={resetForm} type="button">
            New
          </button>
        </div>
        <form className="mt-4 space-y-4" onSubmit={submit}>
          <Field label="Name">
            <input className={inputClasses} onChange={(event) => setName(event.target.value)} required value={name} />
          </Field>
          <Field label="Notes">
            <textarea className={`${inputClasses} min-h-20`} onChange={(event) => setNotes(event.target.value)} value={notes} />
          </Field>
          <Field label="Workouts by day offset">
            <PlanItemEditor items={items} onChange={setItems} templates={templates} />
          </Field>
          {hasUnsavedChanges && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm font-semibold text-amber-800">
              Save plan changes before instantiating.
            </div>
          )}
          {!hasUnsavedChanges && selected && persistedItems.length > 0 && (
            <div className="rounded-md bg-slate-50 p-3">
              <div className="text-xs font-bold uppercase text-slate-500">Instantiation preview</div>
              <ol className="mt-2 space-y-1 text-sm text-slate-700">
                {persistedItems.map((item, index) => {
                  const template = templates.find((templateItem) => templateItem.id === item.workout_template_id);
                  return (
                    <li key={`${item.workout_template_id}-${index}`}>
                      Day {item.day_offset}: {template?.name ?? item.workout_template_id} on {formatDate(addDays(startDate, item.day_offset))}
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
          {!hasUnsavedChanges && selected && persistedItems.length === 0 && (
            <EmptyState>This saved plan has no workouts to preview.</EmptyState>
          )}
          <button className={buttonClasses} disabled={mutating} type="submit">
            {mutating ? "Saving..." : "Save plan"}
          </button>
        </form>
        <div className="mt-5 border-t border-slate-200 pt-4">
          <h3 className="text-base font-black text-slate-950">Instantiate plan</h3>
          <Field label="Start date">
            <input className={inputClasses} onChange={(event) => setStartDate(event.target.value)} type="date" value={startDate} />
          </Field>
          <p className="mt-3 text-sm leading-6 text-slate-600">
            Instantiation schedules workouts from the selected start date. After
            creation, each scheduled workout can be managed independently.
          </p>
          <button className={`${buttonClasses} mt-3`} disabled={mutating || hasUnsavedChanges} onClick={() => void instantiate()} type="button">
            Instantiate from start date
          </button>
        </div>
      </Panel>
    </div>
  );
}

function WeightliftingBoundaryView() {
  return (
    <Panel>
      <h2 className="text-2xl font-black text-slate-950">Weightlifting</h2>
      <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-600">
        Fitness manages scheduled lifting workouts and reusable lifting
        templates with existing Weightlifting exercises. Detailed exercise
        entry, sets, reps, weights, recommendations, and lifting history remain
        in the Android-native Weightlifting experience.
      </p>
      <div className="mt-5 grid gap-3 md:grid-cols-3">
        <Metric label="Fitness role" value="Schedule and template coordination" />
        <Metric label="Lifting templates" value="Use existing Weightlifting exercises" />
        <Metric label="Detailed entry" value="Android Weightlifting" />
      </div>
    </Panel>
  );
}

function FitnessScreen() {
  const [activeTab, setActiveTab] = useState<FitnessTab>(
    initialTabFromPath(window.location.pathname),
  );
  const [completeWorkout, setCompleteWorkout] = useState<FitnessScheduledWorkout | null>(null);
  const [rescheduleWorkout, setRescheduleWorkout] = useState<FitnessScheduledWorkout | null>(null);
  const [editWorkout, setEditWorkout] = useState<FitnessScheduledWorkout | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = () => setRefreshKey((value) => value + 1);

  useEffect(() => {
    const handlePopState = () => {
      setActiveTab(initialTabFromPath(window.location.pathname));
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const completeWorkoutAction = async (
    workout: FitnessScheduledWorkout,
    running?: RunningCompletionRequest,
    showRootError = true,
  ) => {
    setPendingAction(`complete:${workout.id}`);
    setMutationError(null);
    try {
      await completeScheduledWorkout(workout.id, running);
      setCompleteWorkout(null);
      refresh();
    } catch (caught) {
      const message = messageFromError(caught, "Unable to complete workout.");
      if (showRootError) {
        setMutationError(message);
      }
      throw new Error(message);
    } finally {
      setPendingAction(null);
    }
  };

  const skipWorkoutAction = async (workout: FitnessScheduledWorkout) => {
    setPendingAction(`skip:${workout.id}`);
    setMutationError(null);
    try {
      await skipScheduledWorkout(workout.id);
      refresh();
    } catch (caught) {
      setMutationError(messageFromError(caught, "Unable to skip workout."));
    } finally {
      setPendingAction(null);
    }
  };

  const rescheduleWorkoutAction = async (
    workout: FitnessScheduledWorkout,
    date: string,
    showRootError = true,
  ) => {
    setPendingAction(`reschedule:${workout.id}`);
    setMutationError(null);
    try {
      await rescheduleScheduledWorkout(workout.id, date);
      setRescheduleWorkout(null);
      refresh();
    } catch (caught) {
      const message = messageFromError(caught, "Unable to reschedule workout.");
      if (showRootError) {
        setMutationError(message);
      }
      throw new Error(message);
    } finally {
      setPendingAction(null);
    }
  };

  const replaceWorkoutTemplateAction = async (
    workout: FitnessScheduledWorkout,
    workoutTemplateId: string,
    showRootError = true,
  ) => {
    setPendingAction(`edit:${workout.id}`);
    setMutationError(null);
    try {
      await replaceScheduledWorkoutTemplate(workout.id, workoutTemplateId);
      setEditWorkout(null);
      refresh();
    } catch (caught) {
      const message = messageFromError(caught, "Unable to update workout template.");
      if (showRootError) {
        setMutationError(message);
      }
      throw new Error(message);
    } finally {
      setPendingAction(null);
    }
  };

  const removeWorkoutAction = async (workout: FitnessScheduledWorkout) => {
    if (!window.confirm(`Remove ${workout.workout_name} on ${formatDate(workout.scheduled_date)}?`)) {
      return;
    }
    setPendingAction(`remove:${workout.id}`);
    setMutationError(null);
    try {
      await removeScheduledWorkout(workout.id);
      refresh();
    } catch (caught) {
      setMutationError(messageFromError(caught, "Unable to remove workout."));
    } finally {
      setPendingAction(null);
    }
  };

  const undoRescheduleAction = async (workout: FitnessScheduledWorkout) => {
    setPendingAction(`undo:${workout.id}`);
    setMutationError(null);
    try {
      await undoRescheduleScheduledWorkout(workout.id);
      refresh();
    } catch (caught) {
      setMutationError(messageFromError(caught, "Unable to undo reschedule."));
    } finally {
      setPendingAction(null);
    }
  };

  const removeRemainingSeriesAction = async (workout: FitnessScheduledWorkout) => {
    if (!workout.recurring_series_id) {
      return;
    }
    if (!window.confirm(`Remove remaining planned workouts from ${workout.workout_name} series?`)) {
      return;
    }
    setPendingAction(`series:${workout.id}`);
    setMutationError(null);
    try {
      await removeRemainingRecurringWorkouts(workout.recurring_series_id, workout.scheduled_date);
      refresh();
    } catch (caught) {
      setMutationError(messageFromError(caught, "Unable to remove remaining series workouts."));
    } finally {
      setPendingAction(null);
    }
  };

  const requestComplete = (workout: FitnessScheduledWorkout) => {
    if (workout.type === "RUNNING") {
      setCompleteWorkout(workout);
      return;
    }
    completeWorkoutAction(workout).catch(() => undefined);
  };

  return (
    <main className="mx-auto max-w-7xl px-4 py-6">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl font-black text-slate-950">Fitness</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
            Coordinate scheduled workouts, running results, reusable templates,
            and training plans.
          </p>
        </div>
      </div>

      <nav aria-label="Fitness sections" className="mb-5 flex flex-wrap gap-2">
        {tabs.map((tab) => (
          <a
            className={`rounded-md px-3 py-2 text-sm font-black ${
              activeTab === tab.id
                ? "bg-slate-950 text-white"
                : "bg-white text-slate-700 hover:bg-slate-50"
            }`}
            href={tab.href}
            key={tab.id}
            onClick={(event) => {
              event.preventDefault();
              window.history.pushState(null, "", tab.href);
              setActiveTab(tab.id);
            }}
          >
            {tab.label}
          </a>
        ))}
      </nav>

      <ErrorState message={mutationError} />
      <div className="mt-4">
        {activeTab === "today" && (
          <TodayView
            onComplete={requestComplete}
            onRemove={(workout) => void removeWorkoutAction(workout)}
            onReschedule={setRescheduleWorkout}
            onSkip={(workout) => void skipWorkoutAction(workout)}
            onUndoReschedule={(workout) => void undoRescheduleAction(workout)}
            pendingAction={pendingAction}
            refreshToken={refreshKey}
          />
        )}
        {activeTab === "schedule" && (
          <ScheduleView
            onComplete={requestComplete}
            onRemove={(workout) => void removeWorkoutAction(workout)}
            onRemoveRemainingSeries={(workout) => void removeRemainingSeriesAction(workout)}
            onReschedule={setRescheduleWorkout}
            onSkip={(workout) => void skipWorkoutAction(workout)}
            onUndoReschedule={(workout) => void undoRescheduleAction(workout)}
            pendingAction={pendingAction}
            refreshToken={refreshKey}
          />
        )}
        {activeTab === "calendar" && (
          <TrainingCalendarView
            onComplete={requestComplete}
            onEdit={setEditWorkout}
            onRemove={(workout) => void removeWorkoutAction(workout)}
            onReschedule={setRescheduleWorkout}
            onSkip={(workout) => void skipWorkoutAction(workout)}
            onUndoReschedule={(workout) => void undoRescheduleAction(workout)}
            pendingAction={pendingAction}
            refreshToken={refreshKey}
          />
        )}
        {activeTab === "templates" && <TemplatesView />}
        {activeTab === "plans" && <PlansView />}
        {activeTab === "weightlifting" && <WeightliftingBoundaryView />}
      </div>

      {completeWorkout && (
        <RunningCompletionDialog
          onClose={() => setCompleteWorkout(null)}
          onSubmit={(payload) => completeWorkoutAction(completeWorkout, payload, false)}
          submitting={pendingAction === `complete:${completeWorkout.id}`}
          workout={completeWorkout}
        />
      )}
      {rescheduleWorkout && (
        <RescheduleDialog
          onClose={() => setRescheduleWorkout(null)}
          onSubmit={(date) => rescheduleWorkoutAction(rescheduleWorkout, date, false)}
          submitting={pendingAction === `reschedule:${rescheduleWorkout.id}`}
          workout={rescheduleWorkout}
        />
      )}
      {editWorkout && (
        <WorkoutTemplateReplaceDialog
          onClose={() => setEditWorkout(null)}
          onSubmit={(workoutTemplateId) => replaceWorkoutTemplateAction(editWorkout, workoutTemplateId, false)}
          submitting={pendingAction === `edit:${editWorkout.id}`}
          workout={editWorkout}
        />
      )}
    </main>
  );
}

export default FitnessScreen;
