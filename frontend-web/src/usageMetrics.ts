export function remainingQuota(used: number | null, status: string): number | null {
  if (status !== "available" || used == null || !Number.isFinite(used)) return null;
  return 100 - Math.max(0, Math.min(100, used));
}

export function derivedInputTokens(input: number | null, cached: number | null): {
  fresh: number | null;
  cachedPercent: number | null;
} {
  if (input == null || cached == null || !Number.isFinite(input) || !Number.isFinite(cached) ||
    input < 0 || cached < 0 || cached > input) {
    return { fresh: null, cachedPercent: null };
  }
  return { fresh: input - cached, cachedPercent: input === 0 ? null : cached / input * 100 };
}
