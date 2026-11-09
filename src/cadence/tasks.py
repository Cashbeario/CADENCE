"""Tasks: concrete, verifiable units of work, and the loop that runs them.

A task runs as:  act → verify (objective command, if any) → reflect (big model) → done | retry | blocked.
Lessons from every reflection are written to procedural memory; outcomes to episodic memory.
Tasks come from the user, from accepted ideas, or from the planner working toward a long-horizon goal
(see goals.py); `goal` links a task to the goal it advances.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .agent import Agent, Event
from .config import Config, SandboxConfig
from .hooks import Hooks
from .llm import LLMError, ModelRouter
from .memory import MemoryStore
from .sandbox import Sandbox, SandboxError
from .store import JsonStore
from .tools import truncate

STATUSES = ("proposed", "pending", "active", "done", "failed", "blocked")
OPEN = ("proposed", "pending", "active")


@dataclass
class Task:
    title: str
    description: str = ""
    acceptance: list[str] = field(default_factory=list)
    verify_command: str = ""
    status: str = "pending"
    source: str = "user"  # user | auto | planner | idea
    project: str = ""     # project id; "" = the default workspace
    goal: str = ""        # id of the long-horizon goal this task advances
    context: str = ""     # background handed to the agent (e.g. the goal's vision)
    priority: int = 3     # 1 (low) .. 5 (urgent); the daemon runs higher first (user tasks get a boost)
    estimate_min: int = 0 # planned minutes of work; 0 = unknown
    due: float = 0.0      # when it should be done by (epoch); 0 = no deadline
    started: float = 0.0
    finished: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    attempts: int = 0
    reflections: list[dict[str, Any]] = field(default_factory=list)
    result: str = ""
    session: str = ""     # chat session id of the run, for "open transcript"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


class TaskStore(JsonStore[Task]):
    def __init__(self, path: Path):
        path = Path(path)
        legacy = path.with_name("goals.json")
        if not path.exists() and legacy.is_file() and path.name == "tasks.json":
            # ≤0.2 called these "goals"; long-horizon goals now live in goals.json with a different shape.
            legacy.rename(path)
        super().__init__(path, Task)

    def list(self, status: str | None = None, *, project: str | None = None, goal: str | None = None) -> list[Task]:
        return [t for t in self.all()
                if (not status or t.status == status) and (project is None or t.project == project)
                and (goal is None or t.goal == goal)]

    def recover_interrupted(self) -> int:
        """Tasks left `active` by a crash go back to the queue."""
        n = 0
        for t in self.list("active"):
            self.update(t, status="pending")
            n += 1
        return n


def extract_json(text: str) -> Any | None:
    """Pull the first JSON object/array out of model output (handles fences and chatter)."""
    text = re.sub(r"```(?:json)?", "", text)
    pairs = [(text.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if text.find(o) != -1]
    for start, _opener, closer in sorted(pairs):  # outermost (earliest) structure wins
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(re.findall(r"[a-z0-9]{3,}", a.lower())), set(re.findall(r"[a-z0-9]{3,}", b.lower()))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


REFLECT_SYSTEM = (
    "You are the reflection stage of an autonomous coding agent. Judge strictly from the evidence given. "
    'Reply with ONLY JSON: {"verdict":"done|retry|blocked","critique":"...","next_steps":["..."],"lessons":["..."]}. '
    "verdict=done only if every acceptance criterion is demonstrably met. blocked = needs a human or missing access. "
    "lessons = short, reusable, imperative rules worth remembering."
)

EventSink = Callable[[str, Event], Awaitable[None]]


class TaskRunner:
    """`agent_factory(name, project)` builds an agent scoped to the task's project; `sandbox_for(project)`
    gives the sandbox its verify command runs in (both default to the single configured workspace)."""

    def __init__(self, cfg: Config, router: ModelRouter, store: TaskStore, memory: MemoryStore,
                 agent_factory: Callable[..., Agent], sandbox: Sandbox | None = None, hooks: Hooks | None = None,
                 sandbox_for: Callable[[str], Sandbox] | None = None):
        self.cfg, self.router, self.store, self.memory, self.agent_factory = cfg, router, store, memory, agent_factory
        self.sandbox = sandbox or Sandbox(SandboxConfig(mode="none"), cfg.workspace)
        self.sandbox_for = sandbox_for
        self.hooks = hooks or Hooks(workspace=cfg.workspace)
        self.agents: dict[str, Agent] = {}  # live task agents, for steering/interrupt
        self.on_finish: Callable[[Agent, Task], Awaitable[None]] | None = None  # e.g. the reflection pass

    async def run(self, task: Task, sink: EventSink | None = None) -> Task:
        self.store.update(task, status="active", started=task.started or time.time())
        agent = self.agent_factory(f"task-{task.id}", task.project) if task.project else self.agent_factory(f"task-{task.id}")
        self.agents[task.id] = agent
        self.store.update(task, session=agent.session_id)
        prompt = self._initial_prompt(task)
        try:
            first = task.attempts + 1  # a retried task keeps counting from where it stopped
            for attempt in range(first, first + self.cfg.max_reflections_per_goal + 1):
                self.store.update(task, attempts=attempt)
                final = await self._act(agent, task, prompt, sink)
                if final is None:  # interrupted
                    self.store.update(task, status="pending", result="interrupted; will resume")
                    return task
                verify = await self._verify(task)
                reflection = await self._reflect(task, final, verify)
                task.reflections.append({"attempt": attempt, "verify": verify, **reflection})
                for lesson in reflection.get("lessons", []):
                    self.memory.add("procedural", str(lesson), ["lesson", "auto"], task.project)
                verdict = reflection["verdict"]
                if verdict == "done":
                    self.store.update(task, status="done", result=final)
                    break
                if verdict == "blocked":
                    self.store.update(task, status="blocked", result=reflection.get("critique", final))
                    break
                if attempt - first >= self.cfg.max_reflections_per_goal:
                    self.store.update(task, status="failed", result=f"gave up after {attempt} attempts: {reflection.get('critique', '')}")
                    break
                steps = "\n".join(f"- {s}" for s in reflection.get("next_steps", []))
                prompt = (f"Reflection on attempt {attempt}: {reflection.get('critique', '')}\n"
                          f"Verification: {verify or 'n/a'}\nNext steps:\n{steps}\nContinue working on the task until the acceptance criteria are met.")
            self.store.update(task, finished=time.time())
            self.memory.add("episodic", f"Task '{task.title}' → {task.status} after {task.attempts} attempt(s). {truncate(task.result, 300)}", ["task"], task.project)
            self.store.update(task)
            if self.on_finish:
                await self.on_finish(agent, task)
            await self.hooks.emit("task_done", {"agent": agent.name, "task_id": task.id, "goal_id": task.goal, "title": task.title,
                                                "status": task.status, "result": truncate(task.result, 2000)})
        except LLMError as exc:
            self.store.update(task, status="pending", result=f"model unavailable: {exc}")
        finally:
            self.agents.pop(task.id, None)
            agent.save_session()
        return task

    async def _act(self, agent: Agent, task: Task, prompt: str, sink: EventSink | None) -> str | None:
        final = ""
        async for ev in agent.run(prompt):
            if sink:
                await sink(task.id, ev)
            if ev.type == "message_done":
                final = ev.data["text"]
            elif ev.type == "error":
                raise LLMError(ev.data["message"])
            elif ev.type == "interrupted":
                return None
        return final

    @staticmethod
    def _initial_prompt(task: Task) -> str:
        parts = [f"TASK: {task.title}  [priority {task.priority}/5" + (f", about {task.estimate_min} min" if task.estimate_min else "") + "]"]
        if task.description and task.description != task.title:
            parts.append(task.description)
        if task.context:
            parts.append(task.context)
        if task.acceptance:
            parts.append("Acceptance criteria:\n" + "\n".join(f"- {a}" for a in task.acceptance))
        if task.verify_command:
            parts.append(f"Objective check (must exit 0): `{task.verify_command}`")
        return "\n\n".join(parts)

    async def _verify(self, task: Task) -> dict[str, Any] | None:
        if not task.verify_command:
            return None
        sandbox = self.sandbox_for(task.project) if self.sandbox_for else self.sandbox
        try:
            res = await sandbox.run(task.verify_command, timeout=300)
        except SandboxError as exc:
            return {"exit": -1, "output": f"sandbox unavailable: {exc}"}
        if res.timed_out:
            return {"exit": -1, "output": "verification timed out"}
        return {"exit": res.exit, "output": truncate(res.output, 1500)}

    async def _reflect(self, goal: Task, final: str, verify: dict[str, Any] | None) -> dict[str, Any]:
        evidence = (f"TASK: {goal.title}\n{goal.description}\nAcceptance: {goal.acceptance}\n"
                    f"AGENT FINAL REPORT:\n{truncate(final, 3000)}\nVERIFICATION: {json.dumps(verify) if verify else 'none'}")
        parsed: Any = None
        try:
            comp = await self.router.big.complete(
                [{"role": "system", "content": REFLECT_SYSTEM}, {"role": "user", "content": evidence}],
                max_tokens=700, temperature=0.1)
            parsed = extract_json(comp.content)
        except LLMError:
            parsed = None
        reflection = parsed if isinstance(parsed, dict) else {}
        verdict = reflection.get("verdict")
        if verdict not in ("done", "retry", "blocked"):
            verdict = "done" if (verify and verify["exit"] == 0) else "retry"
            reflection.setdefault("critique", "reflection output unparseable; decided from verification")
        if verify is not None:  # objective evidence outranks model opinion
            if verify["exit"] != 0 and verdict == "done":
                verdict = "retry"
                reflection["critique"] = f"verification failed (exit {verify['exit']}): {verify['output'][:300]}"
            elif verify["exit"] == 0 and verdict == "retry":
                verdict = "done"
        return {
            "verdict": verdict,
            "critique": str(reflection.get("critique", "")),
            "next_steps": [str(s) for s in reflection.get("next_steps", []) if s][:6],
            "lessons": [str(s) for s in reflection.get("lessons", []) if s][:4],
        }

    async def consolidate_lessons(self) -> int:
        """Improvement cycle: the small model distills recent reflections into durable rules."""
        recent = [r for g in self.store.list()[-10:] for r in g.reflections[-2:] if r.get("critique")]
        if not recent:
            return 0
        text = "\n".join(f"- {r['verdict']}: {r['critique']}" for r in recent)
        try:
            comp = await self.router.small.complete(
                [{"role": "system", "content": "Extract up to 3 short, reusable, imperative engineering rules from these critiques. One per line, no numbering."},
                 {"role": "user", "content": text}], max_tokens=300, temperature=0.1)
        except LLMError:
            return 0
        n = 0
        for line in comp.content.splitlines():
            line = line.strip("-*• \t")
            if 12 <= len(line) <= 240:
                self.memory.add("procedural", line, ["consolidated"])
                n += 1
        return n


def _tree_snapshot(root: Path, limit: int = 80) -> str:
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".cadence"}
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        depth = Path(dirpath).relative_to(root).parts
        if len(depth) > 3:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            out.append(str(Path(dirpath, fn).relative_to(root)))
            if len(out) >= limit:
                return "\n".join(out) + "\n…"
    return "\n".join(out) or "(empty workspace)"
