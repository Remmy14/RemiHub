import React, { useEffect, useMemo, useState } from "react";

type PoolSummary = {
  id: number;
  name: string;
  participantCount: number;
};

type Driver = {
  number: string;
  name: string;
  starting_position: number;
  takenBy?: string | null;
  car_image_url?: string;
};

type DraftStatus = {
  status: string;
  current_picker: string;
  on_deck: string[];
  total_picks: number;
  participants?: string[];
};

type PoolAssignments = Record<string, { number: string; name: string }[]>;

type RecentPick = {
  participant: string;
  driver_name: string;
  car_number: string;
  pick_number: number;
};

type Tab = "draft" | "field" | "available" | "saved" | "teams";

const API_BASE = "https://remillard.duckdns.org";

const getSavedDriversStorageKey = (poolId: number) =>
  `remihub-draft-saved-drivers-${poolId}`;

const DraftCompanionScreen: React.FC = () => {
  const [pools, setPools] = useState<PoolSummary[]>([]);
  const [selectedPoolId, setSelectedPoolId] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("draft");

  const [draftStatus, setDraftStatus] = useState<DraftStatus | null>(null);
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [assignments, setAssignments] = useState<PoolAssignments>({});
  const [recentPicks, setRecentPicks] = useState<RecentPick[]>([]);
  const [lastUpdated, setLastUpdated] = useState<string>("");
  const [savedDriverNumbers, setSavedDriverNumbers] = useState<string[]>([]);

  const loadPools = async () => {
    const res = await fetch(`${API_BASE}/race/getPools`);
    const data = await res.json();

    if (Array.isArray(data)) {
      setPools(data);
      setSelectedPoolId((current) => current ?? data[0]?.id ?? null);
    }
  };

  const loadDraftData = async (poolId: number) => {
    try {
      const [statusRes, gridRes, assignmentsRes, picksRes] = await Promise.all([
        fetch(`${API_BASE}/race/getDraftStatus?pool_id=${poolId}`),
        fetch(`${API_BASE}/race/getStartingGridStatus?pool_id=${poolId}`),
        fetch(`${API_BASE}/race/getPoolAssignments?pool_id=${poolId}`),
        fetch(`${API_BASE}/race/getRecentPicks?pool_id=${poolId}&limit=5`),
      ]);

      const statusData = await statusRes.json();
      const gridData = await gridRes.json();
      const assignmentsData = await assignmentsRes.json();
      const picksData = await picksRes.json();

      setDraftStatus(statusData);
      setDrivers(Array.isArray(gridData) ? gridData : []);
      setAssignments(assignmentsData ?? {});
      setRecentPicks(Array.isArray(picksData) ? picksData : []);
      setLastUpdated(new Date().toLocaleTimeString());
    } catch (error) {
      console.error("Failed to load draft companion data", error);
    }
  };

  useEffect(() => {
    loadPools();
  }, []);

  useEffect(() => {
    if (!selectedPoolId) return;

    const storageKey = getSavedDriversStorageKey(selectedPoolId);
    const savedValue = localStorage.getItem(storageKey);

    if (!savedValue) {
      setSavedDriverNumbers([]);
      return;
    }

    try {
      const parsedValue = JSON.parse(savedValue);
      setSavedDriverNumbers(Array.isArray(parsedValue) ? parsedValue : []);
    } catch {
      setSavedDriverNumbers([]);
    }
  }, [selectedPoolId]);

  useEffect(() => {
    if (!selectedPoolId) return;

    const storageKey = getSavedDriversStorageKey(selectedPoolId);
    localStorage.setItem(storageKey, JSON.stringify(savedDriverNumbers));
  }, [selectedPoolId, savedDriverNumbers]);

  useEffect(() => {
    if (!selectedPoolId) return;

    loadDraftData(selectedPoolId);

    const interval = setInterval(() => {
      loadDraftData(selectedPoolId);
    }, 1000);

    return () => clearInterval(interval);
  }, [selectedPoolId]);

  const availableDrivers = useMemo(
    () => drivers.filter((driver) => !driver.takenBy),
    [drivers]
  );

  const savedAvailableDrivers = useMemo(
    () =>
      availableDrivers.filter((driver) =>
        savedDriverNumbers.includes(driver.number)
      ),
    [availableDrivers, savedDriverNumbers]
  );

  const participantNames = useMemo(() => {
    const fromStatus = draftStatus?.participants ?? [];
    const fromAssignments = Object.keys(assignments);
    return Array.from(new Set([...fromStatus, ...fromAssignments]));
  }, [draftStatus, assignments]);

  const toggleSavedDriver = (driverNumber: string) => {
    setSavedDriverNumbers((current) => {
      if (current.includes(driverNumber)) {
        return current.filter((number) => number !== driverNumber);
      }

      return [...current, driverNumber];
    });
  };

  const clearSavedDrivers = () => {
    if (!selectedPoolId) return;

    const storageKey = getSavedDriversStorageKey(selectedPoolId);
    localStorage.removeItem(storageKey);
    setSavedDriverNumbers([]);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-md px-3 py-4">
        <h1 className="mb-3 text-xl font-bold">Indy Draft Companion</h1>

        <div className="mb-3 rounded-xl bg-slate-900 p-3 shadow">
          <label className="mb-1 block text-xs font-semibold uppercase text-slate-400">
            Pool
          </label>
          <select
            value={selectedPoolId ?? ""}
            onChange={(event) => {
              const poolId = Number(event.target.value);
              setSelectedPoolId(poolId);
              setDraftStatus(null);
              setDrivers([]);
              setAssignments({});
              setRecentPicks([]);
              setLastUpdated("Loading...");
              loadDraftData(poolId);
            }}
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-white"
          >
            {pools.map((pool) => (
              <option key={pool.id} value={pool.id}>
                {pool.name}
              </option>
            ))}
          </select>
        </div>

        <div className="sticky top-0 z-10 mb-3 grid grid-cols-5 gap-1 bg-slate-950 py-2">
          {(["draft", "field", "available", "saved", "teams"] as Tab[]).map(
            (tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`rounded-lg px-2 py-2 text-xs font-bold capitalize ${
                  activeTab === tab
                    ? "bg-yellow-400 text-slate-950"
                    : "bg-slate-800 text-slate-300"
                }`}
              >
                {tab}
              </button>
            )
          )}
        </div>

        {activeTab === "draft" && (
          <DraftTab status={draftStatus} recentPicks={recentPicks} />
        )}

        {activeTab === "field" && (
          <FieldTab
            drivers={drivers}
            savedDriverNumbers={savedDriverNumbers}
            onToggleSavedDriver={toggleSavedDriver}
          />
        )}

        {activeTab === "available" && (
          <AvailableTab
            drivers={availableDrivers}
            savedDriverNumbers={savedDriverNumbers}
            onToggleSavedDriver={toggleSavedDriver}
          />
        )}

        {activeTab === "saved" && (
          <SavedTab
            drivers={savedAvailableDrivers}
            savedDriverCount={savedDriverNumbers.length}
            savedAvailableDriverCount={savedAvailableDrivers.length}
            savedDriverNumbers={savedDriverNumbers}
            onToggleSavedDriver={toggleSavedDriver}
            onClearSavedDrivers={clearSavedDrivers}
          />
        )}

        {activeTab === "teams" && (
          <TeamsTab participants={participantNames} assignments={assignments} />
        )}
      </div>

      <div className="mt-4 text-center text-xs text-slate-500">
        Last updated: {lastUpdated || "Loading..."}
      </div>
    </div>
  );
};

function DraftTab({
  status,
  recentPicks,
}: {
  status: DraftStatus | null;
  recentPicks: RecentPick[];
}) {
  return (
    <div className="space-y-3">
      <div className="rounded-2xl border border-yellow-400 bg-yellow-300 p-4 text-slate-950 shadow">
        <div className="text-xs font-black uppercase">On the Clock</div>

        {status?.status === "PRE_RACE" ? (
          <>
            <div className="mt-2 text-2xl font-black">Draft Completed</div>

            <a
              href="/race"
              className="mt-3 inline-block w-full rounded-lg bg-blue-500 px-4 py-2 text-center text-sm font-bold text-white"
            >
              View Standings
            </a>
          </>
        ) : (
          <>
            <div className="mt-1 text-3xl font-black">
              {status?.current_picker || "Waiting"}
            </div>

            <div className="mt-1 text-sm font-semibold">
              Pick #{(status?.total_picks ?? 0) + 1}
            </div>
          </>
        )}
      </div>

      {status?.status !== "PRE_RACE" && (
        <Section title="On Deck">
          {(status?.on_deck ?? []).slice(0, 5).map((name, index) => {
            const currentPick = (status?.total_picks ?? 0) + 1;
            const pickNumber = currentPick + index + 1;

            return (
              <div
                key={`${name}-${index}`}
                className="flex items-center justify-between rounded-lg bg-slate-800 px-3 py-2"
              >
                <span className="text-sm font-semibold">
                  {pickNumber} - {name}
                </span>
              </div>
            );
          })}
        </Section>
      )}

      <Section title="Last 5 Picks">
        {recentPicks.length === 0 && (
          <div className="text-sm text-slate-400">No picks yet.</div>
        )}

        {recentPicks.map((pick) => (
          <div
            key={pick.pick_number}
            className="rounded-xl bg-slate-800 p-3 shadow"
          >
            <div className="text-xs text-slate-400">Pick {pick.pick_number}</div>

            <div className="mt-1 text-lg font-bold">
              #{pick.car_number} {pick.driver_name}
            </div>

            <div className="mt-1 text-sm text-slate-300">
              {pick.participant}
            </div>
          </div>
        ))}
      </Section>
    </div>
  );
}

function FieldTab({
  drivers,
  savedDriverNumbers,
  onToggleSavedDriver,
}: {
  drivers: Driver[];
  savedDriverNumbers: string[];
  onToggleSavedDriver: (driverNumber: string) => void;
}) {
  return (
    <div className="grid grid-cols-3 gap-2">
      {drivers.map((driver) => (
        <DriverMiniCard
          key={driver.number}
          driver={driver}
          isSaved={savedDriverNumbers.includes(driver.number)}
          onToggleSavedDriver={onToggleSavedDriver}
        />
      ))}
    </div>
  );
}

function AvailableTab({
  drivers,
  savedDriverNumbers,
  onToggleSavedDriver,
}: {
  drivers: Driver[];
  savedDriverNumbers: string[];
  onToggleSavedDriver: (driverNumber: string) => void;
}) {
  return (
    <div className="space-y-2">
      {drivers.map((driver) => (
        <DriverListCard
          key={driver.number}
          driver={driver}
          isSaved={savedDriverNumbers.includes(driver.number)}
          onToggleSavedDriver={onToggleSavedDriver}
        />
      ))}
    </div>
  );
}

function SavedTab({
  drivers,
  savedDriverCount,
  savedAvailableDriverCount,
  savedDriverNumbers,
  onToggleSavedDriver,
  onClearSavedDrivers,
}: {
  drivers: Driver[];
  savedDriverCount: number;
  savedAvailableDriverCount: number;
  savedDriverNumbers: string[];
  onToggleSavedDriver: (driverNumber: string) => void;
  onClearSavedDrivers: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="rounded-xl bg-slate-900 p-3 text-sm text-slate-300 shadow">
        Showing {savedAvailableDriverCount} available saved driver
        {savedAvailableDriverCount === 1 ? "" : "s"}.
      </div>

      {drivers.length === 0 && (
        <div className="rounded-xl bg-slate-900 p-4 text-sm text-slate-400 shadow">
          No saved drivers are currently available.
        </div>
      )}

      <div className="space-y-2">
        {drivers.map((driver) => (
          <DriverListCard
            key={driver.number}
            driver={driver}
            isSaved={savedDriverNumbers.includes(driver.number)}
            onToggleSavedDriver={onToggleSavedDriver}
          />
        ))}
      </div>

      {savedDriverCount > 0 && (
        <button
          onClick={onClearSavedDrivers}
          className="w-full rounded-xl border border-red-400 bg-red-500/10 px-4 py-3 text-sm font-bold text-red-300"
        >
          Clear Saved
        </button>
      )}
    </div>
  );
}

function TeamsTab({
  participants,
  assignments,
}: {
  participants: string[];
  assignments: PoolAssignments;
}) {
  return (
    <div className="space-y-3">
      {participants.map((participant) => {
        const drivers = assignments[participant] ?? [];

        return (
          <Section key={participant} title={`${participant} (${drivers.length})`}>
            {drivers.length === 0 && (
              <div className="text-sm text-slate-400">No drivers yet.</div>
            )}

            {drivers.map((driver) => (
              <div
                key={driver.number}
                className="rounded-lg bg-slate-800 px-3 py-2 text-sm"
              >
                #{driver.number} {driver.name}
              </div>
            ))}
          </Section>
        );
      })}
    </div>
  );
}

function DriverMiniCard({
  driver,
  isSaved,
  onToggleSavedDriver,
}: {
  driver: Driver;
  isSaved: boolean;
  onToggleSavedDriver: (driverNumber: string) => void;
}) {
  const taken = Boolean(driver.takenBy);

  return (
    <div
      className={`relative rounded-xl p-2 text-center shadow ${
        taken ? "bg-slate-800 opacity-50" : "bg-slate-900"
      }`}
    >
      <button
        type="button"
        onClick={() => onToggleSavedDriver(driver.number)}
        className={`absolute right-1 top-1 rounded-full px-1.5 py-0.5 text-sm font-black ${
          isSaved ? "text-yellow-300" : "text-slate-500"
        }`}
        aria-label={isSaved ? "Unsave driver" : "Save driver"}
      >
        ★
      </button>

      <div className="text-xs font-bold text-slate-400">
        P{driver.starting_position}
      </div>

      {driver.car_image_url && (
        <img
          src={`${API_BASE}/${driver.car_image_url.replace(/^\/+/, "")}`}
          alt={driver.name}
          className="mx-auto my-1 h-10 object-contain"
        />
      )}

      <div className="text-sm font-black">#{driver.number}</div>
      <div className="truncate text-xs">{driver.name}</div>

      {taken && (
        <div className="mt-1 truncate rounded bg-slate-700 px-1 py-0.5 text-[10px]">
          {driver.takenBy}
        </div>
      )}
    </div>
  );
}

function DriverListCard({
  driver,
  isSaved,
  onToggleSavedDriver,
}: {
  driver: Driver;
  isSaved: boolean;
  onToggleSavedDriver: (driverNumber: string) => void;
}) {
  return (
    <div className="rounded-xl bg-slate-900 p-3 shadow">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-bold">
            #{driver.number} {driver.name}
          </div>
          <div className="text-xs text-slate-400">
            Starting P{driver.starting_position}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="rounded-full bg-green-500 px-2 py-1 text-xs font-bold text-slate-950">
            Available
          </div>

          <button
            type="button"
            onClick={() => onToggleSavedDriver(driver.number)}
            className={`rounded-full px-2 py-1 text-lg font-black ${
              isSaved ? "text-yellow-300" : "text-slate-500"
            }`}
            aria-label={isSaved ? "Unsave driver" : "Save driver"}
          >
            ★
          </button>
        </div>
      </div>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl bg-slate-900 p-3 shadow">
      <h2 className="mb-2 text-sm font-bold uppercase text-slate-300">
        {title}
      </h2>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

export default DraftCompanionScreen;