import clsx from "clsx";
import { Check, ChevronDown, CornerDownRight, ListTodo, MessageSquare, Pause, Pencil, Play, Plus, RotateCcw, Square, Target, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { act, ago, ConfirmButton, Empty, Field, Modal, PageHeader, TaskBadge } from "../components/ui";
import { api, type Status, type Task } from "../lib/api";
import { useStore } from "../lib/store";

const FILTERS = ["all", "active", "pending", "proposed", "done", "failed"] as const;
const LABEL: Record<string, string> = { active: "running", pending: "queued" };

export function TasksView({ status, refreshStatus, onOpenSession }: { status: Status | null; refreshStatus: () => void; onOpenSession: (sid: string) => void }) {
  const { liveTick, notices, project } = useStore();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const [editing, setEditing] = useState<Task | null | "new">(null);

  const load = useCallback(() => api.tasks(project).then(setTasks).catch(() => undefined), [project]);
  useEffect(() => {
    void load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => void load(), [liveTick, load]);

  const run = async (t: Task, action: Parameters<typeof api.taskAction>[1], text?: string) => {
    await act(() => api.taskAction(t.id, action, text));
    await load();
  };

  const d = status?.daemon;
  const shown = tasks.filter((t) => filter === "all" || t.status === filter || (filter === "failed" && t.status === "blocked"));
  const count = (s: string) => tasks.filter((t) => t.status === s || (s === "failed" && t.status === "blocked")).length;

  return (
    <div className="flex h-full flex-1 flex-col overflow-hidden">
      <PageHeader
        title="Tasks"
        subtitle={d ? (
          <span>
            Concrete, verifiable units of work: act → verify → reflect → retry. Daemon {d.paused ? <b className="text-warn">paused</b> : "running"} ·{" "}
            {d.running.length}/{d.max_parallel} running
          </span>
        ) : "The daemon is off, so tasks wait until `cadence web` runs with it."}
        actions={<>
          {d && (
            <button className="btn-outline" onClick={() => api.daemon(d.paused ? "resume" : "pause").then(refreshStatus)}>
              {d.paused ? <><Play size={14} /> Resume daemon</> : <><Pause size={14} /> Pause daemon</>}
            </button>
          )}
          <button className="btn-primary" onClick={() => setEditing("new")}><Plus size={15} /> New task</button>
        </>}
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 px-6 py-5">
          <div className="flex flex-wrap gap-1.5">
            {FILTERS.map((f) => (
              <button key={f} onClick={() => setFilter(f)}
                className={clsx("rounded-full border px-3 py-1 text-xs capitalize transition",
                  filter === f ? "border-brand/50 bg-brand/10 text-brand" : "border-line text-muted hover:text-ink")}>
                {LABEL[f] ?? f} {f !== "all" && <span className="text-faint">{count(f)}</span>}
              </button>
            ))}
          </div>

          {shown.length === 0 ? (
            <Empty icon={<ListTodo size={18} />} title={filter === "all" ? "No tasks yet" : `No ${LABEL[filter] ?? filter} tasks`}>
              Tasks come from you, from ideas you promote, and from the planner as it works toward your goals.
            </Empty>
          ) : (
            <div className="space-y-2.5">
              {shown.map((t) => <TaskCard key={t.id} t={t} run={run} onEdit={() => setEditing(t)} onOpenSession={onOpenSession}
                onDelete={async () => { await act(() => api.deleteTask(t.id), "Task deleted"); void load(); }} />)}
            </div>
          )}

          {notices.length > 0 && (
            <div className="card p-4">
              <div className="label">Activity</div>
              <ul className="space-y-1 font-mono text-[12px] text-muted">
                {notices.slice(-12).reverse().map((n, i) => (
                  <li key={i}><span className="text-faint">{new Date(n.at * 1000).toLocaleTimeString()}</span> {n.message}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
      <TaskForm open={editing !== null} task={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={load} />
    </div>
  );
}

function TaskCard({ t, run, onEdit, onDelete, onOpenSession }: {
  t: Task; run: (t: Task, a: Parameters<typeof api.taskAction>[1], text?: string) => Promise<void>;
  onEdit: () => void; onDelete: () => void; onOpenSession: (sid: string) => void;
}) {
  const [open, setOpen] = useState(t.status === "active");
  const [steer, setSteer] = useState("");
  return (
    <div className={clsx("card overflow-hidden", t.status === "active" && "border-brand/40 shadow-glow")}>
      <div className="flex items-start gap-3 p-4">
        <button onClick={() => setOpen((o) => !o)} className="mt-0.5 text-faint hover:text-ink" aria-label="expand">
          <ChevronDown size={16} className={clsx("transition", !open && "-rotate-90")} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <TaskBadge status={t.status} />
            <span className="rounded-full border border-line px-2 py-0.5 text-[11px] text-muted" title="priority">P{t.priority}</span>
            {t.due > 0 && !["done", "failed"].includes(t.status) && (
              <span className={clsx("text-[11.5px]", t.due * 1000 < Date.now() ? "text-warn" : "text-faint")}>
                {t.due * 1000 < Date.now() ? "overdue" : "due"} {new Date(t.due * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
              </span>
            )}
            {t.started > 0 && t.finished > 0 && <span className="text-[11.5px] text-faint">took {Math.max(1, Math.round((t.finished - t.started) / 60))} min</span>}
            {t.source !== "user" && <span className="chip border-line text-faint">{t.source === "planner" ? "from planner" : t.source === "idea" ? "from idea" : "auto"}</span>}
            {t.goal_title && <span className="chip border-violet/30 text-violet"><Target size={10} /> {t.goal_title}</span>}
            <span className="text-[11px] text-faint">{ago(t.updated)} · {t.attempts} attempt{t.attempts === 1 ? "" : "s"}</span>
          </div>
          <div className="mt-1.5 text-[14.5px] font-medium leading-snug">{t.title}</div>
          {t.status === "active" && t.activity && <div className="mt-1 truncate font-mono text-[12px] text-brand/90">⚙ {t.activity.text}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {t.status === "proposed" && (
            <>
              <button className="btn-outline px-2.5 py-1 text-ok" onClick={() => run(t, "approve")}><Check size={14} /> Approve</button>
              <button className="btn-ghost px-2" onClick={() => run(t, "reject")} title="Reject"><X size={14} /></button>
            </>
          )}
          {t.status === "active" && <button className="btn-danger px-2.5 py-1" onClick={() => run(t, "interrupt")}><Square size={11} fill="currentColor" /> Stop</button>}
          {["failed", "blocked", "done"].includes(t.status) && (
            <button className="btn-ghost px-2.5 py-1" onClick={() => run(t, "retry")} title="Run again"><RotateCcw size={13} /> Retry</button>
          )}
          {t.session && <button className="btn-ghost px-2" onClick={() => onOpenSession(t.session)} title="Open the transcript"><MessageSquare size={14} /></button>}
          {t.status !== "active" && <button className="btn-ghost px-2" onClick={onEdit} title="Edit"><Pencil size={13} /></button>}
          {t.status !== "active" && <ConfirmButton onConfirm={onDelete} />}
        </div>
      </div>
      {open && (
        <div className="space-y-3 border-t border-line bg-bg/40 px-4 py-3 pl-11 text-[13px]">
          {t.description && t.description !== t.title && <p className="whitespace-pre-wrap text-muted">{t.description}</p>}
          {t.acceptance.length > 0 && (
            <div>
              <div className="label">Acceptance</div>
              <ul className="list-disc space-y-0.5 pl-4 text-muted">{t.acceptance.map((a, i) => <li key={i}>{a}</li>)}</ul>
            </div>
          )}
          {t.verify_command && (
            <div>
              <div className="label">Verify</div>
              <code className="rounded bg-raised px-1.5 py-0.5 font-mono text-[12px]">{t.verify_command}</code>
            </div>
          )}
          {t.status === "active" && (
            <form className="flex gap-2" onSubmit={(e) => {
              e.preventDefault();
              if (steer.trim()) void run(t, "steer", steer.trim()).then(() => setSteer(""));
            }}>
              <input className="input" placeholder="Steer this task…" value={steer} onChange={(e) => setSteer(e.target.value)} />
              <button className="btn-outline shrink-0" disabled={!steer.trim()}><CornerDownRight size={14} /> Steer</button>
            </form>
          )}
          {t.reflections.length > 0 && (
            <div>
              <div className="label">Reflections</div>
              <ol className="space-y-1.5">
                {t.reflections.map((r, i) => (
                  <li key={i} className="flex gap-2">
                    <span className={clsx("chip shrink-0", r.verdict === "done" ? "border-ok/40 text-ok" : r.verdict === "blocked" ? "border-flame/40 text-flame" : "border-warn/40 text-warn")}>
                      #{r.attempt} {r.verdict}
                    </span>
                    <span className="text-muted">
                      {r.verify && <span className={clsx("mr-1.5 font-mono text-[11px]", r.verify.exit === 0 ? "text-ok" : "text-bad")}>verify exit {r.verify.exit}</span>}
                      {r.critique}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {t.result && t.status !== "active" && (
            <div>
              <div className="label">Result</div>
              <p className="whitespace-pre-wrap text-muted">{t.result}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TaskForm({ open, task, onClose, onSaved }: { open: boolean; task: Task | null; onClose: () => void; onSaved: () => void }) {
  const { project } = useStore();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [verify, setVerify] = useState("");
  useEffect(() => {
    if (!open) return;
    setTitle(task?.title ?? "");
    setDescription(task && task.description !== task.title ? task.description : "");
    setAcceptance(task?.acceptance.join("\n") ?? "");
    setVerify(task?.verify_command ?? "");
  }, [open, task]);
  const save = async () => {
    const body = { title, description: description || title, verify_command: verify, acceptance: acceptance.split("\n").filter((x) => x.trim()) };
    const ok = await act(() => (task ? api.editTask(task.id, body) : api.addTask({ ...body, project })), task ? "Task saved" : "Task queued");
    if (ok) {
      onSaved();
      onClose();
    }
  };
  return (
    <Modal open={open} onClose={onClose} title={task ? "Edit task" : "New task"}
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!title.trim()} onClick={save}>{task ? "Save" : "Queue task"}</button></>}>
      <Field label="Task"><input className="input" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Fix the flaky auth test" /></Field>
      <Field label="Details"><textarea className="input min-h-[70px]" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Context and constraints" /></Field>
      <Field label="Acceptance criteria" hint="One per line."><textarea className="input min-h-[60px]" value={acceptance} onChange={(e) => setAcceptance(e.target.value)} /></Field>
      <Field label="Verify command" hint="Must exit 0 when the task is done. It runs in the sandbox, and its result outranks the model's opinion.">
        <input className="input font-mono" value={verify} onChange={(e) => setVerify(e.target.value)} placeholder="pytest -q tests/test_auth.py" />
      </Field>
    </Modal>
  );
}
