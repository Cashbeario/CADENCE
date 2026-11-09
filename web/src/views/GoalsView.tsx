import clsx from "clsx";
import {
  Archive, CheckCircle2, ChevronRight, Circle, CircleDot, Compass, Flag, HelpCircle, Loader2, MessageSquarePlus, Pause, Pencil, Play, Plus,
  Sparkles, Target, Trophy, XCircle,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { act, ago, ConfirmButton, Empty, Field, GoalBadge, Modal, PageHeader, Progress } from "../components/ui";
import { api, type Goal, type JournalEntry, type Status, type TaskStatus } from "../lib/api";
import { useStore } from "../lib/store";

export function GoalsView({ status, onOpenSession: _open }: { status: Status | null; refreshStatus: () => void; onOpenSession: (sid: string) => void }) {
  const { liveTick, project } = useStore();
  const [goals, setGoals] = useState<Goal[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<Goal | null | "new">(null);
  const [showDone, setShowDone] = useState(false);

  const load = useCallback(() => api.goals(project).then((g) => {
    setGoals(g);
    setSelected((s) => (s && g.some((x) => x.id === s) ? s : g.find((x) => x.status === "active" || x.status === "paused")?.id ?? g[0]?.id ?? null));
  }).catch(() => undefined), [project]);
  useEffect(() => {
    void load();
    const t = setInterval(load, 6000);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => void load(), [liveTick, load]);

  const open = goals.filter((g) => g.status === "active" || g.status === "paused");
  const closed = goals.filter((g) => g.status === "achieved" || g.status === "archived");
  const goal = goals.find((g) => g.id === selected) ?? null;
  const d = status?.daemon;

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader
        title="Goals"
        subtitle={<span>Long-horizon objectives. The planner assesses progress, breaks out the next tasks, and keeps going until every success criterion is met.
          {!d && " The daemon is off, so planning only happens when you press “Plan next steps”."}</span>}
        actions={<button className="btn-primary" onClick={() => setEditing("new")}><Plus size={15} /> New goal</button>}
      />
      {goals.length === 0 ? (
        <div className="flex-1 overflow-y-auto">
          <Empty icon={<Target size={18} />} title="No goals yet">
            A goal is something worth working toward over days, like “Ship v1 of the billing API” or “Get test coverage above 80%”.
            CADENCE plans the steps, runs them as tasks, learns from the results and reports progress.
            <div className="mt-4"><button className="btn-primary" onClick={() => setEditing("new")}><Plus size={14} /> Set your first goal</button></div>
          </Empty>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="w-80 shrink-0 space-y-1.5 overflow-y-auto border-r border-line p-3">
            {open.map((g) => <GoalRow key={g.id} g={g} active={g.id === selected} onClick={() => setSelected(g.id)} />)}
            {closed.length > 0 && (
              <button onClick={() => setShowDone((s) => !s)} className="flex w-full items-center gap-1.5 px-2 pt-3 text-[11px] font-medium uppercase tracking-wide text-faint hover:text-muted">
                <ChevronRight size={12} className={clsx("transition", showDone && "rotate-90")} /> Achieved & archived ({closed.length})
              </button>
            )}
            {showDone && closed.map((g) => <GoalRow key={g.id} g={g} active={g.id === selected} onClick={() => setSelected(g.id)} />)}
          </div>
          <div className="min-w-0 flex-1 overflow-y-auto">
            {goal ? <GoalDetail g={goal} reload={load} onEdit={() => setEditing(goal)} daemon={!!d} /> : <Empty icon={<Target size={18} />} title="Pick a goal" />}
          </div>
        </div>
      )}
      <GoalForm open={editing !== null} goal={editing === "new" ? null : editing} onClose={() => setEditing(null)}
        onSaved={(g) => { void load(); setSelected(g.id); }} />
    </div>
  );
}

function GoalRow({ g, active, onClick }: { g: Goal; active: boolean; onClick: () => void }) {
  const running = g.tasks.some((t) => t.status === "active");
  return (
    <button onClick={onClick} className={clsx("w-full rounded-xl border p-3 text-left transition",
      active ? "border-brand/40 bg-raised shadow-glow" : "border-transparent hover:border-line hover:bg-raised/50")}>
      <div className="flex items-center gap-2">
        {g.status === "achieved" ? <Trophy size={14} className="shrink-0 text-ok" />
          : g.question ? <HelpCircle size={14} className="shrink-0 text-warn" />
          : running || g.planning ? <Loader2 size={14} className="shrink-0 animate-spin text-brand" />
          : <Target size={14} className={clsx("shrink-0", g.status === "active" ? "text-brand" : "text-faint")} />}
        <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium">{g.title}</span>
        <span className="font-mono text-[11px] text-faint">P{g.priority} · {g.progress}%</span>
      </div>
      <Progress value={g.progress} className="mt-2" />
      <div className="mt-1.5 truncate text-[11.5px] text-faint">
        {g.question ? <span className="text-warn">Needs you: {g.question}</span>
          : g.planning ? "planning next steps…"
          : running ? `working: ${g.tasks.find((t) => t.status === "active")?.title}`
          : g.status === "paused" ? "paused" : `${g.tasks.filter((t) => t.status === "done").length} task(s) done · ${ago(g.updated)}`}
      </div>
    </button>
  );
}

const TASK_ICON: Record<TaskStatus, typeof Circle> = {
  proposed: Circle, pending: Circle, active: CircleDot, done: CheckCircle2, failed: XCircle, blocked: XCircle,
};
const JOURNAL_ICON: Record<string, typeof Circle> = {
  plan: Compass, task: Flag, achieved: Trophy, question: HelpCircle, user: MessageSquarePlus, created: Sparkles, edit: Pencil, error: XCircle,
};

function GoalDetail({ g, reload, onEdit, daemon }: { g: Goal; reload: () => void; onEdit: () => void; daemon: boolean }) {
  const [note, setNote] = useState("");
  const set = async (changes: Parameters<typeof api.editGoal>[1], ok?: string) => {
    await act(() => api.editGoal(g.id, changes), ok);
    reload();
  };
  const plan = async () => {
    await act(() => api.goalAction(g.id, "plan"), daemon ? "Planning on the next tick" : "Planned");
    reload();
  };
  const sendNote = async () => {
    if (!note.trim()) return;
    await act(() => api.goalAction(g.id, "note", note.trim()), g.question ? "Answer sent; the planner will continue" : "Note added to the journal");
    setNote("");
    reload();
  };

  return (
    <div className="mx-auto max-w-3xl space-y-5 px-6 py-5">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <GoalBadge status={g.status} />
          <span className="text-[11px] text-faint">started {ago(g.created)} · {g.tasks_today} task(s) planned today</span>
          <div className="ml-auto flex items-center gap-1">
            {g.status === "active" && <button className="btn-ghost px-2.5 py-1" onClick={() => set({ status: "paused" }, "Paused")}><Pause size={13} /> Pause</button>}
            {(g.status === "paused" || g.status === "archived") && <button className="btn-ghost px-2.5 py-1" onClick={() => set({ status: "active" }, "Resumed")}><Play size={13} /> Resume</button>}
            {g.status !== "achieved" && <button className="btn-ghost px-2.5 py-1 text-ok" onClick={() => set({ status: "achieved", progress: 100 }, "Marked achieved")}><Trophy size={13} /> Achieved</button>}
            {g.status !== "archived" && <button className="btn-ghost px-2" onClick={() => set({ status: "archived" }, "Archived")} title="Archive"><Archive size={13} /></button>}
            <button className="btn-ghost px-2" onClick={onEdit} title="Edit"><Pencil size={13} /></button>
            <ConfirmButton onConfirm={async () => { await act(() => api.deleteGoal(g.id), "Goal deleted"); reload(); }} />
          </div>
        </div>
        <h2 className="mt-2 text-xl font-semibold tracking-tight">{g.title}</h2>
        {g.description && g.description !== g.title && <p className="mt-1.5 whitespace-pre-wrap text-[13.5px] leading-relaxed text-muted">{g.description}</p>}
      </div>

      <div className="card p-4">
        <div className="flex items-center gap-3">
          <div className="text-2xl font-semibold tabular-nums">{g.progress}%</div>
          <Progress value={g.progress} className="h-2 flex-1" />
          {g.status !== "achieved" && (
            <button className="btn-outline shrink-0" onClick={plan} disabled={g.planning}>
              {g.planning ? <Loader2 size={14} className="animate-spin" /> : <Compass size={14} />} {g.planning ? "Planning…" : "Plan next steps"}
            </button>
          )}
        </div>
        {g.assessment && <p className="mt-3 text-[13.5px] leading-relaxed">{g.assessment}</p>}
        {!g.assessment && <p className="mt-3 text-[13px] text-faint">No assessment yet. The planner writes one each time it reviews the goal.</p>}
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-faint">
          <label className="flex items-center gap-1.5">Priority
            <select className="rounded border border-line bg-transparent px-1 py-0.5 text-ink" value={g.priority}
              onChange={(e) => set({ priority: Number(e.target.value) }, "Priority updated")}>
              {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}{n === 5 ? " urgent" : n === 1 ? " low" : ""}</option>)}
            </select>
          </label>
          <span>{g.last_planned ? `last evaluated ${ago(g.last_planned)}` : "not evaluated yet"}</span>
          {g.status === "active" && g.next_review > 0 && <span>next evaluation {g.next_review * 1000 < Date.now() ? "due now" : `in ${Math.max(1, Math.round((g.next_review * 1000 - Date.now()) / 60000))} min`}</span>}
        </div>
      </div>

      {g.status === "active" && (
        <div className="rounded-xl border border-brand/30 bg-brand/5 p-4">
          <div className="label">Next task</div>
          {g.next_task ? <NextTask t={g.next_task} /> : (
            <p className="text-[13px] text-muted">{g.planning ? "Planning…" : "Nothing queued. The planner will line one up on its next pass, or press “Plan next steps”."}</p>
          )}
        </div>
      )}

      {g.question && (
        <div className="rounded-xl border border-warn/40 bg-warn/10 p-4">
          <div className="flex items-center gap-2 text-[13px] font-medium text-warn"><HelpCircle size={15} /> The planner needs you</div>
          <p className="mt-1.5 text-[14px]">{g.question}</p>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <section>
          <div className="label">Success criteria</div>
          {g.success.length ? (
            <ul className="space-y-1.5 text-[13px]">
              {g.success.map((c, i) => <li key={i} className="flex gap-2"><Flag size={13} className="mt-0.5 shrink-0 text-faint" />{c}</li>)}
            </ul>
          ) : <p className="text-[13px] text-faint">None set. The planner infers criteria from the vision. <button onClick={onEdit} className="text-brand underline">Add some</button></p>}
        </section>
        <section>
          <div className="label">Tasks toward this goal</div>
          {g.tasks.length ? (
            <ul className="space-y-1.5 text-[13px]">
              {g.tasks.map((t) => {
                const Icon = TASK_ICON[t.status];
                return (
                  <li key={t.id} className="flex items-start gap-2">
                    <Icon size={14} className={clsx("mt-0.5 shrink-0", t.status === "done" ? "text-ok" : t.status === "active" ? "animate-pulse text-brand" : ["failed", "blocked"].includes(t.status) ? "text-bad" : "text-faint")} />
                    <span className={clsx(t.status === "done" && "text-muted")}>{t.title}
                      <span className="ml-1.5 text-[11px] text-faint">P{t.priority}{t.finished && t.started ? ` · ${Math.max(1, Math.round((t.finished - t.started) / 60))} min` : t.estimate_min ? ` · ≈${t.estimate_min} min` : ""}</span></span>
                  </li>
                );
              })}
            </ul>
          ) : <p className="text-[13px] text-faint">None yet. They appear as the planner works.</p>}
        </section>
      </div>

      <section>
        <div className="label">{g.question ? "Answer" : "Add context or direction"}</div>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void sendNote(); }}>
          <input className="input" value={note} onChange={(e) => setNote(e.target.value)}
            placeholder={g.question ? "Your answer…" : "e.g. “Prioritise the public API over the admin UI”"} />
          <button className="btn-outline shrink-0" disabled={!note.trim()}><MessageSquarePlus size={14} /> {g.question ? "Answer" : "Add"}</button>
        </form>
        <p className="mt-1 text-[12px] text-faint">Notes go into the journal, and the planner reads it every time it plans.</p>
      </section>

      <section>
        <div className="label">Journal</div>
        <ol className="relative space-y-3 border-l border-line pl-5">
          {[...g.journal].reverse().map((e, i) => <JournalItem key={i} e={e} />)}
        </ol>
      </section>
    </div>
  );
}

function NextTask({ t }: { t: Goal["tasks"][number] }) {
  const overdue = t.due > 0 && t.due * 1000 < Date.now() && t.status !== "done";
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className="text-[14px] font-medium">{t.title}</span>
      <span className="rounded-full border border-line px-2 py-0.5 text-[11px] text-muted">P{t.priority}</span>
      <span className="text-[12px] text-faint">{t.status === "active" ? `running since ${ago(t.started)}` : "queued"}</span>
      {t.estimate_min > 0 && <span className="text-[12px] text-faint">≈{t.estimate_min} min</span>}
      {t.due > 0 && <span className={clsx("text-[12px]", overdue ? "text-warn" : "text-faint")}>{overdue ? "overdue since" : "due"} {new Date(t.due * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>}
    </div>
  );
}

function JournalItem({ e }: { e: JournalEntry }) {
  const Icon = JOURNAL_ICON[e.kind] ?? Circle;
  const tone = e.kind === "achieved" ? "text-ok" : e.kind === "question" ? "text-warn" : e.kind === "error" ? "text-bad"
    : e.kind === "task" ? (e.status === "done" ? "text-ok" : "text-bad") : e.kind === "user" ? "text-info" : "text-brand";
  return (
    <li className="relative">
      <span className={clsx("absolute -left-[29px] top-0 flex h-[18px] w-[18px] items-center justify-center rounded-full border border-line bg-panel", tone)}>
        <Icon size={10} />
      </span>
      <div className="flex items-center gap-2 text-[11px] text-faint">
        <span className="font-medium uppercase tracking-wide">{e.kind}</span>
        <span>{ago(e.at)}</span>
        {typeof e.progress === "number" && <span className="font-mono">{e.progress}%</span>}
      </div>
      <p className="mt-0.5 whitespace-pre-wrap text-[13px] leading-relaxed text-muted">{e.text}</p>
    </li>
  );
}

function GoalForm({ open, goal, onClose, onSaved }: { open: boolean; goal: Goal | null; onClose: () => void; onSaved: (g: Goal) => void }) {
  const { project } = useStore();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [success, setSuccess] = useState("");
  const [start, setStart] = useState(true);
  const [priority, setPriority] = useState(3);
  useEffect(() => {
    if (!open) return;
    setTitle(goal?.title ?? "");
    setDescription(goal?.description ?? "");
    setSuccess(goal?.success.join("\n") ?? "");
    setStart(true);
    setPriority(goal?.priority ?? 3);
  }, [open, goal]);
  const save = async () => {
    const body = { title, description, priority, success: success.split("\n").map((x) => x.trim()).filter(Boolean) };
    const g = await act(() => (goal ? api.editGoal(goal.id, body) : api.addGoal({ ...body, project, paused: !start })), goal ? "Goal saved" : "Goal set");
    if (g) {
      onSaved(g);
      onClose();
    }
  };
  return (
    <Modal open={open} onClose={onClose} title={goal ? "Edit goal" : "New long-horizon goal"} wide
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!title.trim()} onClick={save}>{goal ? "Save" : "Set goal"}</button></>}>
      <Field label="Goal"><input className="input text-[15px]" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Ship v1 of the billing API" /></Field>
      <Field label="Vision" hint="What “done” looks like and why it matters. The planner rereads this every time it plans.">
        <textarea className="input min-h-[100px]" value={description} onChange={(e) => setDescription(e.target.value)}
          placeholder="Customers can create invoices and get paid through Stripe. Must be documented and covered by tests; no breaking changes to the existing /v0 endpoints." />
      </Field>
      <Field label="Success criteria" hint="One per line, concrete and checkable. The goal is only achieved when all are met.">
        <textarea className="input min-h-[90px] font-mono text-[12.5px]" value={success} onChange={(e) => setSuccess(e.target.value)}
          placeholder={"POST /v1/invoices creates an invoice\nStripe webhook marks invoices paid\nAPI docs in docs/api.md\npytest passes with >80% coverage on billing/"} />
      </Field>
      <Field label="Priority" hint="1 = whenever, 5 = urgent. Higher-priority goals are planned and run first, and always yield to what you ask in chat.">
        <select className="input w-40" value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </Field>
      {!goal && (
        <label className="flex items-center gap-2 text-[13px] text-muted">
          <input type="checkbox" checked={start} onChange={(e) => setStart(e.target.checked)} className="accent-[rgb(var(--brand))]" />
          Start working on it right away
        </label>
      )}
    </Modal>
  );
}
