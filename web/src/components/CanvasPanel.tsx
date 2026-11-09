import clsx from "clsx";
import { Bot, Download, Eye, FilePlus2, History, NotebookPen, PencilLine, RotateCcw, Save, User } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type CanvasDoc, type CanvasSummary } from "../lib/api";
import { useLive, useStore } from "../lib/store";
import { Markdown } from "./Messages";
import { act, ago, CodeEditor, ConfirmButton, Empty, Field, Modal, toast } from "./ui";

const FORMATS = ["markdown", "html", "code", "text"] as const;

export function CanvasPanel() {
  const store = useStore();
  const pid = store.project;
  const [docs, setDocs] = useState<CanvasSummary[]>([]);
  const [doc, setDoc] = useState<CanvasDoc | null>(null);
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<"preview" | "edit">("preview");
  const [incoming, setIncoming] = useState(false); // the agent revised the doc while the user had unsaved edits
  const [history, setHistory] = useState(false);
  const [exporting, setExporting] = useState(false);
  const selected = store.dock.canvasId;
  const scroller = useRef<HTMLDivElement>(null);
  const dirty = !!doc && draft !== doc.content;

  const loadList = useCallback(() => api.canvas(pid).then(setDocs).catch(() => undefined), [pid]);
  useEffect(() => void loadList(), [loadList]);

  const open = useCallback(async (id: string) => {
    const d = await api.canvasDoc(id);
    setDoc(d);
    setDraft(d.content);
    setIncoming(false);
  }, []);

  useEffect(() => {
    const id = selected ?? docs[0]?.id;
    if (id && id !== doc?.id) void open(id).catch(() => store.setDock({ canvasId: null }));
    if (!id) setDoc(null);
  }, [selected, docs]); // eslint-disable-line react-hooks/exhaustive-deps

  useLive(["canvas", "canvas_deleted"], (m) => {
    if (m.project !== pid) return;
    void loadList();
    const id = m.kind === "canvas" ? (m.doc as CanvasSummary).id : (m.id as string);
    if (m.kind === "canvas_deleted" && id === doc?.id) {
      setDoc(null);
      store.setDock({ canvasId: null });
    } else if (m.kind === "canvas" && id === doc?.id && (m.doc as CanvasSummary).author === "agent") {
      if (m.streaming) {  // the agent is still writing: show the text as it arrives
        setDoc({ ...doc, title: (m.doc as CanvasSummary).title, content: String(m.content) });
        if (!dirty) setDraft(String(m.content));
      } else if (dirty) setIncoming(true);
      else void open(id);
    }
  });

  useEffect(() => {  // follow the text while the agent writes
    if (doc?.streaming && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [draft, doc?.streaming]);

  const save = async () => {
    if (!doc || !dirty) return;
    const d = await act(() => api.saveCanvas(doc.id, { content: draft, base_updated: doc.updated }));
    if (d) {
      setDoc(d);
      toast("Saved. The agent sees your edits next time it reads the document.");
    }
  };

  const create = async () => {
    const d = await act(() => api.newCanvas({ project: pid, title: "Untitled", format: "markdown", content: "# Untitled\n\n" }));
    if (d) {
      await loadList();
      store.setDock({ canvasId: d.id });
      setMode("edit");
    }
  };

  const meta = async (changes: { title?: string; format?: string }) => {
    if (!doc) return;
    const d = await act(() => api.saveCanvas(doc.id, { ...changes, content: draft }));
    if (d) {
      setDoc(d);
      void loadList();
    }
  };

  if (!doc) {
    return (
      <div className="flex h-full flex-col">
        <Empty icon={<NotebookPen size={18} />} title="No documents yet">
          When the agent writes a plan, report or draft, it opens here and you can edit it together.
          <div className="mt-4"><button onClick={create} className="btn-outline"><FilePlus2 size={14} /> New document</button></div>
        </Empty>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-line px-3 py-2">
        <select value={doc.id} onChange={(e) => store.setDock({ canvasId: e.target.value })}
          className="input h-8 min-w-0 flex-1 truncate py-0 text-[13px] font-medium">
          {docs.map((d) => <option key={d.id} value={d.id}>{d.title}</option>)}
        </select>
        <button onClick={create} className="btn-ghost px-2" title="New document"><FilePlus2 size={15} /></button>
        <div className="flex rounded-lg border border-line p-0.5">
          <button onClick={() => setMode("preview")} className={clsx("rounded-md px-2 py-1", mode === "preview" ? "bg-raised text-ink" : "text-faint")} title="Preview"><Eye size={14} /></button>
          <button onClick={() => setMode("edit")} className={clsx("rounded-md px-2 py-1", mode === "edit" ? "bg-raised text-ink" : "text-faint")} title="Edit"><PencilLine size={14} /></button>
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line/60 px-3 py-1.5 text-[11.5px] text-faint">
        <input defaultValue={doc.title} key={doc.id + doc.title} onBlur={(e) => e.target.value.trim() && e.target.value !== doc.title && meta({ title: e.target.value.trim() })}
          className="min-w-0 flex-1 bg-transparent text-[12px] text-muted outline-none focus:text-ink" aria-label="title" />
        <select value={doc.format} onChange={(e) => meta({ format: e.target.value })} className="bg-transparent text-[11.5px] outline-none">
          {FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <span className="flex items-center gap-1">{doc.author === "agent" ? <Bot size={12} /> : <User size={12} />}{doc.streaming ? "writing…" : ago(doc.updated)}</span>
        {doc.versions.length > 0 && (
          <button onClick={() => setHistory(true)} className="flex items-center gap-1 hover:text-ink"><History size={12} />{doc.versions.length}</button>
        )}
        <button onClick={() => setExporting(true)} className="hover:text-ink" title="Save as a file in the project"><Download size={13} /></button>
        <ConfirmButton onConfirm={() => act(() => api.deleteCanvas(doc.id), "Document deleted")} className="h-6 px-1" />
      </div>
      {incoming && (
        <div className="flex shrink-0 items-center gap-2 border-b border-brand/30 bg-brand/10 px-3 py-1.5 text-[12px] text-brand">
          The agent revised this document while you were editing.
          <button onClick={() => open(doc.id)} className="ml-auto underline">Load theirs</button>
          <button onClick={() => setIncoming(false)} className="underline">Keep mine</button>
        </div>
      )}
      <div ref={scroller} className="min-h-0 flex-1 overflow-auto">
        {mode === "edit" ? (
          <CodeEditor value={draft} onChange={setDraft} onSave={save} className="h-full rounded-none border-0" minRows={30} />
        ) : doc.format === "html" ? (
          <iframe title={doc.title} sandbox="allow-scripts" srcDoc={draft} className="h-full w-full bg-white" />
        ) : doc.format === "markdown" ? (
          <div className="px-5 py-4"><Markdown text={draft} /></div>
        ) : (
          <pre className="whitespace-pre-wrap px-4 py-3 font-mono text-[12.5px] leading-relaxed">{draft}</pre>
        )}
      </div>
      {dirty && (
        <div className="flex shrink-0 items-center gap-2 border-t border-line bg-panel px-3 py-2 text-[12px] text-muted">
          Unsaved changes
          <button onClick={() => setDraft(doc.content)} className="btn-ghost ml-auto px-2 py-1 text-[12px]">Discard</button>
          <button onClick={save} className="btn-primary px-2.5 py-1 text-[12px]"><Save size={13} /> Save</button>
        </div>
      )}
      <HistoryModal open={history} doc={doc} onClose={() => setHistory(false)} onRestored={(d) => { setDoc(d); setDraft(d.content); }} />
      <ExportModal open={exporting} doc={doc} onClose={() => setExporting(false)} />
    </div>
  );
}

function HistoryModal({ open, doc, onClose, onRestored }: { open: boolean; doc: CanvasDoc; onClose: () => void; onRestored: (d: CanvasDoc) => void }) {
  const [pick, setPick] = useState(doc.versions.length - 1);
  useEffect(() => setPick(doc.versions.length - 1), [doc, open]);
  const v = doc.versions[pick];
  return (
    <Modal open={open} onClose={onClose} wide title="Version history"
      footer={<button className="btn-primary" disabled={!v} onClick={async () => {
        const d = await act(() => api.restoreCanvas(doc.id, pick), "Restored (the current text was kept as a version)");
        if (d) { onRestored(d); onClose(); }
      }}><RotateCcw size={14} /> Restore this version</button>}>
      <div className="flex gap-4">
        <div className="w-44 shrink-0 space-y-1">
          {doc.versions.map((x, i) => (
            <button key={i} onClick={() => setPick(i)} className={clsx("flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[12px]", i === pick ? "bg-raised text-ink" : "text-muted hover:bg-raised/60")}>
              {x.author === "agent" ? <Bot size={12} /> : <User size={12} />} {ago(x.at)}
            </button>
          )).reverse()}
        </div>
        <pre className="max-h-[50vh] min-w-0 flex-1 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-bg p-3 font-mono text-[12px]">{v?.content}</pre>
      </div>
    </Modal>
  );
}

function ExportModal({ open, doc, onClose }: { open: boolean; doc: CanvasDoc; onClose: () => void }) {
  const ext = { markdown: "md", html: "html", code: doc.language || "txt", text: "txt" }[doc.format];
  const [path, setPath] = useState("");
  useEffect(() => setPath(`docs/${doc.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "document"}.${ext}`), [doc, ext, open]);
  return (
    <Modal open={open} onClose={onClose} title="Save to project"
      footer={<button className="btn-primary" onClick={async () => {
        const r = await act(() => api.exportCanvas(doc.id, path), `Saved ${path}`);
        if (r) onClose();
      }}><Download size={14} /> Save file</button>}>
      <Field label="Path in the project"><input className="input font-mono text-[13px]" value={path} onChange={(e) => setPath(e.target.value)} /></Field>
    </Modal>
  );
}
