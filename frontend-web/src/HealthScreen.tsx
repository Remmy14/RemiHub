import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { ApiAuthenticationError } from "./api/authenticatedApi";
import { getServiceHealthSnapshot } from "./api/healthApi";
import type {
  HealthComponent,
  HealthDependencyCheck,
  HealthStatus,
  ServiceHealthSnapshotResponse,
} from "./api/healthApi";

const POLL_INTERVAL_MILLIS = 30000;

const knownGroupOrder = ["core", "agent", "storage", "rh_storage", "media"];

type LoadPhase = "idle" | "loading" | "refreshing";

type StatusTone = {
  label: string;
  summary: string;
  symbol: string;
  chipClasses: string;
  panelClasses: string;
  dotClasses: string;
};

const statusTones: Record<HealthStatus, StatusTone> = {
  healthy: {
    label: "Healthy",
    summary: "Operational",
    symbol: "OK",
    chipClasses: "border-emerald-200 bg-emerald-50 text-emerald-700",
    panelClasses: "border-emerald-200 bg-emerald-50",
    dotClasses: "bg-emerald-500",
  },
  idle: {
    label: "Idle",
    summary: "Normal standby",
    symbol: "IDLE",
    chipClasses: "border-teal-200 bg-teal-50 text-teal-700",
    panelClasses: "border-teal-200 bg-teal-50",
    dotClasses: "bg-teal-500",
  },
  degraded: {
    label: "Degraded",
    summary: "Needs attention",
    symbol: "WARN",
    chipClasses: "border-amber-200 bg-amber-50 text-amber-800",
    panelClasses: "border-amber-200 bg-amber-50",
    dotClasses: "bg-amber-500",
  },
  unhealthy: {
    label: "Unhealthy",
    summary: "Action required",
    symbol: "FAIL",
    chipClasses: "border-red-200 bg-red-50 text-red-700",
    panelClasses: "border-red-200 bg-red-50",
    dotClasses: "bg-red-500",
  },
  unknown: {
    label: "Unknown",
    summary: "Not confirmed healthy",
    symbol: "?",
    chipClasses: "border-slate-300 bg-slate-100 text-slate-700",
    panelClasses: "border-slate-300 bg-slate-50",
    dotClasses: "bg-slate-500",
  },
};

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

function groupLabel(value: string): string {
  if (value === "rh_storage") {
    return "RH-Storage";
  }
  return humanize(value);
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "Unknown";
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
    second: "2-digit",
  });
}

function safeRefreshError(error: unknown): string {
  if (error instanceof ApiAuthenticationError) {
    return error.message;
  }
  return "Service health is temporarily unavailable.";
}

function StatusChip({ status }: { status: HealthStatus }) {
  const tone = statusTones[status];

  return (
    <span
      className={`inline-flex max-w-full items-center gap-2 rounded-full border px-2.5 py-1 text-xs font-black ${tone.chipClasses}`}
    >
      <span aria-hidden="true">{tone.symbol}</span>
      <span>{tone.label}</span>
    </span>
  );
}

function DetailPill({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex max-w-full items-center rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">
      {children}
    </span>
  );
}

function MetadataItem({
  label,
  value,
}: {
  label: string;
  value: ReactNode | null | undefined;
}) {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  return (
    <div className="rounded-md bg-slate-50 px-3 py-2">
      <dt className="text-xs font-bold uppercase text-slate-500">{label}</dt>
      <dd className="mt-1 break-words text-sm font-semibold text-slate-900">
        {value}
      </dd>
    </div>
  );
}

function DependencyRow({ dependency }: { dependency: HealthDependencyCheck }) {
  return (
    <li className="rounded-md border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="break-words text-sm font-black text-slate-900">
            {dependency.name}
          </div>
          <div className="mt-1 text-sm leading-6 text-slate-600">
            {dependency.message}
          </div>
        </div>
        <StatusChip status={dependency.status} />
      </div>
      <dl className="mt-3 grid gap-2 sm:grid-cols-3">
        <MetadataItem label="Kind" value={humanize(dependency.kind)} />
        <MetadataItem label="Expected" value={dependency.expected} />
        <MetadataItem label="Observed" value={dependency.observed} />
        <MetadataItem label="Path" value={dependency.path} />
      </dl>
    </li>
  );
}

function ComponentCard({ component }: { component: HealthComponent }) {
  const tone = statusTones[component.status];
  const systemd = component.systemd;
  const hasDetails =
    Boolean(component.unit) ||
    Boolean(component.expected_mode) ||
    Boolean(systemd) ||
    component.dependencies.length > 0;

  return (
    <article className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span
              aria-hidden="true"
              className={`h-2.5 w-2.5 rounded-full ${tone.dotClasses}`}
            />
            <h3 className="break-words text-base font-black text-slate-950">
              {component.name}
            </h3>
          </div>
          {component.message && (
            <p className="mt-2 text-sm leading-6 text-slate-600">
              {component.message}
            </p>
          )}
        </div>
        <StatusChip status={component.status} />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <DetailPill>{humanize(component.kind)}</DetailPill>
        <DetailPill>
          {component.required ? "Required" : "Optional"}
        </DetailPill>
        {component.expected_mode && (
          <DetailPill>{humanize(component.expected_mode)}</DetailPill>
        )}
        {component.unit && <DetailPill>{component.unit}</DetailPill>}
      </div>

      {hasDetails && (
        <details className="mt-4 rounded-md border border-slate-200 bg-slate-50">
          <summary className="cursor-pointer px-3 py-2 text-sm font-bold text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-100">
            Component details
          </summary>
          <div className="border-t border-slate-200 p-3">
            <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              <MetadataItem label="Component ID" value={component.id} />
              <MetadataItem label="Group" value={groupLabel(component.group)} />
              <MetadataItem label="Unit" value={component.unit} />
              <MetadataItem
                label="Expected Mode"
                value={component.expected_mode && humanize(component.expected_mode)}
              />
              <MetadataItem
                label="Checked"
                value={formatDateTime(component.checked_at)}
              />
              <MetadataItem
                label="Systemd Available"
                value={
                  systemd ? (systemd.available ? "Yes" : "No") : undefined
                }
              />
              <MetadataItem label="Load State" value={systemd?.load_state} />
              <MetadataItem label="Active State" value={systemd?.active_state} />
              <MetadataItem label="Substate" value={systemd?.sub_state} />
              <MetadataItem label="Result" value={systemd?.result} />
              <MetadataItem label="Unit File" value={systemd?.unit_file_state} />
              <MetadataItem label="Type" value={systemd?.type} />
              <MetadataItem label="Main PID" value={systemd?.main_pid} />
              <MetadataItem label="Restarts" value={systemd?.n_restarts} />
              <MetadataItem
                label="State Changed"
                value={systemd?.state_change_timestamp}
              />
            </dl>

            {component.dependencies.length > 0 && (
              <div className="mt-4">
                <h4 className="text-xs font-black uppercase text-slate-500">
                  Dependency Checks
                </h4>
                <ul className="mt-2 space-y-2">
                  {component.dependencies.map((dependency) => (
                    <DependencyRow
                      dependency={dependency}
                      key={dependency.id}
                    />
                  ))}
                </ul>
              </div>
            )}
          </div>
        </details>
      )}
    </article>
  );
}

function EmptyState({ onRefresh }: { onRefresh: () => void }) {
  return (
    <section className="rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center shadow-sm">
      <h2 className="text-lg font-black text-slate-950">
        No health components were returned
      </h2>
      <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-slate-600">
        The health API responded without component data, so the dashboard cannot
        present this snapshot as operational.
      </p>
      <button
        className="mt-4 rounded-md bg-slate-950 px-4 py-2 text-sm font-bold text-white hover:bg-slate-800"
        onClick={onRefresh}
        type="button"
      >
        Retry
      </button>
    </section>
  );
}

function InitialError({
  error,
  onRefresh,
}: {
  error: string;
  onRefresh: () => void;
}) {
  return (
    <section className="rounded-lg border border-red-200 bg-red-50 p-6 shadow-sm">
      <h2 className="text-lg font-black text-red-900">
        Service health is unavailable
      </h2>
      <p className="mt-2 text-sm font-semibold text-red-800">{error}</p>
      <button
        className="mt-4 rounded-md bg-red-700 px-4 py-2 text-sm font-bold text-white hover:bg-red-800"
        onClick={onRefresh}
        type="button"
      >
        Retry
      </button>
    </section>
  );
}

function groupedComponents(components: HealthComponent[]) {
  const groups = new Map<string, HealthComponent[]>();

  components.forEach((component) => {
    const group = component.group;
    groups.set(group, [...(groups.get(group) ?? []), component]);
  });

  return Array.from(groups.entries()).sort(([left], [right]) => {
    const leftIndex = knownGroupOrder.indexOf(left);
    const rightIndex = knownGroupOrder.indexOf(right);
    if (leftIndex !== -1 || rightIndex !== -1) {
      if (leftIndex === -1) {
        return 1;
      }
      if (rightIndex === -1) {
        return -1;
      }
      return leftIndex - rightIndex;
    }
    return groupLabel(left).localeCompare(groupLabel(right));
  });
}

function HealthDashboard({
  snapshot,
  stale,
  refreshError,
  phase,
  onRefresh,
}: {
  snapshot: ServiceHealthSnapshotResponse;
  stale: boolean;
  refreshError: string | null;
  phase: LoadPhase;
  onRefresh: () => void;
}) {
  const tone = statusTones[snapshot.overall];
  const groups = useMemo(
    () => groupedComponents(snapshot.components),
    [snapshot.components],
  );

  return (
    <>
      <section
        className={`rounded-lg border p-5 shadow-sm ${tone.panelClasses}`}
        aria-labelledby="health-overall-heading"
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-sm font-bold uppercase text-slate-600">
              RemiHub Health
            </div>
            <h1
              className="mt-1 break-words text-3xl font-black text-slate-950"
              id="health-overall-heading"
            >
              {tone.label}: {tone.summary}
            </h1>
            <p className="mt-2 text-sm font-semibold text-slate-700">
              Snapshot checked {formatDateTime(snapshot.checked_at)}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <StatusChip status={snapshot.overall} />
            <button
              aria-label="Refresh service health"
              className="rounded-md bg-slate-950 px-4 py-2 text-sm font-bold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
              disabled={phase === "refreshing"}
              onClick={onRefresh}
              type="button"
            >
              {phase === "refreshing" ? "Refreshing..." : "Refresh"}
            </button>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <DetailPill>
            {phase === "refreshing" ? "Refresh in progress" : "Refresh ready"}
          </DetailPill>
          <DetailPill>
            {snapshot.components.length} component
            {snapshot.components.length === 1 ? "" : "s"}
          </DetailPill>
          {stale && (
            <span className="inline-flex max-w-full items-center rounded-full border border-amber-200 bg-white px-2.5 py-1 text-xs font-black text-amber-800">
              Stale snapshot
            </span>
          )}
        </div>
      </section>

      {refreshError && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-semibold text-amber-900">
          Last refresh failed. Showing the last successful snapshot from{" "}
          {formatDateTime(snapshot.checked_at)}. {refreshError}
        </div>
      )}

      {snapshot.components.length === 0 ? (
        <EmptyState onRefresh={onRefresh} />
      ) : (
        <div className="space-y-6">
          {groups.map(([group, components]) => (
            <section key={group} aria-labelledby={`health-group-${group}`}>
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h2
                  className="text-sm font-black uppercase text-slate-500"
                  id={`health-group-${group}`}
                >
                  {groupLabel(group)}
                </h2>
                <span className="text-xs font-bold text-slate-500">
                  {components.length}
                </span>
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                {components.map((component) => (
                  <ComponentCard component={component} key={component.id} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </>
  );
}

export default function HealthScreen() {
  const [snapshot, setSnapshot] =
    useState<ServiceHealthSnapshotResponse | null>(null);
  const [phase, setPhase] = useState<LoadPhase>("loading");
  const [initialError, setInitialError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const requestInFlight = useRef(false);
  const mounted = useRef(false);
  const snapshotRef = useRef<ServiceHealthSnapshotResponse | null>(null);

  const refresh = useCallback(async () => {
    if (requestInFlight.current) {
      return;
    }

    requestInFlight.current = true;
    const hadSnapshot = snapshotRef.current !== null;
    setPhase(hadSnapshot ? "refreshing" : "loading");
    if (!hadSnapshot) {
      setInitialError(null);
    }

    try {
      const nextSnapshot = await getServiceHealthSnapshot();
      if (!mounted.current) {
        return;
      }
      if (nextSnapshot.success !== true || !Array.isArray(nextSnapshot.components)) {
        throw new Error("Malformed service health response.");
      }
      snapshotRef.current = nextSnapshot;
      setSnapshot(nextSnapshot);
      setInitialError(null);
      setRefreshError(null);
      setStale(false);
    } catch (error) {
      if (!mounted.current) {
        return;
      }
      const safeError = safeRefreshError(error);
      if (hadSnapshot) {
        setRefreshError(safeError);
        setStale(true);
      } else {
        setInitialError(safeError);
      }
    } finally {
      requestInFlight.current = false;
      if (mounted.current) {
        setPhase("idle");
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();

    const interval = window.setInterval(() => {
      if (document.visibilityState === "hidden") {
        return;
      }
      void refresh();
    }, POLL_INTERVAL_MILLIS);

    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void refresh();
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      mounted.current = false;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refresh]);

  return (
    <main className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6">
        <div className="text-sm font-bold uppercase text-blue-600">
          RemiHub Portal
        </div>
        <h1 className="mt-1 text-3xl font-black text-slate-950">
          Service Health
        </h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
          Read-only operational status from the centralized RemiHub health API.
        </p>
      </div>

      <div className="space-y-6">
        {phase === "loading" && !snapshot && (
          <section className="rounded-lg border border-slate-200 bg-white p-6 text-sm font-semibold text-slate-600 shadow-sm">
            Loading service health...
          </section>
        )}

        {initialError && !snapshot && (
          <InitialError error={initialError} onRefresh={refresh} />
        )}

        {snapshot && (
          <HealthDashboard
            phase={phase}
            refreshError={refreshError}
            snapshot={snapshot}
            stale={stale}
            onRefresh={refresh}
          />
        )}
      </div>
    </main>
  );
}
