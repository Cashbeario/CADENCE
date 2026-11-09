import clsx from "clsx";
import { Brain, Plus, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { act, ConfirmButton, Empty, KindChip, PageHeader, ago } from "../components/ui";
import { api, type MemoryItem, type Status } from "../lib/api";
import { useStore } from "../lib/store";

const KINDS = ["", "semantic", "procedural", "episodic"] as const;
const KIND_HELP: Record<string, string> = {
  semantic: "facts about the project, user, environment",
  procedural: "how-tos, conventions, lessons learned",
  episodic: "what happened: goal outcomes, events",
};

export function MemoryView() {
  const { project, projects, liveTick } = useStore();
  const [allProjects, setAllProjects] = useState(false);
  const [shared, setShared] = useState(false); // new memory: shared by every project, or just this one
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<(typeof KINDS)[number]>("");
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [stats, setStats] = useState<Status["memory"] | null>(null);
  const [fallback, setFallback] = useState("");
  const [loading, setLoading] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newKind, setNewKind] = useState("semantic");
  const [newText, setNewText] = useState("");

  const run = async (query = q) => {
    setLoading(true);
    try {
      const r = await api.memory(query, kind || undefined, allProjects ? undefined : project);
      setItems(r.items);
      setStats(r.stats as Status["memory"]);
      setFallback(r.fallback);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const t = setTimeout(() => void run(), q ? 300 : 0);
    return () => clearTimeout(t);
  }, [q, kind, project, allProjects, liveTick]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader
        title="Memory"
        subtitle={stats ? `${stats.total} memories · ${stats.semantic} semantic · ${stats.procedural} procedural · ${stats.episodic} episodic` : "Long-term memory the agent recalls by meaning"}
        actions={<button className="btn-primary" onClick={() => setAdding((a) => !a)}><Plus size={15} /> Add memory</button>}
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 px-6 py-5">
          {adding && (
            <form
              className="card space-y-3 p-4"
              onSubmit={async (e) => {
                e.preventDefault();
                await api.addMemory(newKind, newText, shared ? "" : project);
                setNewText("");
                setAdding(false);
                void run();
              }}
            >
              <div className="flex gap-1.5">
                {KINDS.filter(Boolean).map((k) => (
                  <button type="button" key={k} onClick={() => setNewKind(k)}
                    className={clsx("rounded-full border px-3 py-1 text-xs", newKind === k ? "border-brand/50 bg-brand/10 text-brand" : "border-line text-muted")}>
                    {k}
                  </button>
                ))}
                <span className="ml-2 self-center text-xs text-faint">{KIND_HELP[newKind]}</span>
              </div>
              <textarea className="input min-h-[70px]" autoFocus placeholder="e.g. Deploys go through `make release`, never push to main directly" value={newText} onChange={(e) => setNewText(e.target.value)} />
              <div className="flex items-center justify-end gap-2">
                <label className="mr-auto flex items-center gap-2 text-xs text-muted">
                  <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} className="accent-[rgb(var(--brand))]" />
                  Share with every project
                </label>
                <button type="button" className="btn-ghost" onClick={() => setAdding(false)}>Cancel</button>
                <button className="btn-primary" disabled={!newText.trim()}>Save</button>
              </div>
            </form>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[240px] flex-1">
              <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
              <input className="input pl-9" placeholder="Search by meaning — “how do we deploy?”" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <button onClick={() => setAllProjects((a) => !a)} title="Memories belong to the project they were learned in; shared ones apply everywhere"
              className={clsx("rounded-full border px-3 py-1 text-xs", allProjects ? "border-brand/50 bg-brand/10 text-brand" : "border-line text-muted hover:text-ink")}>
              {allProjects ? "All projects" : "This project + shared"}
            </button>
            <div className="flex gap-1.5">
              {KINDS.map((k) => (
                <button key={k || "all"} onClick={() => setKind(k)}
                  className={clsx("rounded-full border px-3 py-1 text-xs capitalize", kind === k ? "border-brand/50 bg-brand/10 text-brand" : "border-line text-muted hover:text-ink")}>
                  {k || "all"}
                </button>
              ))}
            </div>
          </div>
          {fallback && <div className="rounded-lg bg-warn/10 px-3 py-2 text-[13px] text-warn">Semantic search unavailable ({fallback}) — showing keyword matches.</div>}
          <div className="text-xs text-faint">{q ? (loading ? "Searching…" : `${items.length} relevant`) : "Most recent"}</div>

          {items.length === 0 && !loading ? (
            <Empty icon={<Brain size={18} />} title={q ? "Nothing relevant" : "No memories yet"}>
              {q ? "Unrelated memories are filtered out by a similarity floor." : "The agent stores lessons and facts as it works, or add your own."}
            </Empty>
          ) : (
            <ul className="space-y-2">
              {items.map((m) => (
                <li key={m.id} className="card group flex items-start gap-3 p-3.5">
                  <KindChip kind={m.kind} />
                  <div className="min-w-0 flex-1">
                    <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed">{m.text}</p>
                    <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-faint">
                      <span>{ago(m.created)}</span>
                      <span>{m.project ? projects.find((p) => p.id === m.project)?.name ?? "other project" : "shared"}</span>
                      {m.tags.map((t) => <span key={t}>#{t}</span>)}
                    </div>
                  </div>
                  <div className="opacity-0 transition group-hover:opacity-100">
                    <ConfirmButton label="Forget" onConfirm={async () => {
                      if (await act(() => api.deleteMemory(m.id), "Forgotten")) setItems((xs) => xs.filter((x) => x.id !== m.id));
                    }} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
