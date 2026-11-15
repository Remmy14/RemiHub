import assert from "node:assert/strict";
import test from "node:test";
import { derivedInputTokens, remainingQuota } from "../src/usageMetrics.ts";

test("remaining quota drains and clamps reported usage", () => {
  assert.equal(remainingQuota(0, "available"), 100);
  assert.equal(remainingQuota(42, "available"), 58);
  assert.equal(remainingQuota(100, "available"), 0);
  assert.equal(remainingQuota(-20, "available"), 100);
  assert.equal(remainingQuota(120, "available"), 0);
  assert.equal(remainingQuota(null, "available"), null);
  assert.equal(remainingQuota(42, "unavailable"), null);
  assert.equal(remainingQuota(Number.NaN, "available"), null);
});

test("fresh and cached input derive only from consistent telemetry", () => {
  assert.deepEqual(derivedInputTokens(100, 30), { fresh: 70, cachedPercent: 30 });
  assert.deepEqual(derivedInputTokens(0, 0), { fresh: 0, cachedPercent: null });
  assert.deepEqual(derivedInputTokens(100, null), { fresh: null, cachedPercent: null });
  assert.deepEqual(derivedInputTokens(null, 0), { fresh: null, cachedPercent: null });
  assert.deepEqual(derivedInputTokens(10, 11), { fresh: null, cachedPercent: null });
  assert.deepEqual(derivedInputTokens(10, -1), { fresh: null, cachedPercent: null });
  assert.deepEqual(derivedInputTokens(Number.NaN, 1), { fresh: null, cachedPercent: null });
});
