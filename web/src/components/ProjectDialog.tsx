import { Archive, ArchiveRestore, FolderOpen, FolderPlus, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type Project } from "../lib/api";
import { useStore } from "../lib/store";
import { FolderPicker } from "./FolderPicker";
import { act, Busy, ConfirmButton, Field, Modal, toast } from "./ui";

const COLORS = ["#ef3340", "#9f1020", "#f97316", "#f59e0b", "#ec4899", "#a855f7", "#6366f1", "#3b82f6", "#06b6d4", "#10b981", "#84cc16"];

export function ProjectDialog({ open, onClose, project }: { open: boolean; onClose: () => void; project?: Project }) {
  const store = useStore();
  const editing = !!project;
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [color, setColor] = useState(COLORS[0]);
  const [create, setCreate] = useState(false);
  const [naming, setNaming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [picking, setPicking] = useState(false);

  useEffect(() => {
    if (!open) return;
    setPath(project?.path ?? "");
    setName(project?.name ?? "");
    setDescription(project?.description ?? "");
    setInstructions(project?.instructions ?? "");
    setColor(project?.color || COLORS[Math.floor(Math.random() * COLORS.length)]);
    setCreate(false);
    setError("");
  }, [open, project]);

  const suggest = async () => {
    if (!path.trim()) return;
    setNaming(true);
    setError("");
    try {
      setName((await api.suggestName(path.trim())).name);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setNaming(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const body = { path: path.trim(), name: name.trim(), description, instructions, color, create };
      const p = editing ? await api.editProject(project!.id, body) : await api.addProject(body);
      await store.refreshProjects();
      if (!editing) store.setProject(p.id);
      toast(editing ? "Project saved" : `Project “${p.name}” created`);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!project) return;
    await act(() => api.deleteProject(project.id), "Project removed (its files were not touched)");
    await store.refreshProjects();
    onClose();
  };

  return (
    <Modal open={open} onClose={onClose} title={editing ? "Edit project" : <span className="flex items-center gap-2"><FolderPlus size={16} className="text-brand" /> New project</span>}
      footer={<>
        {editing && !project!.default && <ConfirmButton onConfirm={remove} label="Remove project" icon={false} />}
        {editing && !project!.default && (
          <button className="btn-ghost mr-auto" onClick={async () => {
            if (await act(() => api.editProject(project!.id, { archived: !project!.archived }), project!.archived ? "Project restored" : "Project archived")) {
              await store.refreshProjects();
              if (!project!.archived && project!.id === store.project) store.setProject(store.projects.find((x) => x.default)?.id ?? "");
              onClose();
            }
          }}>{project!.archived ? <><ArchiveRestore size={14} /> Restore</> : <><Archive size={14} /> Archive</>}</button>
        )}
        <button onClick={onClose} className="btn-ghost">Cancel</button>
        <button onClick={save} disabled={!path.trim() || saving} className="btn-primary"><Busy on={saving}>{editing ? "Save" : "Create project"}</Busy></button>
      </>}>
      <Field label="Folder" hint="The agent's file tools, shell and sandbox are confined to this directory.">
        <div className="flex gap-2">
        <input className="input font-mono text-[13px]" value={path} onChange={(e) => setPath(e.target.value)} onBlur={() => !editing && !name && suggest()}
          placeholder="~/code/my-app" autoFocus={!editing} />
          <button className="btn-outline shrink-0" onClick={() => setPicking(true)}><FolderOpen size={14} /> Browse</button>
        </div>
        <FolderPicker open={picking} start={path.trim() || undefined} onClose={() => setPicking(false)} onPick={(p) => { setPath(p); setPicking(false); if (!editing && !name) setNaming(false); }} />
      </Field>
      {!editing && (
        <label className="flex items-center gap-2 text-[13px] text-muted">
          <input type="checkbox" checked={create} onChange={(e) => setCreate(e.target.checked)} className="accent-[rgb(var(--brand))]" />
          Create the folder if it does not exist
        </label>
      )}
      <Field label="Name" hint={editing ? undefined : "Leave empty and the small model names it from the folder's files and README."}>
        <div className="flex gap-2">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={naming ? "naming…" : "Named automatically"} />
          <button onClick={suggest} disabled={!path.trim() || naming} className="btn-outline shrink-0" title="Ask the small model for a name">
            <Busy on={naming}><Sparkles size={14} className="text-brand" /></Busy> Suggest
          </button>
        </div>
      </Field>
      <Field label="Description">
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="One line on what this project is" />
      </Field>
      <Field label="Instructions for the agent" hint="Added to the system prompt in this project: conventions, commands, things to avoid.">
        <textarea className="input min-h-[84px] font-mono text-[12.5px]" value={instructions} onChange={(e) => setInstructions(e.target.value)}
          placeholder={"Use pnpm, not npm.\nRun `make check` before saying you're done."} />
      </Field>
      <div>
        <span className="label">Colour</span>
        <div className="flex gap-1.5">
          {COLORS.map((c) => (
            <button key={c} onClick={() => setColor(c)} className="h-6 w-6 rounded-full border-2 transition"
              style={{ background: c, borderColor: c === color ? "rgb(var(--ink))" : "transparent" }} aria-label={c} />
          ))}
        </div>
      </div>
      {error && <p className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-[13px] text-bad">{error}</p>}
    </Modal>
  );
}

export function ProjectMark({ project, size = 22 }: { project?: Pick<Project, "name" | "color">; size?: number }) {
  const letter = (project?.name ?? "?").trim().charAt(0).toUpperCase();
  return (
    <span className="inline-flex shrink-0 items-center justify-center rounded-md font-semibold text-black/80"
      style={{ width: size, height: size, fontSize: size * 0.5, background: project?.color || "rgb(var(--brand))" }}>
      {letter}
    </span>
  );
}
