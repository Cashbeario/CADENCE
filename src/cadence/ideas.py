"""Ideas: an inbox of things worth doing, from the user or from the daemon's idea cycles.

An idea is cheap and unvetted. Nothing runs until it is promoted, either to a task (one concrete,
verifiable change) or to a goal (a long-horizon objective the planner works toward).
With `auto_approve_ideas: true`, ideas the daemon generates are promoted to tasks immediately.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import LLMError, ModelRouter
from .memory import MemoryStore
from .store import JsonStore
from .tasks import Task, TaskStore, _jaccard, _tree_snapshot, extract_json

log = logging.getLogger(__name__)

IDEA_STATUSES = ("new", "promoted", "dismissed")

IDEA_SYSTEM = (
    "You suggest valuable next things to do in a software project. Mix quick wins with bigger ambitions. "
    'Reply with ONLY a JSON array of up to {n} objects: {"title","description","size":"task|goal",'
    '"acceptance":["..."],"verify_command":"shell command or empty"}. '
    "size=task for a small verifiable change; size=goal for a multi-session objective. "
    "Prefer: missing tests, bug risks, docs gaps, performance, features the project clearly wants. "
    "Never propose destructive or out-of-project work. Do not repeat existing ideas, tasks or goals."
)


@dataclass
class Idea:
    title: str
    description: str = ""
    size: str = "task"          # task | goal: what the idea would most naturally become
    acceptance: list[str] = field(default_factory=list)
    verify_command: str = ""
    project: str = ""
    source: str = "user"        # user | auto
    status: str = "new"         # new | promoted | dismissed
    promoted_to: str = ""       # "task:<id>" | "goal:<id>"
    tags: list[str] = field(default_factory=list)
    priority: int = 3           # carried onto the task or goal it is promoted to
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


class IdeaStore(JsonStore[Idea]):
    def __init__(self, path: Path):
        super().__init__(path, Idea)

    def list(self, status: str | None = None, *, project: str | None = None) -> list[Idea]:
        return [i for i in self.all() if (not status or i.status == status) and (project is None or i.project == project)]


def promote_to_task(ideas: IdeaStore, tasks: TaskStore, idea: Idea) -> Task:
    task = tasks.add(Task(title=idea.title, description=idea.description or idea.title, acceptance=list(idea.acceptance),
                          verify_command=idea.verify_command, source="idea", project=idea.project, priority=idea.priority))
    ideas.update(idea, status="promoted", promoted_to=f"task:{task.id}")
    return task


async def ideate(router: ModelRouter, ideas: IdeaStore, tasks: TaskStore, memory: MemoryStore, *,
                 project: str, workspace: Path, goals: list[Any] = (), n: int = 3, auto_approve: bool = False) -> list[Idea]:
    """One idea cycle for a project. Returns the new ideas (already promoted when `auto_approve`)."""
    seen = [i.title for i in ideas.list(project=project)] + [t.title for t in tasks.list(project=project)] + \
        [g.title for g in goals]
    recent = "\n".join(f"- {t}" for t in seen[-25:]) or "(none)"
    lessons = "\n".join(f"- {m.text}" for m in memory.recent(8, "procedural", project)) or "(none)"
    facts = "\n".join(f"- {m.text}" for m in memory.recent(6, "semantic", project)) or "(none)"
    aims = "\n".join(f"- (priority {g.priority}) {g.title}" for g in goals if getattr(g, "status", "") == "active") or "(none)"
    user = (f"Project files:\n{_tree_snapshot(workspace)}\n\nActive goals (ideas should serve these first):\n{aims}\n\n"
            f"What we know about the project:\n{facts}\n\nAlready known (do not repeat):\n{recent}\n\nLessons learned:\n{lessons}")
    items: Any = None
    for _attempt in range(2):  # a thinking model sometimes spends its budget or answers off-format: one retry
        try:
            comp = await router.big.complete(
                [{"role": "system", "content": IDEA_SYSTEM.replace("{n}", str(n))}, {"role": "user", "content": user}],
                max_tokens=1200, temperature=0.7)
        except LLMError as exc:
            log.warning("brainstorm: model unavailable: %s", exc)
            return []
        items = extract_json(comp.content)
        if isinstance(items, dict):  # {"ideas":[...]}
            items = next((v for v in items.values() if isinstance(v, list)), None)
        if isinstance(items, list):
            break
        log.warning("brainstorm: reply was not a JSON list (finish=%s, %d chars)", comp.finish_reason, len(comp.content))
    if not isinstance(items, list):
        return []
    added: list[Idea] = []
    for it in items[:n]:
        if not isinstance(it, dict) or not str(it.get("title", "")).strip():
            continue
        title = str(it["title"]).strip()[:120]
        if any(_jaccard(title, e) >= 0.6 for e in seen):
            continue
        idea = ideas.add(Idea(title=title, description=str(it.get("description", "")),
                              size="goal" if it.get("size") == "goal" else "task",
                              acceptance=[str(a) for a in it.get("acceptance") or []][:6],
                              verify_command=str(it.get("verify_command", "") or ""), project=project, source="auto"))
        if auto_approve and idea.size == "task":
            promote_to_task(ideas, tasks, idea)
        seen.append(title)
        added.append(idea)
    return added
