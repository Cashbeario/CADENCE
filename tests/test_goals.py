"""Behavioural tests for the long-horizon Goal / Planner lifecycle in goals.py.

These mirror tests/test_tasks.py: a ScriptedLLM (via make_router) stands in for the
planner's big model, whose JSON reply drives Planner.plan(). The store has no status
validation and there is no dependency graph, so these tests pin the *actual* behaviour:
free-form status updates, planner-driven task creation with goal linkage, journaling on
record(), success-criteria surfacing in planner context, and the achieved/paused endings.

The async code paths are driven through asyncio.run() inside plain ``test_*`` functions so
the suite runs under a bare pytest (no pytest-asyncio plugin required).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cadence.goals import Goal, GOAL_STATUSES, GoalStore, Planner
from cadence.llm import Completion
from cadence.memory import MemoryStore
from cadence.tasks import Task, TaskStore


def _run(coro):
    return asyncio.run(coro)


def planner(cfg, router, goals=None, tasks=None):
    store = goals or GoalStore(cfg.state_dir / "goals.json")
    task_store = tasks or TaskStore(cfg.state_dir / "tasks.json")
    memory = MemoryStore(cfg.state_dir / "m.jsonl")
    p = Planner(router, store, task_store, memory, cfg)
    return p, store, task_store, memory


def reply(**fields):
    return Completion(content=json.dumps(fields))


async def _goal_defaults_on_creation(cfg):
    g = GoalStore(cfg.state_dir / "goals.json").add(Goal(title="ship auth", success=["tests pass"]))
    assert g.title == "ship auth"
    assert g.status == "active"          # default status
    assert g.progress == 0 and g.priority == 3
    assert g.success == ["tests pass"]
    assert g.id and not g.journal


async def _update_sets_any_status_without_validation(cfg):
    # The store performs no transition validation: any GOAL_STATUSES value is accepted.
    store = GoalStore(cfg.state_dir / "goals.json")
    g = store.add(Goal(title="g"))
    for status in GOAL_STATUSES:
        updated = store.update(g, status=status)
        assert updated.status == status
    # unknown statuses are likewise accepted (no enforcement anywhere)
    assert store.update(g, status="banana").status == "banana"


async def _list_filters_by_status_and_project(cfg):
    store = GoalStore(cfg.state_dir / "goals.json")
    a = store.add(Goal(title="alpha", project="p1"))
    b = store.add(Goal(title="beta", project="p2", status="paused"))
    assert len(store.list()) == 2
    assert [g.title for g in store.list(status="paused")] == ["beta"]
    assert [g.title for g in store.list(project="p1")] == ["alpha"]


async def _planner_queues_tasks_linked_to_goal(cfg, make_router):
    router = make_router(big_script=[reply(
        assessment="getting there", progress=40, status="working",
        tasks=[{"title": "write the auth module", "acceptance": ["covers login"],
                "verify_command": "pytest -q auth"}],
    )])
    p, store, task_store, _ = planner(cfg, router)
    g = store.add(Goal(title="ship auth", description="let users log in", success=["login works"]))

    added = await p.plan(g, cfg.workspace, n=2)

    assert len(added) == 1
    t = added[0]
    # goal-to-task linkage: the task records which goal it serves and why
    assert t.goal == g.id
    assert t.status == "pending" and t.source == "planner"
    assert t.acceptance == ["covers login"] and t.verify_command == "pytest -q auth"
    assert t.context.startswith(f'This task advances the long-term goal "{g.title}"')
    assert "login works" in t.context                       # success criteria flow into context
    # goal bookkeeping reflects the plan
    reloaded = store.get(g.id)
    assert reloaded.progress == 40 and reloaded.assessment == "getting there"
    assert reloaded.last_planned > 0 and reloaded.tasks_today == 1
    assert any(e["kind"] == "plan" for e in reloaded.journal)


async def _planner_always_offers_a_next_task_when_nothing_queued(cfg, make_router):
    # Empty tasks list + working status => the goal must still get a trackable next task.
    router = make_router(big_script=[reply(assessment="still planning", progress=10, status="working")])
    p, store, _task_store, _mem = planner(cfg, router)
    g = store.add(Goal(title="explore unknowns"))

    added = await p.plan(g, cfg.workspace, n=2)

    assert len(added) == 1
    assert added[0].title.startswith("Take the next step on:")
    assert added[0].goal == g.id


async def _planner_marks_goal_achieved_when_criteria_met(cfg, make_router):
    router = make_router(big_script=[reply(assessment="all green", progress=100, status="achieved")])
    p, store, _task_store, memory = planner(cfg, router)
    g = store.add(Goal(title="ship auth", success=["login works"]))

    added = await p.plan(g, cfg.workspace, n=2)

    assert added == []                       # no new tasks: the goal is over
    assert store.get(g.id).status == "achieved"
    assert store.get(g.id).progress == 100
    assert any(e["kind"] == "achieved" for e in g.journal)
    assert any("achieved" in m.text for m in memory.recent(5))


async def _planner_pauses_goal_when_it_needs_the_user(cfg, make_router):
    router = make_router(big_script=[reply(
        assessment="stuck", progress=20, status="needs_user", question="which identity provider?")])
    p, store, _task_store, _mem = planner(cfg, router)
    g = store.add(Goal(title="ship auth"))

    added = await p.plan(g, cfg.workspace, n=2)

    assert added == []
    goal = store.get(g.id)
    assert goal.status == "paused" and goal.question == "which identity provider?"
    assert any(e["kind"] == "question" for e in g.journal)


async def _planner_does_not_achieve_while_tasks_are_open(cfg, make_router):
    # status=achieved only wins when nothing is still queued.
    router = make_router(big_script=[reply(assessment="done?", progress=100, status="achieved")])
    p, store, task_store, _mem = planner(cfg, router)
    g = store.add(Goal(title="ship auth", success=["x"]))
    open_task = task_store.add(Task(title="still working", goal=g.id, status="active"))

    added = await p.plan(g, cfg.workspace, n=2)

    assert added == []
    assert store.get(g.id).status != "achieved"   # still has an open task to finish


async def _record_writes_task_outcome_to_goal_journal(cfg, make_router):
    router = make_router()
    p, store, task_store, _mem = planner(cfg, router)
    g = store.add(Goal(title="ship auth"))
    t = task_store.add(Task(title="write the auth module", goal=g.id, status="done", result="ok"))

    returned = p.record(t)

    assert returned is g                         # record returns the owning goal
    kinds = [e["kind"] for e in g.journal]
    assert "task" in kinds
    last = g.journal[-1]
    assert last["text"].startswith("write the auth module → done")
    assert last["task"] == t.id and last["status"] == "done"


async def _record_without_a_goal_returns_none(cfg, make_router):
    router = make_router()
    p, store, task_store, _mem = planner(cfg, router)
    g = store.add(Goal(title="orphan goal"))
    t = task_store.add(Task(title="ungated", status="done"))   # no .goal

    assert p.record(t) is None
    assert g.journal == []                       # nothing logged for a goal-less task


async def _goal_store_survives_reload(cfg):
    store = GoalStore(cfg.state_dir / "goals.json")
    g = store.add(Goal(title="persist me", success=["a"]))
    store.update(g, status="paused", progress=55)

    reloaded = GoalStore(cfg.state_dir / "goals.json")
    got = reloaded.get(g.id)
    assert got.status == "paused" and got.progress == 55 and got.success == ["a"]


# --- sync wrappers: drive the async bodies via asyncio.run so no pytest-asyncio is needed ---

def test_goal_defaults_on_creation(cfg):
    return _run(_goal_defaults_on_creation(cfg))


def test_update_sets_any_status_without_validation(cfg):
    return _run(_update_sets_any_status_without_validation(cfg))


def test_list_filters_by_status_and_project(cfg):
    return _run(_list_filters_by_status_and_project(cfg))


def test_planner_queues_tasks_linked_to_goal(cfg, make_router):
    return _run(_planner_queues_tasks_linked_to_goal(cfg, make_router))


def test_planner_always_offers_a_next_task_when_nothing_queued(cfg, make_router):
    return _run(_planner_always_offers_a_next_task_when_nothing_queued(cfg, make_router))


def test_planner_marks_goal_achieved_when_criteria_met(cfg, make_router):
    return _run(_planner_marks_goal_achieved_when_criteria_met(cfg, make_router))


def test_planner_pauses_goal_when_it_needs_the_user(cfg, make_router):
    return _run(_planner_pauses_goal_when_it_needs_the_user(cfg, make_router))


def test_planner_does_not_achieve_while_tasks_are_open(cfg, make_router):
    return _run(_planner_does_not_achieve_while_tasks_are_open(cfg, make_router))


def test_record_writes_task_outcome_to_goal_journal(cfg, make_router):
    return _run(_record_writes_task_outcome_to_goal_journal(cfg, make_router))


def test_record_without_a_goal_returns_none(cfg, make_router):
    return _run(_record_without_a_goal_returns_none(cfg, make_router))


def test_goal_store_survives_reload(cfg):
    return _run(_goal_store_survives_reload(cfg))
