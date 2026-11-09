"""Long-horizon goals: objectives the agent works toward over days, not single tasks.

A goal has a vision, success criteria and a journal. The planner (big model) repeatedly:
  1. reviews the goal, its journal and the results of the tasks done so far,
  2. assesses progress (0-100) and whether the success criteria are met,
  3. breaks out the next 1-3 concrete, verifiable tasks, which the daemon runs (see tasks.py).
When a task finishes, its outcome is written to the journal and the goal is planned again.
The goal ends when the planner judges every success criterion met, or the user marks it achieved.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import LLMError, ModelRouter
from .memory import MemoryStore
from .store import JsonStore
from .tasks import OPEN, Task, TaskStore, _jaccard, _tree_snapshot, extract_json
from .tools import truncate

GOAL_STATUSES = ("active", "paused", "achieved", "archived")

PLAN_SYSTEM = (
    "You are the planner of an autonomous coding agent pursuing a LONG-HORIZON goal over many work sessions. "
    "Review the goal, its success criteria, the journal and the project files. Then decide the next step.\n"
    'Reply with ONLY JSON: {"assessment":"2-4 sentences on where things stand","progress":0-100,'
    '"status":"working|achieved|needs_user","question":"only if needs_user: what you need from the user",'
    '"tasks":[{"title","description","acceptance":["..."],"verify_command":"shell command that exits 0 when done, or empty"}]}\n'
    "Rules: propose at most {n} tasks, each small enough for one focused session and concretely verifiable. "
    "When status=working you MUST propose at least one task unless you may not propose any: the goal always needs a "
    "concrete, time-boxed NEXT task (estimate_min <= 60). priority 5 = do first, 1 = whenever. "
    "Do not repeat tasks that are already done or queued. status=achieved only if EVERY success criterion is "
    "demonstrably met by the evidence. Never propose destructive or out-of-project work."
)


def _clamp(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(value))))
    except (TypeError, ValueError):
        return default


@dataclass
class Goal:
    title: str
    description: str = ""           # the vision: what "good" looks like and why
    success: list[str] = field(default_factory=list)  # success criteria
    project: str = ""
    status: str = "active"          # active | paused | achieved | archived
    progress: int = 0               # planner's estimate, 0-100
    assessment: str = ""            # planner's latest view of where things stand
    question: str = ""              # set when the planner needs the user
    journal: list[dict[str, Any]] = field(default_factory=list)  # {at, kind, text, task?}
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    last_planned: float = 0.0
    priority: int = 3               # 1 (low) .. 5 (urgent): weighs this goal's tasks against other goals and user work
    next_review: float = 0.0        # when the planner next re-evaluates the goal, even with tasks still queued
    day: str = ""                   # for the per-day task budget
    tasks_today: int = 0


class GoalStore(JsonStore[Goal]):
    def __init__(self, path: Path):
        path = Path(path)
        try:  # ≤0.2 kept tasks in goals.json; TaskStore normally migrates it first, this catches leftovers
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            if raw and isinstance(raw, list) and "verify_command" in raw[0] and "success" not in raw[0]:
                path.rename(path.with_suffix(".legacy.json"))
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            pass
        super().__init__(path, Goal)

    def list(self, status: str | None = None, *, project: str | None = None) -> list[Goal]:
        return [g for g in self.all() if (not status or g.status == status) and (project is None or g.project == project)]

    def log(self, goal: Goal, kind: str, text: str, **extra: Any) -> None:
        entry = {"at": time.time(), "kind": kind, "text": truncate(text, 1500), **extra}
        self.update(goal, journal=(goal.journal + [entry])[-200:])


class Planner:
    def __init__(self, router: ModelRouter, goals: GoalStore, tasks: TaskStore, memory: MemoryStore, cfg: Any):
        self.router, self.goals, self.tasks, self.memory, self.cfg = router, goals, tasks, memory, cfg

    def open_tasks(self, goal: Goal) -> list[Task]:
        return [t for t in self.tasks.list(goal=goal.id) if t.status in OPEN]

    def budget_left(self, goal: Goal) -> int:
        today = time.strftime("%Y-%m-%d")
        if goal.day != today:
            self.goals.update(goal, day=today, tasks_today=0)
        return max(0, int(self.cfg.goal_daily_tasks) - goal.tasks_today)

    def context(self, goal: Goal) -> str:
        """Background handed to every task the goal spawns, so the agent knows why it is doing it."""
        crit = "\n".join(f"- {c}" for c in goal.success)
        return (f"This task advances the long-term goal \"{goal.title}\".\n{goal.description}\n"
                + (f"Goal success criteria:\n{crit}\n" if crit else "")
                + (f"Where things stand: {goal.assessment}" if goal.assessment else "")).strip()

    def schedule_review(self, goal: Goal) -> None:
        self.goals.update(goal, next_review=time.time() + float(getattr(self.cfg, "goal_review_minutes", 60)) * 60)

    async def plan(self, goal: Goal, workspace: Path, n: int = 2) -> list[Task]:
        """One planning step. Returns the tasks it queued (possibly none). Keeps up to `n` tasks open, so a
        review of a goal that still has work queued only tops the queue up."""
        n = min(max(0, n - len(self.open_tasks(goal))), self.budget_left(goal))
        self.schedule_review(goal)
        history = []
        for t in self.tasks.list(goal=goal.id)[-12:]:
            history.append(f"- [{t.status}] {t.title}" + (f" → {truncate(t.result, 240)}" if t.result else ""))
        journal = "\n".join(f"- ({e['kind']}) {truncate(e['text'], 300)}" for e in goal.journal[-12:]) or "(empty)"
        lessons = "\n".join(f"- {m.text}" for m in self.memory.recent(6, "procedural", goal.project)) or "(none)"
        user = (f"GOAL: {goal.title} (priority {goal.priority}/5)\nVISION: {goal.description or '(none)'}\nSUCCESS CRITERIA:\n"
                + ("\n".join(f"- {c}" for c in goal.success) or "- (none given: infer sensible ones from the vision)")
                + f"\n\nTASKS SO FAR:\n{chr(10).join(history) or '(none yet)'}\n\nJOURNAL:\n{journal}\n\n"
                f"LESSONS LEARNED:\n{lessons}\n\nPROJECT FILES:\n{_tree_snapshot(workspace)}\n\n"
                + (f"You may propose up to {n} task(s) now." if n else "You may not propose tasks today (budget used); assess only."))
        self.goals.update(goal, last_planned=time.time())
        try:
            comp = await self.router.big.complete(
                [{"role": "system", "content": PLAN_SYSTEM.replace("{n}", str(max(n, 1)))}, {"role": "user", "content": user}],
                max_tokens=1400, temperature=0.3)
        except LLMError as exc:
            self.goals.log(goal, "error", f"planner unavailable: {exc}")
            return []
        data = extract_json(comp.content)
        if not isinstance(data, dict):
            self.goals.log(goal, "error", "planner reply was not JSON; will retry later")
            return []
        try:
            progress = max(0, min(100, int(float(data.get("progress", goal.progress)))))
        except (TypeError, ValueError):
            progress = goal.progress
        assessment = str(data.get("assessment", "")).strip()
        status = str(data.get("status", "working"))
        self.goals.update(goal, progress=progress, assessment=assessment or goal.assessment)
        if status == "achieved" and not self.open_tasks(goal):
            self.goals.update(goal, status="achieved", progress=100, question="")
            self.goals.log(goal, "achieved", assessment or "planner judged every success criterion met")
            self.memory.add("episodic", f"Goal '{goal.title}' achieved. {truncate(assessment, 300)}", ["goal"], goal.project)
            return []
        if status == "needs_user":
            q = str(data.get("question", "")).strip() or assessment or "the planner needs your input"
            self.goals.update(goal, status="paused", question=q)
            self.goals.log(goal, "question", q)
            return []
        existing = [t.title for t in self.tasks.list(goal=goal.id)]
        added: list[Task] = []
        for it in (data.get("tasks") or [])[:n]:
            if not isinstance(it, dict) or not str(it.get("title", "")).strip():
                continue
            title = str(it["title"]).strip()[:120]
            if any(_jaccard(title, e) >= 0.7 for e in existing):
                continue
            prio = _clamp(it.get("priority"), goal.priority, 1, 5)
            est = _clamp(it.get("estimate_min"), 30, 5, 480)
            task = self.tasks.add(Task(
                priority=prio, estimate_min=est, due=time.time() + est * 60,
                title=title, description=str(it.get("description", "") or title),
                acceptance=[str(a) for a in it.get("acceptance") or []][:6],
                verify_command=str(it.get("verify_command", "") or ""), status="pending", source="planner",
                project=goal.project, goal=goal.id, context=self.context(goal)))
            existing.append(title)
            added.append(task)
        if not added and n and status == "working" and not self.open_tasks(goal):
            title = f"Take the next step on: {goal.title}"[:120]  # the goal must always have a trackable next task
            added.append(self.tasks.add(Task(
                title=title, description=f"{assessment or goal.description}\nPick the single most valuable next step toward the "
                "goal, do it, and report what changed.", status="pending", source="planner", project=goal.project, goal=goal.id,
                context=self.context(goal), priority=goal.priority, estimate_min=30, due=time.time() + 1800)))
        self.goals.update(goal, tasks_today=goal.tasks_today + len(added))
        summary = assessment or "planned"
        if added:
            summary += "\nNext: " + "; ".join(t.title for t in added)
        self.goals.log(goal, "plan", summary, progress=progress)
        return added

    def record(self, task: Task) -> Goal | None:
        """Write a finished task's outcome into its goal's journal."""
        goal = self.goals.get(task.goal) if task.goal else None
        if goal:
            self.goals.log(goal, "task", f"{task.title} → {task.status}. {truncate(task.result, 600)}", task=task.id, status=task.status)
        return goal
