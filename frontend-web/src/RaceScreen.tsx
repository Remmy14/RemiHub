import React, { useEffect, useState } from 'react';

type DriverEntry = {
  name: string;
  number: string;
  position: number;
};

type StandingEntry = {
  name: string;
  average_position: number;
  drivers: DriverEntry[];
};

type PoolSummary = {
  id: number;
  name: string;
  participantCount: number;
};

const RaceScreen: React.FC = () => {
  const [standings, setStandings] = useState<StandingEntry[]>([]);
  const [updatedAt, setUpdatedAt] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [pools, setPools] = useState<PoolSummary[]>([]);
  const [selectedPool, setSelectedPool] = useState<PoolSummary | null>(null);

  const fetchLeaderboard = async (poolId: number) => {
    try {
      const res = await fetch(`https://remillard.duckdns.org/race/getLeaderboard?pool_id=${poolId}`);
      const data = await res.json();

      if (data.success && Array.isArray(data.standings)) {
        setStandings(data.standings);
        setUpdatedAt(data.updatedAt);
        setError('');
      } else {
        setStandings([]);
        setUpdatedAt(data.updatedAt);
        setError(data.message || 'Leaderboard is not available.');
      }
    } catch (err) {
      console.error('Fetch error:', err);
      setError('Failed to fetch leaderboard.');
      setStandings([]);
    }
  };

  // Load pool list once
  useEffect(() => {
    const loadPools = async () => {
      try {
        const res = await fetch('https://remillard.duckdns.org/race/getPools');
        const data = await res.json();
        if (Array.isArray(data)) {
          setPools(data);
          setSelectedPool(data[0]); // Default to first
        }
      } catch (err) {
        console.error('Failed to load pools', err);
      }
    };

    loadPools();
  }, []);

  // Load leaderboard anytime pool changes
  useEffect(() => {
    if (!selectedPool) return;

    fetchLeaderboard(selectedPool.id);
    const interval = setInterval(() => fetchLeaderboard(selectedPool.id), 30000);
    return () => clearInterval(interval);
  }, [selectedPool]);

  return (
      <div className="p-4 max-w-3xl mx-auto font-sans text-gray-800">
        <h1 className="text-2xl font-bold mb-2">Indy 500 Pool Standings</h1>

        <div className="flex justify-between items-center mb-4 flex-wrap gap-2">
          <div className="flex items-center">
            <label className="font-semibold text-gray-700 mr-2">Pool:</label>
            <div className="relative inline-block">
              <select
                  value={selectedPool?.id ?? ''}
                  onChange={(e) => {
                    const poolId = parseInt(e.target.value);
                    const selected = pools.find((p) => p.id === poolId) || null;
                    setSelectedPool(selected);
                  }}
                  className="appearance-none rounded-md border border-gray-300 bg-white py-2 pl-3 pr-8 text-sm text-gray-700 shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-200"
              >
                {pools.map((pool) => (
                    <option key={pool.id} value={pool.id}>
                      {pool.name}
                    </option>
                ))}
              </select>
              <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center pr-2 text-gray-400">
                <svg
                    className="h-4 w-4"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    aria-hidden="true"
                >
                  <path
                      fillRule="evenodd"
                      d="M5.23 7.21a.75.75 0 011.06.02L10 10.94l3.71-3.71a.75.75 0 111.06 1.06l-4.24 4.25a.75.75 0 01-1.06 0L5.25 8.27a.75.75 0 01-.02-1.06z"
                      clipRule="evenodd"
                  />
                </svg>
              </div>
            </div>
          </div>

          <div className="text-sm text-gray-500">
            Last updated: {updatedAt ? new Date(updatedAt).toLocaleTimeString() : 'Loading...'}
          </div>
        </div>

        {error && (
            <div className="text-red-500 italic text-center mb-4">{error}</div>
        )}

        {standings.length > 0 &&
            standings.map((entry, index) => (
                <div
                    key={entry.name}
                    className={`rounded-xl shadow-md p-4 mb-4 border ${
                        entry.drivers.some((driver) => driver.position === 1)
                            ? 'bg-yellow-100 border-yellow-300'
                            : 'bg-white border-gray-200'
                    }`}
                >
                  <div className="flex justify-between items-center mb-2">
                    <h2 className="text-lg font-semibold">
                      {index + 1} - {entry.name}
                    </h2>
                    <span className="text-sm bg-blue-100 text-blue-800 px-2 py-1 rounded-full">
                Avg Pos: {entry.average_position.toFixed(2)}
              </span>
                  </div>
                  <ul className="pl-4 list-disc text-sm text-gray-700">
                    {entry.drivers.map((driver) => (
                        <li key={driver.number}>
                          <span className="font-bold">{driver.position}</span> - #
                          {driver.number} {driver.name}
                        </li>
                    ))}
                  </ul>
                </div>
            ))}
      </div>
  );
};

export default RaceScreen;
