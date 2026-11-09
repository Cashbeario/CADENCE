"""Heartbeat + event-driven daemon.

* Event-driven: `submit()` / `wake()` interrupt the sleep immediately.
* Tasks: up to `max_parallel_goals` pending tasks run as independent asyncio tasks.
* Goals: every active long-horizon goal with nothing queued is handed to the planner, which assesses
  progress and queues its next tasks. A finished task triggers an immediate re-plan of its goal.
* Heartbeat: every `24h / reflection_cycles`, when idle, it runs an idea cycle (fills the idea inbox)
  and an improvement cycle (consolidates lessons).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Any, Awaitable, Callable

from .agent import Event
from .config import Config
from .goals import Goal, Planner
from .tasks import Task, TaskRunner, TaskStore

log = logging.getLogger(__name__)
Notify = Callable[[str, str], Awaitable[None]]  # (kind, message)
REPLAN_COOLDOWN = 600.0  # seconds before re-planning a goal whose last plan queued nothing


class Daemon:
    def __init__(self, cfg: Config, runner: TaskRunner, store: TaskStore, notify: Notify | None = None,
                 event_sink: Callable[[str, Event], Awaitable[None]] | None = None, *,
                 planner: Planner | None = None, workspace_for: Callable[[str], Any] | None = None,
                 ideate: Callable[[], Awaitable[int]] | None = None, archived: Callable[[], set[str]] | None = None):
        self.cfg, self.runner, self.store = cfg, runner, store
        self.notify = notify
        self.event_sink = event_sink
        self.planner = planner
        self.workspace_for = workspace_for or (lambda _pid: cfg.workspace)
        self.ideate = ideate
        self.archived = archived or set  # project ids the daemon must leave alone
        self.paused = False
        self.user_turns = 0              # chat turns in flight: a live user directive outranks background work
        self._force: set[str] = set()    # goals the user asked to plan right now
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[Task]] = {}
        self._planning: dict[str, asyncio.Task[None]] = {}
        self._state_path = cfg.state_dir / "daemon.json"
        self.last_tick = ""
        self.cycles_today = 0
        self._day = dt.date.today().isoformat()
        self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if data.get("day") == self._day:
                self.cycles_today = int(data.get("cycles", 0))
        except (OSError, json.JSONDecodeError):
            pass

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"day": self._day, "cycles": self.cycles_today}), encoding="utf-8")

    def _roll_day(self) -> None:
        today = dt.date.today().isoformat()
        if today != self._day:
            self._day, self.cycles_today = today, 0
            self._save_state()

    @property
    def interval(self) -> float:
        return 86400.0 / max(1, self.cfg.reflection_cycles)

    @property
    def running(self) -> list[str]:
        return list(self._tasks)

    @property
    def planning(self) -> list[str]:
        return list(self._planning)

    # ------------------------------------------------------------------- API
    def submit(self, task: Task) -> Task:
        self.store.add(task)
        self._wake.set()
        return task

    def plan_now(self, goal: Goal) -> None:
        """Ask for a planning pass on the next tick, ignoring the cooldown."""
        self._force.add(goal.id)
        if self.planner:
            self.planner.goals.update(goal, last_planned=0.0)
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_forever(self) -> None:
        recovered = self.store.recover_interrupted()
        if recovered:
            await self._say("info", f"re-queued {recovered} task(s) interrupted by a previous shutdown")
        next_cycle = 0.0
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            self._roll_day()
            self._reap()
            if not self.paused:
                self._launch_ready()
                self._advance_goals()
                idle = not self._tasks and not self._planning
                if idle and loop.time() >= next_cycle and self.cycles_today < self.cfg.reflection_cycles:
                    await self._cycle()
                    next_cycle = loop.time() + self.interval
            self.last_tick = dt.datetime.now().strftime("%H:%M:%S")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(30.0, self.interval))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
        pending = [*self._tasks.values(), *self._planning.values()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # -------------------------------------------------------------- internals
    def _launch_ready(self) -> None:
        free = self.cfg.max_parallel_goals - len(self._tasks)
        if free <= 0:
            return
        paused = {g.id for g in self.planner.goals.all() if g.status != "active"} if self.planner else set()
        archived = self.archived()
        ready = [t for t in self.store.list("pending") if t.goal not in paused and t.project not in archived]
        if self.user_turns:  # a user directive is being worked on: only user-set or urgent tasks may start beside it
            ready = [t for t in ready if t.source == "user" or t.priority >= 5]
        for task in sorted(ready, key=self._weight, reverse=True)[:free]:
            if task.id not in self._tasks:
                self._tasks[task.id] = asyncio.create_task(self._run_task(task), name=f"task-{task.id}")

    def _weight(self, task: Task) -> tuple[float, float]:
        """Run order: task priority + its goal's priority + a boost for work the user asked for; older and overdue first."""
        goal = self.planner.goals.get(task.goal) if self.planner and task.goal else None
        score = task.priority + (goal.priority if goal else 0) + (3 if task.source == "user" else 0)
        overdue = 1.0 if task.due and task.due < time.time() else 0.0
        return (score + overdue, -task.created)

    async def _run_task(self, task: Task) -> Task:
        await self._say("task", f"▶ {task.title}")
        result = await self.runner.run(task, self.event_sink)
        await self._say("task", f"{'✔' if result.status == 'done' else '✖'} {task.title} → {result.status}")
        if self.planner and result.status not in ("pending", "active"):
            goal = self.planner.record(result)
            if goal and goal.status == "active":
                self.planner.goals.update(goal, last_planned=0.0)  # re-plan right away with the new evidence
        self._wake.set()
        return result

    def _advance_goals(self) -> None:
        """Evaluate goals (one planning call at a time: it is a big-model call), highest priority first. A goal is
        planned when asked for, when nothing is queued for it, or when its review time has come round."""
        if not self.planner or self._planning:
            return
        now = time.time()
        archived = self.archived()
        for goal in sorted(self.planner.goals.list("active"), key=lambda g: (-g.priority, g.last_planned)):
            if goal.project in archived:
                continue
            forced = goal.id in self._force
            due_review = bool(goal.last_planned) and now >= goal.next_review > 0
            idle = not self.planner.open_tasks(goal) and now - goal.last_planned >= REPLAN_COOLDOWN
            if not (forced or due_review or idle):
                continue
            if not forced and not due_review and goal.last_planned and self.planner.budget_left(goal) == 0:
                continue
            self._force.discard(goal.id)
            self._planning[goal.id] = asyncio.create_task(self._plan(goal), name=f"plan-{goal.id}")
            return

    async def _plan(self, goal: Goal) -> None:
        assert self.planner is not None
        try:
            added = await self.planner.plan(goal, self.workspace_for(goal.project))
            if goal.status == "achieved":
                await self._say("goal", f"★ goal achieved: {goal.title}")
                await self.runner.hooks.emit("goal_achieved", {"goal_id": goal.id, "title": goal.title, "assessment": goal.assessment})
            elif goal.status == "paused" and goal.question:
                await self._say("goal", f"? {goal.title}: {goal.question}")
            elif added:
                await self._say("goal", f"◆ {goal.title} ({goal.progress}%): queued {', '.join(t.title for t in added)}")
            await self.runner.hooks.emit("goal_planned", {"goal_id": goal.id, "title": goal.title, "progress": goal.progress,
                                                          "assessment": goal.assessment, "queued": [t.title for t in added]})
        except Exception as exc:  # noqa: BLE001 — a planning bug must not stop the daemon
            log.exception("planning %s failed", goal.id)
            self.planner.goals.log(goal, "error", f"planning crashed: {exc}")
        finally:
            self._planning.pop(goal.id, None)
            self._wake.set()

    def _reap(self) -> None:
        for tid, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[tid]
                if not task.cancelled() and task.exception():
                    log.error("task %s crashed: %s", tid, task.exception())

    async def _cycle(self) -> None:
        self.cycles_today += 1
        self._save_state()
        added = await self.ideate() if self.ideate else 0
        learned = await self.runner.consolidate_lessons()
        await self.runner.hooks.emit("heartbeat", {"cycle": self.cycles_today, "ideas": added, "lessons": learned})
        await self._say("cycle", f"cycle {self.cycles_today}/{self.cfg.reflection_cycles}: {added} idea(s), {learned} lesson(s) consolidated")
        if added and self.cfg.auto_approve_ideas:
            self._wake.set()

    async def _say(self, kind: str, message: str) -> None:
        if self.notify:
            await self.notify(kind, message)
