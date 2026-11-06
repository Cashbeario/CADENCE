"""Tests for src/cadence/ideas.py: idea creation/validation, deduplication-by-policy, state
transitions (new -> promoted / dismissed) and persistence through the IdeaStore."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from cadence.ideas import IDEA_STATUSES, Idea, IdeaStore, ideate, promote_to_task
from cadence.llm import Completion
from cadence.memory import MemoryStore
from cadence.tasks import Task, TaskStore


def make_ideas(cfg):
    return IdeaStore(cfg.state_dir / "ideas.json")


def make_tasks(cfg):
    return TaskStore(cfg.state_dir / "tasks.json")


def make_memory(cfg):
    return MemoryStore(cfg.state_dir / "m.jsonl")


# --------------------------------------------------------------------------- creation & validation

def test_idea_defaults_are_reasonable():
    i = Idea(title="do the thing")
    assert i.title == "do the thing"
    assert i.description == "" and i.size == "task" and i.status == "new"
    assert i.source == "user" and i.priority == 3
    assert i.acceptance == [] and i.tags == [] and i.verify_command == ""
    assert i.id and not i.promoted_to and i.created > 0 and i.updated > 0


def test_creation_requires_a_title():
    with pytest.raises(TypeError):
        Idea(description="a title is mandatory")  # title has no default


def test_empty_or_whitespace_only_title_is_still_a_string():
    # The dataclass accepts any string; the dedup/ideate layer is what rejects empties.
    assert Idea(title="   ").title == "   "


# --------------------------------------------------------------------------- store helpers

def test_store_add_get_list_and_len(cfg):
    ideas = make_ideas(cfg)
    a = ideas.add(Idea(title="alpha", project="p1"))
    b = ideas.add(Idea(title="beta", project="p2"))
    assert len(ideas) == 2 and ideas.get(a.id) is a
    assert [i.title for i in ideas.list()] == ["alpha", "beta"]  # sorted by created
    assert [i.title for i in ideas.list(status="new")] == ["alpha", "beta"]
    assert [i.title for i in ideas.list(project="p1")] == ["alpha"]
    assert ideas.get("nonexistent") is None


def test_get_accepts_unique_prefix(cfg):
    # prefix lookup does not touch the filesystem beyond the store's own load/save
    ideas = make_ideas(cfg)
    a = Idea(title="x")
    ideas.add(a)
    assert ideas.get(a.id[:4]) is a
    assert ideas.get("zzzznope") is None


# --------------------------------------------------------------------------- persistence

def test_persistence_roundtrip_and_update_timestamp(cfg):
    path = cfg.state_dir / "ideas.json"
    store = make_ideas(cfg)
    i = store.add(Idea(title="remember me", project="p1"))
    old_updated = i.updated

    time.sleep(0.01)
    reloaded = IdeaStore(path)
    got = reloaded.get(i.id)
    assert got is not None and got.title == "remember me" and got.project == "p1"
    assert got.status == "new" and got.size == "task"

    # update bumps `updated` and survives a reload
    time.sleep(0.01)
    reloaded.update(got, status="promoted")
    assert got.updated > old_updated
    again = IdeaStore(path).get(i.id)
    assert again.status == "promoted"


def test_load_ignores_unknown_keys_and_keeps_missing_defaults(tmp_path):
    path = tmp_path / "ideas.json"
    path.write_text(json.dumps([{"title": "legacy", "description": "d", "bogus_key": 1}]))
    store = IdeaStore(path)
    recs = store.all()
    assert len(recs) == 1
    rec = recs[0]
    # unknown keys are dropped, missing fields take dataclass defaults
    assert rec.title == "legacy" and rec.description == "d"
    assert rec.size == "task" and rec.status == "new" and rec.source == "user"
    # the real (random) id is prefix-lookupable; a bogus prefix is not
    assert store.get(rec.id[:4]) is rec
    assert store.get("zzzznope") is None


# --------------------------------------------------------------------------- state transitions

def test_promote_to_task_marks_idea_and_creates_task(cfg):
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    idea = ideas.add(Idea(title="add a test", description="cover X", acceptance=["it passes"],
                          verify_command="pytest -q", project="p1", priority=4))
    task = promote_to_task(ideas, tasks, idea)

    assert task.title == "add a test" and task.source == "idea" and task.project == "p1"
    assert task.priority == 4 and task.verify_command == "pytest -q"
    assert task.acceptance == ["it passes"]
    # the idea is now promoted and points at its task, persisted in the store
    reloaded = ideas.get(idea.id)
    assert reloaded.status == "promoted" and reloaded.promoted_to == f"task:{task.id}"


def test_promoting_twice_creates_two_tasks_and_stays_promoted(cfg):
    # promote_to_task always materialises a new task; the idea itself stays promoted.
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    idea = ideas.add(Idea(title="do it"))
    first = promote_to_task(ideas, tasks, idea)
    second = promote_to_task(ideas, tasks, ideas.get(idea.id))  # re-promote the same (now promoted) idea
    assert first.id != second.id and len(tasks.list()) == 2
    got = ideas.get(idea.id)
    assert got.status == "promoted" and got.promoted_to == f"task:{second.id}"


def test_promotion_carries_onto_a_goal_like_object(cfg):
    """The web layer promotes an idea to a goal by setting status/promoted_to directly; mirror that."""
    ideas = make_ideas(cfg)
    idea = ideas.add(Idea(title="bigger ambition", size="goal"))
    ideas.update(idea, status="promoted", promoted_to="goal:abc123")
    assert ideas.get(idea.id).status == "promoted" and ideas.get(idea.id).promoted_to == "goal:abc123"


def test_dismiss_marks_idea_and_persists(cfg):
    """The CLI dismisses an idea by setting status='dismissed' directly; mirror that."""
    ideas = make_ideas(cfg)
    path = cfg.state_dir / "ideas.json"
    idea = ideas.add(Idea(title="probably not worth it", project="p1"))

    assert ideas.get(idea.id).status == "new"
    ideas.update(idea, status="dismissed")
    reloaded = ideas.get(idea.id)
    assert reloaded.status == "dismissed"

    # the dismissal survives a fresh store load from JSON on disk
    again = IdeaStore(path).get(idea.id)
    assert again is not None and again.status == "dismissed"


def test_status_values_are_the_declared_set():
    assert set(IDEA_STATUSES) == {"new", "promoted", "dismissed"}


# --------------------------------------------------------------------------- dedup / similarity

def test_ideate_dedups_exact_and_near_duplicates(cfg, make_router):
    raw = json.dumps([
        {"title": "Add unit tests for the parser"},
        {"title": "Add unit tests for parser module", "description": "near dup of the first"},
        {"title": "Ship a plugin marketplace", "size": "goal"},
    ])
    router = make_router(big_script=[Completion(content=raw)])
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    memory = make_memory(cfg)

    added = asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace, n=5))
    titles = [i.title for i in added]
    assert "Add unit tests for the parser" in titles          # first occurrence wins
    assert "Add unit tests for parser module" not in titles    # near-duplicate (jaccard >= 0.6) dropped
    assert "Ship a plugin marketplace" in titles


def test_ideate_does_not_repeat_existing_ideas_or_tasks(cfg, make_router):
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    existing_title = "Add unit tests for the parser"
    ideas.add(Idea(title=existing_title, project="p1"))
    tasks.add(Task(title="Refactor lexer", project="p1"))

    raw = json.dumps([
        {"title": existing_title},                 # exact repeat of an existing idea -> dropped
        {"title": "Add unit tests for the parser"},  # identical string again, still deduped
        {"title": "New independent feature"},
    ])
    router = make_router(big_script=[Completion(content=raw)])
    memory = make_memory(cfg)

    added = asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace, n=5))
    titles = [i.title for i in added]
    assert titles == ["New independent feature"]


def test_ideate_skips_items_without_a_title(cfg, make_router):
    raw = json.dumps([
        {"description": "no title here"},
        {"title": "   "},          # blank after strip -> skipped
        {"title": "  Add logging  ", "description": "pads and trims"},
    ])
    router = make_router(big_script=[Completion(content=raw)])
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    memory = make_memory(cfg)

    added = asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace, n=5))
    assert len(added) == 1 and added[0].title == "Add logging"


def test_ideate_trims_and_caps_fields(cfg, make_router):
    raw = json.dumps([
        {"title": "x" * 200, "acceptance": ["a"] * 50, "verify_command": "run"},
    ])
    router = make_router(big_script=[Completion(content=raw)])
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    memory = make_memory(cfg)

    added = asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace, n=5))
    assert len(added) == 1
    assert len(added[0].title) == 120          # title truncated to 120 chars
    assert len(added[0].acceptance) == 6       # acceptance capped at 6


def test_ideate_auto_approve_promotes_tasks_immediately(cfg, make_router):
    raw = json.dumps([
        {"title": "Quick fix task"},
        {"title": "Big goal", "size": "goal"},
    ])
    router = make_router(big_script=[Completion(content=raw)])
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    memory = make_memory(cfg)

    added = asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace, n=5, auto_approve=True))
    assert len(added) == 2
    quick = next(i for i in added if i.title == "Quick fix task")
    big = next(i for i in added if i.title == "Big goal")
    # only the task-sized idea is promoted to a live task; the goal stays `new`
    assert ideas.get(quick.id).status == "promoted" and ideas.get(quick.id).promoted_to.startswith("task:")
    assert ideas.get(big.id).status == "new"
    assert any(t.title == "Quick fix task" for t in tasks.list())


def test_ideate_returns_empty_when_model_unavailable(cfg, make_router):
    from cadence.llm import LLMError
    router = make_router()

    async def boom(_messages, **_kw):
        raise LLMError("model down")
    router.big.complete = boom  # type: ignore[method-assign]

    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    memory = make_memory(cfg)
    assert asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace)) == []


def test_ideate_returns_empty_for_non_json_reply(cfg, make_router):
    router = make_router(big_script=[Completion(content="sorry, no json")])
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    memory = make_memory(cfg)
    assert asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace)) == []


def test_ideate_handles_wrapped_dict_reply(cfg, make_router):
    raw = json.dumps({"ideas": [{"title": "Wrapped idea"}]})
    router = make_router(big_script=[Completion(content=raw)])
    ideas = make_ideas(cfg)
    tasks = make_tasks(cfg)
    memory = make_memory(cfg)
    added = asyncio.run(ideate(router, ideas, tasks, memory, project="p1", workspace=cfg.workspace, n=5))
    assert [i.title for i in added] == ["Wrapped idea"]
