import clsx from "clsx";
import {
  Bot, ChevronRight, Download, Eye, EyeOff, File, FilePlus2, FileCode2, FileImage, FileSpreadsheet, FileText, Folder, FolderOpen, FolderPlus, MessageSquare,
  PencilLine, RefreshCw, Save, Search, TextCursorInput, Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { FolderPicker } from "../components/FolderPicker";
import { LookPanel, LOOKABLE, RichPreview, richKind } from "../components/FilePreview";
import { Markdown } from "../components/Messages";
import { act, ago, CodeEditor, ConfirmButton, Empty, Field, Modal, toast } from "../components/ui";
import { api, ApiError, type FileContent, type FileEntry } from "../lib/api";
import { useLive, useStore } from "../lib/store";

const IMAGE = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i;
const CODE = /\.(py|ts|tsx|js|jsx|rs|go|java|c|cc|cpp|h|rb|php|sh|toml|ya?ml|json|css|scss|html|sql|lua|kt|swift)$/i;

function icon(e: FileEntry, open: boolean) {
  if (e.dir) return open ? FolderOpen : Folder;
  if (IMAGE.test(e.name)) return FileImage;
  if (CODE.test(e.name)) return FileCode2;
  if (/\.(md|txt|rst|pdf|docx?|pptx?|odt|rtf|epub)$/i.test(e.name)) return FileText;
  if (/\.(csv|tsv|xlsx?|xlsm|ods|db|sqlite3?)$/i.test(e.name)) return FileSpreadsheet;
  return File;
}

function size(n: number) {
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
}

export function FilesView({ onDiscuss }: { onDiscuss: (text: string) => void }) {
  const { project, current, projects, setProject, refreshProjects } = useStore();
  const [tree, setTree] = useState<Record<string, FileEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set([""]));
  const [hidden, setHidden] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<FileContent | null>(null);
  const [draft, setDraft] = useState("");
  const [preview, setPreview] = useState(false);
  const [touched, setTouched] = useState<Record<string, number>>({}); // path → when the agent last wrote it
  const [changedOnDisk, setChangedOnDisk] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FileEntry[] | null>(null);
  const [dialog, setDialog] = useState<null | { kind: "file" | "folder" | "rename"; base: string }>(null);
  const [drag, setDrag] = useState(false);
  const [picking, setPicking] = useState(false);
  const uploadRef = useRef<HTMLInputElement>(null);
  const dirty = !!file && !file.binary && draft !== file.content;

  const loadDir = useCallback(async (path: string) => {
    try {
      const r = await api.files(project, path);
      setTree((t) => ({ ...t, [path]: r.entries }));
    } catch (e) {
      if (path === "") toast((e as Error).message, "bad");
    }
  }, [project]);

  useEffect(() => {
    setTree({});
    setExpanded(new Set([""]));
    setSelected(null);
    setFile(null);
    void loadDir("");
  }, [project, loadDir]);

  const refresh = () => [...expanded].forEach((p) => void loadDir(p));

  const openFile = useCallback(async (path: string) => {
    setSelected(path);
    setChangedOnDisk(false);
    try {
      const f = await api.readFile(project, path);
      setFile(f);
      setDraft(f.content ?? "");
      const rich = richKind(path);
      setPreview(/\.(md|html?)$/i.test(path) || IMAGE.test(path) || f.binary || rich === "table" || rich === "document" || rich === "media");
    } catch (e) {
      toast((e as Error).message, "bad");
    }
  }, [project]);

  const toggle = (e: FileEntry) => {
    if (!e.dir) return void openFile(e.path);
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(e.path)) n.delete(e.path);
      else {
        n.add(e.path);
        if (!tree[e.path]) void loadDir(e.path);
      }
      return n;
    });
  };

  useLive(["file_changed"], (m) => {
    if (m.project !== project) return;
    const path = String(m.path);
    if (m.by === "agent") setTouched((t) => ({ ...t, [path]: Date.now() }));
    const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    if (tree[parent] || parent === "") void loadDir(parent);
    if (path === selected && m.by === "agent") {
      if (dirty) setChangedOnDisk(true);
      else void openFile(path);
    }
  });

  const save = async (force = false) => {
    if (!file || !selected || !dirty) return;
    try {
      const e = await api.writeFile(project, selected, draft, force ? undefined : file.mtime, force);
      setFile({ ...file, content: draft, mtime: e.mtime, size: e.size });
      setChangedOnDisk(false);
      toast(`Saved ${selected}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setChangedOnDisk(true);
      else toast((e as Error).message, "bad");
    }
  };

  useEffect(() => {
    if (!query.trim()) return setResults(null);
    const t = setTimeout(() => api.searchFiles(project, query).then(setResults).catch(() => undefined), 200);
    return () => clearTimeout(t);
  }, [query, project]);

  const selectedDir = useMemo(() => {
    if (!selected) return "";
    const isDir = Object.values(tree).flat().find((e) => e.path === selected)?.dir;
    return isDir ? selected : selected.includes("/") ? selected.slice(0, selected.lastIndexOf("/")) : "";
  }, [selected, tree]);

  const upload = async (files: File[]) => {
    if (!files.length) return;
    const r = await act(() => api.uploadFiles(project, selectedDir, files), `Uploaded ${files.length} file(s)`);
    if (r) void loadDir(selectedDir);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    void upload(Array.from(e.dataTransfer.files));
  };

  const Row = ({ e, depth }: { e: FileEntry; depth: number }) => {
    const open = expanded.has(e.path);
    const Icon = icon(e, open);
    const agentTouched = touched[e.path] && Date.now() - touched[e.path] < 10 * 60 * 1000;
    return (
      <>
        <div onClick={() => toggle(e)} style={{ paddingLeft: 8 + depth * 14 }}
          className={clsx("group flex cursor-pointer items-center gap-1.5 rounded-md py-[3px] pr-2 text-[13px]",
            selected === e.path ? "bg-raised text-ink" : "text-muted hover:bg-raised/60 hover:text-ink", e.hidden && "opacity-60")}>
          {e.dir ? <ChevronRight size={12} className={clsx("shrink-0 text-faint transition", open && "rotate-90")} /> : <span className="w-3 shrink-0" />}
          <Icon size={14} className={clsx("shrink-0", e.dir ? "text-brand/80" : "text-faint")} />
          <span className="min-w-0 flex-1 truncate">{e.name}</span>
          {agentTouched && <span title="edited by the agent"><Bot size={12} className="shrink-0 text-brand" /></span>}
        </div>
        {e.dir && open && !e.heavy && (tree[e.path] ?? []).filter((c) => hidden || !c.hidden).map((c) => <Row key={c.path} e={c} depth={depth + 1} />)}
        {e.dir && open && e.heavy && <div style={{ paddingLeft: 30 + depth * 14 }} className="py-0.5 text-[11.5px] text-faint">(not expanded: large generated folder)</div>}
      </>
    );
  };

  const pickFolder = async (path: string) => {
    setPicking(false);
    const home = path.replace(/\/+$/, "");
    const known = projects.find((p) => p.path.replace(/\/+$/, "") === home);
    if (known) return setProject(known.id);
    const p = await act(() => api.addProject({ path }), `Opened ${path} as a new project`);
    if (p) {
      await refreshProjects();
      setProject(p.id);
    }
  };

  const isImage = selected && IMAGE.test(selected);
  const isHtml = selected && /\.html?$/i.test(selected);
  const isMd = selected && /\.md$/i.test(selected);
  const rich = selected ? richKind(selected) : "";

  return (
    <div className="flex h-full min-w-0 flex-1" onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={onDrop}>
      <div className={clsx("flex w-72 shrink-0 flex-col border-r border-line", drag && "bg-brand/5 ring-2 ring-inset ring-brand/40")}>
        <div className="flex h-14 shrink-0 items-center gap-1 border-b border-line px-3">
          <div className="min-w-0 flex-1 cursor-pointer" title="Change folder" onClick={() => setPicking(true)}>
            <div className="truncate text-sm font-medium">{current?.name}</div>
            <div className="truncate font-mono text-[10.5px] text-faint">{current?.path.replace(/^\/home\/[^/]+/, "~")}</div>
          </div>
          <button className="btn-ghost px-1.5" title="Change folder" onClick={() => setPicking(true)}><FolderOpen size={15} /></button>
          <button className="btn-ghost px-1.5" title="New file" onClick={() => setDialog({ kind: "file", base: selectedDir })}><FilePlus2 size={15} /></button>
          <button className="btn-ghost px-1.5" title="New folder" onClick={() => setDialog({ kind: "folder", base: selectedDir })}><FolderPlus size={15} /></button>
          <button className="btn-ghost px-1.5" title="Upload (or drop files here)" onClick={() => uploadRef.current?.click()}><Upload size={15} /></button>
          <input ref={uploadRef} type="file" multiple hidden onChange={(e) => { void upload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
        </div>
        <div className="flex items-center gap-1 border-b border-line/60 px-2 py-1.5">
          <div className="relative flex-1">
            <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-faint" />
            <input className="input h-7 py-0 pl-7 text-[12.5px]" placeholder="Find file…" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <button className="btn-ghost px-1.5" title={hidden ? "Hide dotfiles" : "Show dotfiles"} onClick={() => setHidden((h) => !h)}>{hidden ? <EyeOff size={14} /> : <Eye size={14} />}</button>
          <button className="btn-ghost px-1.5" title="Refresh" onClick={refresh}><RefreshCw size={13} /></button>
        </div>
        <div className="flex-1 overflow-y-auto px-1.5 py-1.5">
          {results ? (
            results.length ? results.map((e) => (
              <div key={e.path} onClick={() => (e.dir ? setQuery("") : openFile(e.path))}
                className={clsx("cursor-pointer rounded-md px-2 py-1 text-[12.5px] hover:bg-raised", selected === e.path && "bg-raised")}>
                <div className="truncate">{e.name}</div>
                <div className="truncate font-mono text-[10.5px] text-faint">{e.path}</div>
              </div>
            )) : <p className="px-2 py-2 text-[12.5px] text-faint">No matches.</p>
          ) : (tree[""] ?? []).filter((c) => hidden || !c.hidden).map((e) => <Row key={e.path} e={e} depth={0} />)}
          {!results && tree[""]?.length === 0 && <p className="px-2 py-2 text-[12.5px] text-faint">Empty folder. Drop files here or ask the agent to create some.</p>}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        {!file || !selected ? (
          <Empty icon={<Folder size={18} />} title="Pick a file">
            Browse and edit the project's files. Files the agent changes are marked <Bot size={12} className="inline text-brand" /> and refresh live.
          </Empty>
        ) : (
          <>
            <div className="flex h-14 shrink-0 items-center gap-2 border-b border-line px-4">
              <div className="min-w-0 flex-1">
                <div className="truncate font-mono text-[13px]">{selected}</div>
                <div className="text-[11px] text-faint">{size(file.size)}{file.mtime ? ` · modified ${ago(file.mtime)}` : ""}{dirty ? " · unsaved" : ""}</div>
              </div>
              {(isMd || isHtml || rich) && !file.binary && (
                <button className="btn-ghost px-2" onClick={() => setPreview((p) => !p)} title={preview ? "Edit" : "Preview"}>{preview ? <PencilLine size={15} /> : <Eye size={15} />}</button>
              )}
              <button className="btn-ghost px-2" title="Discuss in a new chat" onClick={() => onDiscuss(`Look at \`${selected}\` and `)}><MessageSquare size={15} /></button>
              <button className="btn-ghost px-2" title="Rename / move" onClick={() => setDialog({ kind: "rename", base: selected })}><TextCursorInput size={15} /></button>
              <a className="btn-ghost px-2" href={api.rawUrl(project, selected, true)} title="Download"><Download size={15} /></a>
              <ConfirmButton onConfirm={async () => {
                const parent = selected.includes("/") ? selected.slice(0, selected.lastIndexOf("/")) : "";
                if (await act(() => api.deleteFile(project, selected), `Deleted ${selected}`)) {
                  setFile(null);
                  setSelected(null);
                  void loadDir(parent);
                }
              }} />
              {!file.binary && <button className="btn-primary px-3" disabled={!dirty} onClick={() => save()}><Save size={14} /> Save</button>}
            </div>
            {changedOnDisk && (
              <div className="flex items-center gap-2 border-b border-brand/30 bg-brand/10 px-4 py-1.5 text-[12.5px] text-brand">
                This file changed on disk (the agent edited it) while you had unsaved changes.
                <button className="ml-auto underline" onClick={() => openFile(selected)}>Load theirs</button>
                <button className="underline" onClick={() => save(true)}>Overwrite with mine</button>
              </div>
            )}
            <div className="min-h-0 flex-1 overflow-auto">
              {isImage ? (
                <div className="flex h-full items-center justify-center bg-[repeating-conic-gradient(rgb(var(--raised))_0%_25%,transparent_0%_50%)] bg-[length:16px_16px] p-6">
                  <img src={api.rawUrl(project, selected)} alt={selected} className="max-h-full max-w-full rounded shadow-card" />
                </div>
              ) : file.binary || (preview && rich) ? (
                <RichPreview project={project} path={selected} version={file.mtime} />
              ) : preview && isHtml ? (
                <iframe title={selected} src={api.rawUrl(project, selected)} sandbox="allow-scripts" className="h-full w-full bg-white" />
              ) : preview && isMd ? (
                <div className="mx-auto max-w-3xl px-6 py-5"><Markdown text={draft} /></div>
              ) : (
                <CodeEditor value={draft} onChange={setDraft} onSave={() => save()} className="h-full rounded-none border-0" minRows={40} />
              )}
            </div>
            {LOOKABLE.test(selected) && <LookPanel project={project} path={selected} />}
          </>
        )}
      </div>
      <FolderPicker open={picking} start={current?.path} onClose={() => setPicking(false)} onPick={(p) => void pickFolder(p)} />
      <NameDialog dialog={dialog} onClose={() => setDialog(null)} onDone={async (path) => {
        const d = dialog!;
        const parentOf = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
        if (d.kind === "folder") {
          if (await act(() => api.mkdir(project, path))) {
            void loadDir(parentOf(path));
            setExpanded((s) => new Set([...s, parentOf(path)]));
          }
        } else if (d.kind === "file") {
          if (await act(() => api.writeFile(project, path, ""))) {
            void loadDir(parentOf(path));
            setExpanded((s) => new Set([...s, parentOf(path)]));
            void openFile(path);
          }
        } else if (await act(() => api.rename(project, d.base, path), "Renamed")) {
          void loadDir(parentOf(d.base));
          void loadDir(parentOf(path));
          void openFile(path);
        }
        setDialog(null);
      }} />
    </div>
  );
}

function NameDialog({ dialog, onClose, onDone }: { dialog: null | { kind: "file" | "folder" | "rename"; base: string }; onClose: () => void; onDone: (path: string) => void }) {
  const [path, setPath] = useState("");
  useEffect(() => {
    if (dialog) setPath(dialog.kind === "rename" ? dialog.base : dialog.base ? `${dialog.base}/` : "");
  }, [dialog]);
  const title = dialog?.kind === "rename" ? "Rename or move" : dialog?.kind === "folder" ? "New folder" : "New file";
  return (
    <Modal open={!!dialog} onClose={onClose} title={title}
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!path.trim() || path.endsWith("/")} onClick={() => onDone(path.trim())}>OK</button></>}>
      <Field label="Path in the project">
        <input className="input font-mono text-[13px]" autoFocus value={path} onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && path.trim() && !path.endsWith("/") && onDone(path.trim())} />
      </Field>
    </Modal>
  );
}
