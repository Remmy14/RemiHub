import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  deleteBacklogItem, getBacklogItem, listBacklogItems, saveBacklogItem,
  sendBacklogToPlanning,
} from "./api/agentApi";
import type { BacklogItem, BacklogPriority } from "./api/agentApi";
import {
  confirmBacklogDiscard, confirmBacklogPromotion, editorFromItem,
  hasUnsavedBacklogChanges, promoteBacklogItem, saveBacklogEditor,
} from "./backlogFlow";
import type { BacklogEditor } from "./backlogFlow";

const emptyEditor: BacklogEditor = { title: "", prompt: "", priority: "Medium" };

export default function AgentBacklog({ onOpenCard, onDirtyChange }: {
  onOpenCard: (id: string) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [items, setItems] = useState<BacklogItem[]>([]);
  const [order, setOrder] = useState<"priority" | "created_at">("priority");
  const [editor, setEditor] = useState<BacklogEditor | null>(null);
  const [savedEditor, setSavedEditor] = useState<BacklogEditor | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = hasUnsavedBacklogChanges(editor, savedEditor);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try { setItems(await listBacklogItems(order)); setError(null); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to load backlog."); }
    finally { setLoading(false); }
  }, [order]);
  useEffect(() => { void refresh(); }, [refresh]);

  const closeEditor = () => {
    if (!confirmBacklogDiscard(dirty, window.confirm)) return;
    setEditor(null);
    setSavedEditor(null);
  };

  const edit = async (id: string) => {
    if (busy || !confirmBacklogDiscard(dirty, window.confirm)) return;
    setBusy(true);
    try {
      const item = await getBacklogItem(id);
      const next = editorFromItem(item);
      setEditor(next);
      setSavedEditor(next);
      setError(null);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to open item."); }
    finally { setBusy(false); }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!editor || busy) return;
    setBusy(true);
    try {
      await saveBacklogEditor(editor, saveBacklogItem);
      setEditor(null);
      setSavedEditor(null);
      await refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to save item."); }
    finally { setBusy(false); }
  };

  const remove = async (item: BacklogItem) => {
    if (busy || (editor?.id === item.id && !confirmBacklogDiscard(dirty, window.confirm)) ||
        !window.confirm(`Delete backlog item “${item.title}”?`)) return;
    setBusy(true);
    try {
      await deleteBacklogItem(item.id);
      if (editor?.id === item.id) { setEditor(null); setSavedEditor(null); }
      await refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to delete item."); }
    finally { setBusy(false); }
  };

  const promote = async (item: BacklogItem) => {
    if (busy || !confirmBacklogPromotion(item.title, dirty, window.confirm)) return;
    setBusy(true);
    try {
      await promoteBacklogItem(item, sendBacklogToPlanning,
        (id) => setItems((current) => current.filter((entry) => entry.id !== id)), onOpenCard);
      setEditor(null);
      setSavedEditor(null);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Unable to send item to Planning."); }
    finally { setBusy(false); }
  };

  return <section className="space-y-4" aria-label="Agent backlog">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 className="text-xl font-black text-slate-950">Backlog</h2>
        <p className="text-sm text-slate-600">Saved requests stay inactive until you send one to Planning.</p>
      </div>
      <button type="button" disabled={busy} onClick={() => {
        if (!confirmBacklogDiscard(dirty, window.confirm)) return;
        setEditor({ ...emptyEditor }); setSavedEditor({ ...emptyEditor });
      }} className="rounded-md bg-slate-950 px-4 py-2 text-sm font-bold text-white disabled:opacity-50">New Backlog Item</button>
    </div>
    {error && <div role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {editor && <form onSubmit={(event) => void save(event)} className="space-y-3 rounded-md border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="font-bold">{editor.id ? "Edit Backlog Item" : "New Backlog Item"}</h3>
      <label className="block text-sm font-semibold">Title
        <input required value={editor.title} onChange={(event) => setEditor({ ...editor, title: event.target.value })}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base" />
      </label>
      {editor.title.length > 160 && <p role="alert" className="text-sm text-red-700">Title must be at most 160 characters.</p>}
      <label className="block text-sm font-semibold">Full prompt
        <textarea required value={editor.prompt} onChange={(event) => setEditor({ ...editor, prompt: event.target.value })}
          className="mt-1 min-h-64 w-full rounded-md border border-slate-300 px-3 py-2 text-base" />
      </label>
      <div className="text-right text-xs text-slate-500">{editor.prompt.length.toLocaleString()} / 20,000 characters</div>
      {editor.prompt.length > 20000 && <p role="alert" className="text-sm text-red-700">Prompt must be at most 20,000 characters.</p>}
      <label className="block text-sm font-semibold">Priority
        <select value={editor.priority} onChange={(event) => setEditor({ ...editor, priority: event.target.value as BacklogPriority })}
          className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base">
          <option>High</option><option>Medium</option><option>Low</option>
        </select>
      </label>
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={busy || !editor.title.trim() || !editor.prompt.trim() || editor.title.length > 160 || editor.prompt.length > 20000}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-bold text-white disabled:opacity-50">{busy ? "Saving…" : "Save to Backlog"}</button>
        <button type="button" onClick={closeEditor} disabled={busy} className="rounded-md border border-slate-300 px-4 py-2 text-sm font-bold">Cancel</button>
      </div>
    </form>}
    <label className="flex items-center gap-2 text-sm font-semibold">Sort by
      <select value={order} onChange={(event) => setOrder(event.target.value as "priority" | "created_at")}
        className="rounded-md border border-slate-300 bg-white px-3 py-2">
        <option value="priority">Priority</option><option value="created_at">Newest first</option>
      </select>
    </label>
    {loading ? <p className="text-sm text-slate-600">Loading backlog…</p> : items.length === 0 ?
      <div className="rounded-md border border-slate-200 bg-white p-6 text-center shadow-sm">
        <p className="mb-3 text-sm text-slate-600">No saved backlog items yet.</p>
        <button type="button" onClick={() => {
          if (!confirmBacklogDiscard(dirty, window.confirm)) return;
          setEditor({ ...emptyEditor }); setSavedEditor({ ...emptyEditor });
        }}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-bold text-white">New Backlog Item</button>
      </div> : <div className="grid gap-3 md:grid-cols-2">
        {items.map((item) => <article key={item.id} className="min-w-0 rounded-md border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex items-start justify-between gap-2">
            <h3 className="font-bold text-slate-950">{item.title}</h3>
            <span className="rounded bg-slate-100 px-2 py-1 text-xs font-bold">{item.priority}</span>
          </div>
          <p className="mt-1 text-xs text-slate-500">Updated {new Date(item.updated_at).toLocaleDateString()}</p>
          <p className="mt-3 line-clamp-3 whitespace-pre-wrap text-sm text-slate-700">{item.prompt}</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" disabled={busy} onClick={() => void edit(item.id)} className="rounded border px-3 py-2 text-sm font-semibold">Edit</button>
            <button type="button" disabled={busy} onClick={() => void remove(item)} className="rounded border px-3 py-2 text-sm font-semibold">Delete</button>
            <button type="button" disabled={busy} onClick={() => void promote(item)} className="rounded bg-blue-600 px-3 py-2 text-sm font-bold text-white disabled:opacity-50">{busy ? "Working…" : "Send to Planning"}</button>
          </div>
        </article>)}
      </div>}
  </section>;
}
