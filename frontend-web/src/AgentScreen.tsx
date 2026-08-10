import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import {
  addAgentFollowUp,
  AgentActions,
  createAgentCard,
  getAgentCard,
  listAgentCards,
  performAgentAction,
} from "./api/agentApi";
import type {
  AgentAction,
  AgentApproval,
  AgentCardDetail,
  AgentCardSummary,
  AgentDeploymentRecovery,
  AgentEvent,
  AgentLatestRun,
  AgentMessage,
  AgentRun,
} from "./api/agentApi";

const POLL_INTERVAL_MILLIS = 5000;

const activeStatuses = new Set([
  "planning_queued",
  "planning",
  "implementation_queued",
  "implementing",
  "deployment_queued",
  "deploying",
]);

const terminalStatuses = new Set([
  "completed",
  "failed",
  "cancelled",
  "closed",
]);

function humanize(value: string | null | undefined): string {
  if (!value?.trim()) {
    return "Unknown";
  }

  return value
    .trim()
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

function scopeLabel(value: string): string {
  switch (value) {
    case "backend":
      return "Backend";
    case "android":
      return "Android";
    case "backend_and_android":
      return "Backend + Android";
    case "auto":
      return "Automatic";
    default:
      return humanize(value);
  }
}

function actionLabel(action: AgentAction): string {
  switch (action) {
    case AgentActions.ADD_FOLLOW_UP:
      return "Send Follow-up";
    case AgentActions.APPROVE_IMPLEMENTATION:
      return "Approve Implementation";
    case AgentActions.APPROVE_DEPLOYMENT:
      return "Approve Deployment";
    case AgentActions.RETRY:
      return "Retry Failed Run";
    case AgentActions.RETRY_GITHUB_SYNC:
      return "Retry GitHub Sync";
    case AgentActions.CANCEL:
      return "Cancel Card";
    case AgentActions.CLOSE:
      return "Close Card";
    default:
      return humanize(action);
  }
}

function authorLabel(value: string): string {
  switch (value.toLowerCase()) {
    case "user":
      return "You";
    case "agent":
    case "assistant":
      return "Codex";
    case "worker":
      return "Worker";
    case "system":
      return "RemiHub";
    default:
      return humanize(value);
  }
}

function isDestructiveAction(action: AgentAction): boolean {
  return action === AgentActions.CANCEL || action === AgentActions.CLOSE;
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatShortId(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }

  return value.length > 12 ? value.slice(0, 12) : value;
}

function metadataValue(
  metadata: Record<string, unknown>,
  key: string,
): string | null {
  const value = metadata[key];
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function metadataSummary(metadata: Record<string, unknown>): string | null {
  const fields: Array<[string, string]> = [
    ["candidate_commit", "Candidate"],
    ["model", "Model"],
    ["executor", "Executor"],
    ["deployment_manifest", "Manifest"],
    ["signed_apk_sha256", "APK"],
  ];

  const summary = fields
    .map(([key, label]) => {
      const value = metadataValue(metadata, key);
      return value ? `${label}: ${value}` : null;
    })
    .filter(Boolean);

  return summary.length > 0 ? summary.join(" | ") : null;
}

function statusClasses(status: string): string {
  if (status === "failed" || status === "cancelled") {
    return "border-red-200 bg-red-50 text-red-700";
  }
  if (status === "blocked") {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }
  if (status === "completed" || status === "closed" || status === "succeeded") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (activeStatuses.has(status) || status === "running" || status === "queued" || status === "claimed") {
    return "border-blue-200 bg-blue-50 text-blue-700";
  }
  if (status === "awaiting_implementation_approval" || status === "review_ready" || status === "awaiting_feedback") {
    return "border-violet-200 bg-violet-50 text-violet-700";
  }
  return "border-slate-200 bg-slate-100 text-slate-700";
}

function StatusChip({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex max-w-full items-center rounded-full border px-2.5 py-1 text-xs font-bold ${statusClasses(status)}`}
    >
      {humanize(status)}
    </span>
  );
}

function InfoPill({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex max-w-full items-center rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">
      {children}
    </span>
  );
}

function ErrorBanner({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss: () => void;
}) {
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="font-semibold">{message}</div>
        <button
          className="rounded-md border border-red-200 bg-white px-3 py-1 text-xs font-bold text-red-700 hover:bg-red-50"
          onClick={onDismiss}
          type="button"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

function EmptyPanel({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 px-4 py-6 text-center text-sm font-medium text-slate-500">
      {children}
    </div>
  );
}

function CopyableBlock({
  label,
  text,
  tone = "default",
}: {
  label: string;
  text: string;
  tone?: "default" | "error";
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      className={`rounded-md border ${
        tone === "error"
          ? "border-red-200 bg-red-50"
          : "border-slate-200 bg-slate-50"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-inherit px-3 py-2">
        <div className="text-xs font-bold uppercase text-slate-500">{label}</div>
        <button
          className="rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs font-bold text-slate-700 hover:bg-slate-100"
          onClick={copy}
          type="button"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="max-h-[34rem] overflow-auto whitespace-pre-wrap break-words p-3 text-sm leading-6 text-slate-800">
        {text}
      </pre>
    </div>
  );
}

function CardSummaryRow({
  card,
  selected,
  onSelect,
}: {
  card: AgentCardSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`w-full rounded-md border bg-white p-4 text-left shadow-sm transition hover:border-blue-300 ${
        selected ? "border-blue-400 ring-2 ring-blue-100" : "border-slate-200"
      }`}
      onClick={onSelect}
      type="button"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="break-words text-base font-black text-slate-950">
            {card.title}
          </h2>
          <p className="mt-2 line-clamp-3 text-sm leading-6 text-slate-600">
            {card.description}
          </p>
        </div>
        <StatusChip status={card.status} />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <InfoPill>{scopeLabel(card.repository_scope)}</InfoPill>
        <InfoPill>Revision {card.revision}</InfoPill>
        <InfoPill>Updated {formatDateTime(card.updated_at)}</InfoPill>
      </div>

      {card.latest_run && (
        <div className="mt-3 rounded-md bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-600">
          {humanize(card.latest_run.phase)} / {humanize(card.latest_run.status)} / attempt{" "}
          {card.latest_run.attempt_count}
        </div>
      )}

      {card.blocked_reason && (
        <div className="mt-2 text-sm font-semibold text-amber-800">
          Blocked: {card.blocked_reason}
        </div>
      )}

      {card.latest_run?.error_message && (
        <div className="mt-2 line-clamp-2 text-sm font-semibold text-red-700">
          {card.latest_run.error_message}
        </div>
      )}
    </button>
  );
}

function CardList({
  cards,
  selectedId,
  onSelect,
}: {
  cards: AgentCardSummary[];
  selectedId: string | null;
  onSelect: (cardId: string) => void;
}) {
  const activeCards = cards.filter((card) => !terminalStatuses.has(card.status));
  const historicalCards = cards.filter((card) => terminalStatuses.has(card.status));

  if (cards.length === 0) {
    return <EmptyPanel>No Agent cards are visible for this filter.</EmptyPanel>;
  }

  return (
    <div className="space-y-5">
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-black uppercase text-slate-500">
            Active Work
          </h2>
          <span className="text-xs font-bold text-slate-500">
            {activeCards.length}
          </span>
        </div>
        <div className="space-y-3">
          {activeCards.length === 0 ? (
            <EmptyPanel>No active Agent cards.</EmptyPanel>
          ) : (
            activeCards.map((card) => (
              <CardSummaryRow
                card={card}
                key={card.id}
                onSelect={() => onSelect(card.id)}
                selected={selectedId === card.id}
              />
            ))
          )}
        </div>
      </section>

      {historicalCards.length > 0 && (
        <section>
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-black uppercase text-slate-500">
              History
            </h2>
            <span className="text-xs font-bold text-slate-500">
              {historicalCards.length}
            </span>
          </div>
          <div className="space-y-3">
            {historicalCards.map((card) => (
              <CardSummaryRow
                card={card}
                key={card.id}
                onSelect={() => onSelect(card.id)}
                selected={selectedId === card.id}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function CreateCardForm({
  disabled,
  onCreate,
}: {
  disabled: boolean;
  onCreate: (title: string, description: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const cleanTitle = title.trim();
    const cleanDescription = description.trim();
    if (!cleanTitle || !cleanDescription) {
      return;
    }
    onCreate(cleanTitle, cleanDescription);
    setTitle("");
    setDescription("");
    setOpen(false);
  };

  if (!open) {
    return (
      <button
        className="rounded-md bg-slate-950 px-4 py-2 text-sm font-bold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
        disabled={disabled}
        onClick={() => setOpen(true)}
        type="button"
      >
        New Card
      </button>
    );
  }

  return (
    <form
      className="rounded-md border border-slate-200 bg-white p-4 shadow-sm"
      onSubmit={submit}
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-black uppercase text-slate-500">
          New Agent Card
        </h2>
        <button
          className="text-sm font-bold text-slate-500 hover:text-slate-900"
          onClick={() => setOpen(false)}
          type="button"
        >
          Cancel
        </button>
      </div>
      <label className="block">
        <span className="text-sm font-semibold text-slate-700">Title</span>
        <input
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
          maxLength={160}
          onChange={(event) => setTitle(event.target.value)}
          required
          type="text"
          value={title}
        />
      </label>
      <label className="mt-3 block">
        <span className="text-sm font-semibold text-slate-700">
          Request
        </span>
        <textarea
          className="mt-1 min-h-40 w-full rounded-md border border-slate-300 px-3 py-2 text-base outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
          maxLength={20000}
          onChange={(event) => setDescription(event.target.value)}
          required
          value={description}
        />
      </label>
      <button
        className="mt-3 w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-bold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-400"
        disabled={disabled || !title.trim() || !description.trim()}
        type="submit"
      >
        Create Card
      </button>
    </form>
  );
}

function ActionPanel({
  card,
  actionInProgress,
  onAction,
}: {
  card: AgentCardDetail;
  actionInProgress: AgentAction | "create" | null;
  onAction: (action: AgentAction, notes?: string | null) => void;
}) {
  const actions = card.allowed_actions.filter(
    (action) => action !== AgentActions.ADD_FOLLOW_UP,
  );
  const [pendingAction, setPendingAction] = useState<AgentAction | null>(null);
  const [notes, setNotes] = useState("");

  if (actions.length === 0) {
    return null;
  }

  const confirm = () => {
    if (!pendingAction) {
      return;
    }
    onAction(pendingAction, notes);
    setNotes("");
    setPendingAction(null);
  };

  return (
    <section className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <h2 className="text-sm font-black uppercase text-slate-500">
        Available Actions
      </h2>
      <div className="mt-3 flex flex-wrap gap-2">
        {actions.map((action) => (
          <button
            className={`rounded-md px-3 py-2 text-sm font-bold disabled:cursor-not-allowed disabled:bg-slate-300 disabled:text-slate-500 ${
              isDestructiveAction(action)
                ? "border border-red-200 bg-white text-red-700 hover:bg-red-50"
                : "bg-blue-600 text-white hover:bg-blue-700"
            }`}
            disabled={actionInProgress !== null}
            key={action}
            onClick={() => setPendingAction(action)}
            type="button"
          >
            {actionInProgress === action ? "Submitting..." : actionLabel(action)}
          </button>
        ))}
      </div>

      {pendingAction && (
        <div className="mt-4 rounded-md border border-slate-200 bg-slate-50 p-3">
          <div className="text-sm font-bold text-slate-900">
            {actionLabel(pendingAction)}
          </div>
          <p className="mt-1 text-sm text-slate-600">
            Review the current card state before confirming this lifecycle action.
          </p>
          <textarea
            className="mt-3 min-h-24 w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
            maxLength={2000}
            onChange={(event) => setNotes(event.target.value)}
            placeholder="Notes (optional)"
            value={notes}
          />
          <div className="mt-3 flex flex-wrap justify-end gap-2">
            <button
              className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-100"
              disabled={actionInProgress !== null}
              onClick={() => setPendingAction(null)}
              type="button"
            >
              Back
            </button>
            <button
              className="rounded-md bg-slate-950 px-3 py-2 text-sm font-bold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
              disabled={actionInProgress !== null}
              onClick={confirm}
              type="button"
            >
              Confirm
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function FollowUpComposer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (content: string) => void;
}) {
  const [content, setContent] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const cleanContent = content.trim();
    if (!cleanContent) {
      return;
    }
    onSend(cleanContent);
    setContent("");
  };

  return (
    <form
      className="rounded-md border border-slate-200 bg-white p-4 shadow-sm"
      onSubmit={submit}
    >
      <h2 className="text-sm font-black uppercase text-slate-500">
        Send Follow-up
      </h2>
      <textarea
        className="mt-3 min-h-32 w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
        disabled={disabled}
        maxLength={20000}
        onChange={(event) => setContent(event.target.value)}
        value={content}
      />
      <button
        className="mt-3 rounded-md bg-blue-600 px-4 py-2 text-sm font-bold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-400"
        disabled={disabled || !content.trim()}
        type="submit"
      >
        Send
      </button>
    </form>
  );
}

function DetailHeader({ card }: { card: AgentCardDetail }) {
  return (
    <section className="rounded-md border border-blue-100 bg-blue-50 p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="break-words text-2xl font-black text-slate-950">
            {card.title}
          </h1>
          <div className="mt-3 flex flex-wrap gap-2">
            <StatusChip status={card.status} />
            <InfoPill>{scopeLabel(card.repository_scope)}</InfoPill>
            <InfoPill>Revision {card.revision}</InfoPill>
            {card.feature_branch && (
              <InfoPill>{formatShortId(card.feature_branch)}</InfoPill>
            )}
          </div>
        </div>
        <div className="text-right text-xs font-semibold text-slate-600">
          <div>Created {formatDateTime(card.created_at)}</div>
          <div>Updated {formatDateTime(card.updated_at)}</div>
        </div>
      </div>

      <div className="mt-4">
        <CopyableBlock label="Original Request" text={card.description} />
      </div>

      {card.blocked_reason && (
        <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-900">
          Blocked: {card.blocked_reason}
        </div>
      )}
    </section>
  );
}

function LatestRunCard({ run }: { run: AgentLatestRun }) {
  return (
    <section className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-black uppercase text-slate-500">
          Latest Run
        </h2>
        <StatusChip status={run.status} />
      </div>
      <div className="mt-3 grid gap-3 text-sm md:grid-cols-4">
        <Metric label="Phase" value={humanize(run.phase)} />
        <Metric label="Revision" value={run.card_revision} />
        <Metric label="Attempt" value={run.attempt_count} />
        <Metric label="Updated" value={formatDateTime(run.updated_at)} />
      </div>
      {run.blocked_reason && (
        <div className="mt-3 text-sm font-semibold text-amber-800">
          Blocked: {run.blocked_reason}
        </div>
      )}
      {run.error_message && (
        <div className="mt-3">
          <CopyableBlock label="Run Error" text={run.error_message} tone="error" />
        </div>
      )}
    </section>
  );
}

function DeploymentRecoveryCard({
  recovery,
}: {
  recovery: AgentDeploymentRecovery;
}) {
  return (
    <section className="rounded-md border border-amber-200 bg-amber-50 p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-black uppercase text-amber-900">
          Deployment Recovery
        </h2>
        <StatusChip status={recovery.github_sync_status} />
      </div>
      <div className="mt-3 grid gap-3 text-sm md:grid-cols-3">
        <Metric label="Retryable" value={recovery.retryable ? "Yes" : "No"} />
        <Metric
          label="Production Deployed"
          value={recovery.production_deployed ? "Yes" : "No"}
        />
        <Metric
          label="Deployment Run"
          value={formatShortId(recovery.deployment_run_id) ?? "-"}
        />
      </div>
      {recovery.candidate_commit && (
        <div className="mt-3 text-sm font-semibold text-amber-900">
          Candidate: {recovery.candidate_commit}
        </div>
      )}
      {recovery.blocker_code && (
        <div className="mt-2 text-sm font-semibold text-amber-900">
          Blocker: {humanize(recovery.blocker_code)}
        </div>
      )}
      {recovery.last_error && (
        <div className="mt-3">
          <CopyableBlock label="Recovery Error" text={recovery.last_error} tone="error" />
        </div>
      )}
    </section>
  );
}

function Metric({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="rounded-md bg-slate-50 px-3 py-2">
      <div className="text-xs font-bold uppercase text-slate-500">{label}</div>
      <div className="mt-1 break-words text-sm font-semibold text-slate-900">
        {value}
      </div>
    </div>
  );
}

function MessageCard({ message }: { message: AgentMessage }) {
  return (
    <article className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="font-bold text-slate-900">
          {authorLabel(message.author_type)}
        </div>
        <div className="text-xs font-semibold text-slate-500">
          {formatDateTime(message.created_at)}
        </div>
      </div>
      <CopyableBlock label="Message" text={message.content} />
    </article>
  );
}

function RunCard({ run }: { run: AgentRun }) {
  const metadata = metadataSummary(run.result_metadata);

  return (
    <article className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="font-bold text-slate-900">{humanize(run.phase)}</h3>
          <div className="mt-1 text-xs font-semibold text-slate-500">
            Revision {run.card_revision} / attempt {run.attempt_count}
          </div>
        </div>
        <StatusChip status={run.status} />
      </div>

      <div className="mt-3 grid gap-3 text-sm md:grid-cols-4">
        <Metric label="Worker" value={run.worker_id || "-"} />
        <Metric label="Started" value={formatDateTime(run.started_at)} />
        <Metric label="Finished" value={formatDateTime(run.finished_at)} />
        <Metric label="Updated" value={formatDateTime(run.updated_at)} />
      </div>

      {run.blocked_reason && (
        <div className="mt-3 text-sm font-semibold text-amber-800">
          Blocked: {run.blocked_reason}
        </div>
      )}
      {run.error_message && (
        <div className="mt-3">
          <CopyableBlock label="Run Error" text={run.error_message} tone="error" />
        </div>
      )}
      {metadata && (
        <div className="mt-3 rounded-md bg-slate-50 px-3 py-2 text-sm font-semibold text-slate-600">
          {metadata}
        </div>
      )}
    </article>
  );
}

function ApprovalCard({ approval }: { approval: AgentApproval }) {
  return (
    <article className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="font-bold text-slate-900">
          {humanize(approval.approval_type)}: {humanize(approval.decision)}
        </div>
        <div className="text-xs font-semibold text-slate-500">
          {formatDateTime(approval.created_at)}
        </div>
      </div>
      <div className="mt-1 text-xs font-semibold text-slate-500">
        Revision {approval.card_revision}
      </div>
      {approval.notes && (
        <div className="mt-3">
          <CopyableBlock label="Approval Notes" text={approval.notes} />
        </div>
      )}
    </article>
  );
}

function EventRow({ event }: { event: AgentEvent }) {
  return (
    <div className="rounded-md border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="font-semibold text-slate-900">
          {humanize(event.event_type)}
        </div>
        <div className="text-xs font-semibold text-slate-500">
          {authorLabel(event.actor_type)} / {formatDateTime(event.created_at)}
        </div>
      </div>
    </div>
  );
}

function Section({
  count,
  title,
  children,
}: {
  count?: number;
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-black text-slate-950">{title}</h2>
        {typeof count === "number" && <InfoPill>{count}</InfoPill>}
      </div>
      {children}
    </section>
  );
}

function CardDetail({
  actionInProgress,
  card,
  isLoading,
  onAction,
  onBack,
  onRefresh,
  onSendFollowUp,
}: {
  actionInProgress: AgentAction | "create" | null;
  card: AgentCardDetail;
  isLoading: boolean;
  onAction: (action: AgentAction, notes?: string | null) => void;
  onBack: () => void;
  onRefresh: () => void;
  onSendFollowUp: (content: string) => void;
}) {
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <button
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-100 lg:hidden"
          onClick={onBack}
          type="button"
        >
          Back to cards
        </button>
        <button
          className="ml-auto rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
          disabled={isLoading || actionInProgress !== null}
          onClick={onRefresh}
          type="button"
        >
          Refresh Card
        </button>
      </div>

      {(isLoading || actionInProgress !== null) && (
        <div className="h-1 overflow-hidden rounded-full bg-slate-200">
          <div className="h-full w-1/2 animate-pulse rounded-full bg-blue-500" />
        </div>
      )}

      <DetailHeader card={card} />

      <ActionPanel
        actionInProgress={actionInProgress}
        card={card}
        onAction={onAction}
      />

      {card.allowed_actions.includes(AgentActions.ADD_FOLLOW_UP) && (
        <FollowUpComposer
          disabled={actionInProgress !== null}
          onSend={onSendFollowUp}
        />
      )}

      {card.latest_run && <LatestRunCard run={card.latest_run} />}
      {card.deployment_recovery && (
        <DeploymentRecoveryCard recovery={card.deployment_recovery} />
      )}

      <Section count={card.messages.length} title="Conversation">
        <div className="space-y-3">
          {card.messages.length === 0 ? (
            <EmptyPanel>No messages have been recorded yet.</EmptyPanel>
          ) : (
            card.messages.map((message) => (
              <MessageCard key={message.id} message={message} />
            ))
          )}
        </div>
      </Section>

      <Section count={card.runs.length} title="Run History">
        <div className="space-y-3">
          {card.runs.length === 0 ? (
            <EmptyPanel>No worker runs have been recorded yet.</EmptyPanel>
          ) : (
            [...card.runs]
              .reverse()
              .map((run) => <RunCard key={run.id} run={run} />)
          )}
        </div>
      </Section>

      {card.approvals.length > 0 && (
        <Section count={card.approvals.length} title="Approvals">
          <div className="space-y-3">
            {[...card.approvals]
              .reverse()
              .map((approval) => (
                <ApprovalCard approval={approval} key={approval.id} />
              ))}
          </div>
        </Section>
      )}

      {card.events.length > 0 && (
        <Section count={card.events.length} title="Recent Events">
          <div className="space-y-3">
            {[...card.events]
              .slice(-20)
              .reverse()
              .map((event) => <EventRow event={event} key={event.id} />)}
          </div>
        </Section>
      )}
    </div>
  );
}

export default function AgentScreen() {
  const [cards, setCards] = useState<AgentCardSummary[]>([]);
  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [selectedCard, setSelectedCard] = useState<AgentCardDetail | null>(null);
  const [includeClosed, setIncludeClosed] = useState(false);
  const [isLoadingCards, setIsLoadingCards] = useState(true);
  const [isLoadingCard, setIsLoadingCard] = useState(false);
  const [actionInProgress, setActionInProgress] = useState<AgentAction | "create" | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const selectedSummary = useMemo(
    () => cards.find((card) => card.id === selectedCardId) ?? null,
    [cards, selectedCardId],
  );

  const loadCards = useCallback(
    async (silent = false) => {
      if (!silent) {
        setIsLoadingCards(true);
        setErrorMessage(null);
      }
      try {
        const nextCards = await listAgentCards(includeClosed);
        setCards(nextCards);
      } catch (caught) {
        if (!silent) {
          setErrorMessage(
            caught instanceof Error ? caught.message : "Unable to load Agent cards.",
          );
        }
      } finally {
        if (!silent) {
          setIsLoadingCards(false);
        }
      }
    },
    [includeClosed],
  );

  const loadCard = useCallback(
    async (cardId: string, silent = false) => {
      if (!silent) {
        setIsLoadingCard(true);
        setErrorMessage(null);
      }
      try {
        const nextCard = await getAgentCard(cardId);
        setSelectedCard(nextCard);
        setSelectedCardId(nextCard.id);
      } catch (caught) {
        if (!silent) {
          setErrorMessage(
            caught instanceof Error ? caught.message : "Unable to load Agent card.",
          );
        }
      } finally {
        if (!silent) {
          setIsLoadingCard(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    void loadCards(false);
  }, [loadCards]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (actionInProgress === null) {
        void loadCards(true);
        if (selectedCardId) {
          void loadCard(selectedCardId, true);
        }
      }
    }, POLL_INTERVAL_MILLIS);

    return () => window.clearInterval(interval);
  }, [actionInProgress, loadCard, loadCards, selectedCardId]);

  const selectCard = (cardId: string) => {
    setSelectedCardId(cardId);
    void loadCard(cardId, false);
  };

  const createCard = async (title: string, description: string) => {
    setActionInProgress("create");
    setErrorMessage(null);
    try {
      const card = await createAgentCard(title, description);
      setSelectedCard(card);
      setSelectedCardId(card.id);
      await loadCards(true);
    } catch (caught) {
      setErrorMessage(
        caught instanceof Error ? caught.message : "Unable to create Agent card.",
      );
    } finally {
      setActionInProgress(null);
    }
  };

  const sendFollowUp = async (content: string) => {
    if (!selectedCard) {
      return;
    }
    if (!selectedCard.allowed_actions.includes(AgentActions.ADD_FOLLOW_UP)) {
      setErrorMessage("This card is not accepting follow-up messages right now.");
      return;
    }

    setActionInProgress(AgentActions.ADD_FOLLOW_UP);
    setErrorMessage(null);
    try {
      const card = await addAgentFollowUp(selectedCard.id, content);
      setSelectedCard(card);
      setSelectedCardId(card.id);
      await loadCards(true);
    } catch (caught) {
      setErrorMessage(
        caught instanceof Error ? caught.message : "Unable to send follow-up.",
      );
    } finally {
      setActionInProgress(null);
    }
  };

  const runAction = async (action: AgentAction, notes?: string | null) => {
    if (!selectedCard) {
      return;
    }
    if (!selectedCard.allowed_actions.includes(action)) {
      setErrorMessage("That action is no longer available. Refresh the card and try again.");
      return;
    }

    setActionInProgress(action);
    setErrorMessage(null);
    try {
      const card = await performAgentAction(selectedCard, action, notes);
      setSelectedCard(card);
      setSelectedCardId(card.id);
      await loadCards(true);
    } catch (caught) {
      setErrorMessage(
        caught instanceof Error ? caught.message : "Agent action failed.",
      );
    } finally {
      setActionInProgress(null);
    }
  };

  return (
    <main className="mx-auto max-w-7xl px-4 py-6">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-black text-slate-950">Agent</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">
            Monitor RemiHub Agent cards, review output, and perform approved lifecycle actions.
          </p>
        </div>
        <CreateCardForm
          disabled={actionInProgress !== null}
          onCreate={createCard}
        />
      </div>

      {errorMessage && (
        <div className="mb-5">
          <ErrorBanner
            message={errorMessage}
            onDismiss={() => setErrorMessage(null)}
          />
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(20rem,24rem)_1fr]">
        <aside className="space-y-4">
          <section className="rounded-md border border-slate-200 bg-white p-4 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                <input
                  checked={includeClosed}
                  className="h-4 w-4"
                  onChange={(event) => setIncludeClosed(event.target.checked)}
                  type="checkbox"
                />
                Include closed
              </label>
              <button
                className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
                disabled={isLoadingCards || actionInProgress !== null}
                onClick={() => void loadCards(false)}
                type="button"
              >
                Refresh
              </button>
            </div>
          </section>

          {isLoadingCards ? (
            <div className="rounded-md border border-slate-200 bg-white p-6 text-center text-sm font-semibold text-slate-500 shadow-sm">
              Loading Agent cards...
            </div>
          ) : (
            <CardList
              cards={cards}
              onSelect={selectCard}
              selectedId={selectedCardId}
            />
          )}
        </aside>

        <section>
          {selectedCard ? (
            <CardDetail
              actionInProgress={actionInProgress}
              card={selectedCard}
              isLoading={isLoadingCard}
              onAction={runAction}
              onBack={() => {
                setSelectedCard(null);
                setSelectedCardId(null);
              }}
              onRefresh={() => void loadCard(selectedCard.id, false)}
              onSendFollowUp={sendFollowUp}
            />
          ) : selectedSummary ? (
            <div className="rounded-md border border-slate-200 bg-white p-6 text-sm font-semibold text-slate-500 shadow-sm">
              Loading {selectedSummary.title}...
            </div>
          ) : (
            <EmptyPanel>Select an Agent card to inspect its details.</EmptyPanel>
          )}
        </section>
      </div>
    </main>
  );
}
