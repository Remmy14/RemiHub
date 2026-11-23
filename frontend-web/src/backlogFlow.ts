import type { BacklogItem, BacklogPriority } from "./api/agentApi";

export type BacklogEditor = { id?: string; title: string; prompt: string; priority: BacklogPriority };

export function editorFromItem(item: BacklogItem): BacklogEditor {
  return { id: item.id, title: item.title, prompt: item.prompt, priority: item.priority };
}

export function hasUnsavedBacklogChanges(editor: BacklogEditor | null, saved: BacklogEditor | null): boolean {
  return editor !== null && JSON.stringify(editor) !== JSON.stringify(saved);
}

export function confirmBacklogDiscard(dirty: boolean, confirm: (message: string) => boolean): boolean {
  return !dirty || confirm("Discard unsaved backlog changes?");
}

export function confirmBacklogPromotion(
  title: string, dirty: boolean, confirm: (message: string) => boolean,
): boolean {
  return confirmBacklogDiscard(dirty, confirm) &&
    confirm(`Send “${title}” to Planning? This creates an active Agent card and starts the normal workflow.`);
}

export async function saveBacklogEditor(
  editor: BacklogEditor,
  save: (values: { title: string; prompt: string; priority: BacklogPriority }, id?: string) => Promise<BacklogItem>,
): Promise<BacklogItem> {
  return save({ title: editor.title, prompt: editor.prompt, priority: editor.priority }, editor.id);
}

export async function promoteBacklogItem(
  item: BacklogItem,
  send: (id: string) => Promise<string>,
  removeActive: (id: string) => void,
  openCard: (id: string) => void,
): Promise<void> {
  const cardId = await send(item.id);
  removeActive(item.id);
  openCard(cardId);
}
