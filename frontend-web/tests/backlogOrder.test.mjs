import assert from "node:assert/strict";
import test from "node:test";
import { focusBacklogHandle, keyboardMove, moveItem } from "../src/backlogOrder.ts";

const items = [{ id: "a", priority: "Low" }, { id: "b", priority: "High" }, { id: "c", priority: "Medium" }];

test("pointer and keyboard moves preserve priority labels", () => {
  const moved = moveItem(items, "a", "c");
  assert.deepEqual(moved.map((item) => item.id), ["b", "c", "a"]);
  assert.deepEqual(moved.map((item) => item.priority), ["High", "Medium", "Low"]);
  assert.deepEqual(items.map((item) => item.id), ["a", "b", "c"]);
  assert.deepEqual(moveItem(moved, "a", "c").map((item) => item.id), ["b", "a", "c"]);
});

test("invalid or unchanged targets leave order untouched", () => {
  assert.equal(moveItem(items, "a", "a"), items);
  assert.equal(moveItem(items, "missing", "a"), items);
});

test("successive arrow moves keep the moved handle as the keyboard target", () => {
  let focused = null;
  const handles = items.map((item) => ({
    dataset: { backlogHandleId: item.id },
    focus() { focused = item.id; },
  }));
  const container = { querySelectorAll: () => handles };
  let order = items;
  for (const expected of [["b", "a", "c"], ["b", "c", "a"]]) {
    order = keyboardMove(order, "a", "ArrowDown");
    assert.deepEqual(order.map((item) => item.id), expected);
    focusBacklogHandle(container, "a");
    assert.equal(focused, "a");
  }
  assert.equal(keyboardMove(order, "a", "ArrowDown"), null);
  assert.equal(keyboardMove(order, "a", "Enter"), null);
});
