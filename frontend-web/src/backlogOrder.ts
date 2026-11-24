export function moveItem<T extends { id: string }>(items: T[], sourceId: string, targetId: string): T[] {
  const from = items.findIndex((item) => item.id === sourceId);
  const to = items.findIndex((item) => item.id === targetId);
  if (from < 0 || to < 0 || from === to) return items;
  const next = [...items];
  next.splice(to, 0, ...next.splice(from, 1));
  return next;
}

export function keyboardMove<T extends { id: string }>(items: T[], id: string, key: string): T[] | null {
  if (key !== "ArrowUp" && key !== "ArrowDown") return null;
  const index = items.findIndex((item) => item.id === id);
  if (index < 0) return null;
  const target = items[index + (key === "ArrowUp" ? -1 : 1)];
  return target ? moveItem(items, id, target.id) : null;
}

export function focusBacklogHandle(
  container: Pick<HTMLElement, "querySelectorAll"> | null,
  id: string,
): void {
  const handles = container?.querySelectorAll<HTMLButtonElement>("[data-backlog-handle-id]");
  for (const handle of handles ?? []) {
    if (handle.dataset.backlogHandleId === id) {
      handle.focus();
      return;
    }
  }
}
