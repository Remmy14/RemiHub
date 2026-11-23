import assert from "node:assert/strict";
import test from "node:test";
import {
  confirmBacklogDiscard, confirmBacklogPromotion, editorFromItem,
  hasUnsavedBacklogChanges, promoteBacklogItem, saveBacklogEditor,
} from "../src/backlogFlow.ts";

const item = {
  id: "draft-1", title: "Original title", prompt: "  First line\n\nSecond line\n",
  priority: "Medium", created_by: "admin", promoted_card_id: null,
  promoted_at: null, created_at: "2026-11-23T00:00:00Z", updated_at: "2026-11-23T00:00:00Z",
};

test("editing reopens the complete prompt and saving sends only the draft API payload", async () => {
  const saved = editorFromItem(item);
  const edited = { ...saved, title: "Edited title", prompt: `${saved.prompt}Third line`, priority: "High" };
  assert.equal(saved.prompt, item.prompt);
  assert.equal(hasUnsavedBacklogChanges(saved, saved), false);
  assert.equal(hasUnsavedBacklogChanges(edited, saved), true);
  const calls = [];
  const result = await saveBacklogEditor(edited, async (values, id) => {
    calls.push({ values, id });
    return { ...item, ...values };
  });
  assert.deepEqual(calls, [{
    values: { title: "Edited title", prompt: "  First line\n\nSecond line\nThird line", priority: "High" },
    id: "draft-1",
  }]);
  assert.equal(result.prompt, edited.prompt);
});

test("unsaved changes block navigation when discard is declined", () => {
  let prompts = 0;
  assert.equal(confirmBacklogDiscard(true, () => { prompts++; return false; }), false);
  assert.equal(prompts, 1);
  assert.equal(confirmBacklogDiscard(false, () => { throw Error("unexpected prompt"); }), true);
});

test("promotion requires confirmation and opens exactly the returned card", async () => {
  const prompts = [];
  assert.equal(confirmBacklogPromotion(item.title, false, (message) => {
    prompts.push(message); return true;
  }), true);
  assert.match(prompts[0], /creates an active Agent card/);
  assert.match(prompts[0], /Planning/);
  assert.equal(confirmBacklogPromotion(item.title, true, () => false), false);

  const actions = [];
  await promoteBacklogItem(item, async (id) => {
    actions.push(["send", id]); return "card-1";
  }, (id) => actions.push(["remove", id]), (id) => actions.push(["open", id]));
  assert.deepEqual(actions, [["send", "draft-1"], ["remove", "draft-1"], ["open", "card-1"]]);
});

test("failed promotion retains the draft and does not navigate", async () => {
  const actions = [];
  await assert.rejects(promoteBacklogItem(item, async () => { throw Error("conflict"); },
    (id) => actions.push(["remove", id]), (id) => actions.push(["open", id])), /conflict/);
  assert.deepEqual(actions, []);
});
