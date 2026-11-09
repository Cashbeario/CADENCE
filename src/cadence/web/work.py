"""Routes for the work model: projects, long-horizon goals, tasks and the idea inbox."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ..goals import GOAL_STATUSES, Goal
from ..ideas import IDEA_STATUSES, Idea, ideate, promote_to_task
from ..projects import Project, ProjectError, check_path, suggest_name
from ..tasks import OPEN, Task

if TYPE_CHECKING:
    from ..runtime import Runtime
    from .server import Hub


def _prio(v: Any, default: int = 3) -> int:
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return default


def _strs(value: Any, limit: int = 12) -> list[str]:
    return [str(v).strip() for v in (value or []) if str(v).strip()][:limit]


def routes(r: web.RouteTableDef, rt: "Runtime", hub: "Hub") -> None:
    from .server import body

    def wake() -> None:
        if rt.daemon:
            rt.daemon.wake()

    async def changed(kind: str, project: str = "") -> None:
        await hub.broadcast({"kind": "changed", "what": kind, "project": project})

    def pid_of(req: web.Request) -> str | None:
        p = req.query.get("project")
        return rt.project(p).id if p else None

    def norm(pid: str) -> str:
        """Records made before projects existed have project "" = the default project."""
        return pid or rt.default_project.id

    # ────────────────────────────────────────────────────────── projects
    def project_dict(p: Project) -> dict[str, Any]:
        d = asdict(p)
        d["exists"] = p.root.is_dir()
        d["default"] = p.id == rt.default_project.id
        d["counts"] = {
            "goals": sum(1 for g in rt.goals.all() if norm(g.project) == p.id and g.status == "active"),
            "tasks": sum(1 for t in rt.tasks.all() if norm(t.project) == p.id and t.status in OPEN),
            "ideas": sum(1 for i in rt.ideas.all() if norm(i.project) == p.id and i.status == "new"),
        }
        return d

    @r.get("/api/projects")
    async def projects(_req: web.Request) -> web.Response:
        return web.json_response([project_dict(p) for p in rt.projects.all()])

    @r.get("/api/fs")
    async def browse(req: web.Request) -> web.Response:
        """Sub-directories of any folder on this machine, for the folder picker (names only, never file contents)."""
        raw = req.query.get("path", "").strip() or "~"
        d = Path(raw).expanduser()
        if not d.is_absolute():
            raise web.HTTPBadRequest(text="use an absolute path (or one starting with ~)")
        d = d.resolve()
        if not d.is_dir():
            raise web.HTTPBadRequest(text=f"{d} is not a directory")
        try:
            kids = sorted((c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")), key=lambda c: c.name.lower())
        except PermissionError:
            raise web.HTTPForbidden(text="permission denied")
        return web.json_response({"path": str(d), "parent": str(d.parent) if d != d.parent else None, "home": str(Path.home()),
                                  "dirs": [{"name": c.name, "path": str(c)} for c in kids[:1000]]})

    @r.post("/api/projects/suggest-name")
    async def suggest(req: web.Request) -> web.Response:
        data = await body(req)
        try:
            path = check_path(str(data.get("path", "")))
        except ProjectError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"name": await suggest_name(rt.router.small, path), "path": str(path)})

    @r.post("/api/projects")
    async def add_project(req: web.Request) -> web.Response:
        data = await body(req)
        try:
            path = check_path(str(data.get("path", "")), create=bool(data.get("create")))
        except ProjectError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if existing := rt.projects.by_path(path):
            raise web.HTTPConflict(text=f"{path} is already the project '{existing.name}'")
        name = str(data.get("name", "")).strip()[:60] or await suggest_name(rt.router.small, path)
        p = rt.projects.add(Project(name=name, path=str(path), description=str(data.get("description", "")),
                                    instructions=str(data.get("instructions", "")), color=str(data.get("color", ""))))
        await changed("projects")
        return web.json_response(project_dict(p))

    @r.patch("/api/projects/{pid}")
    async def edit_project(req: web.Request) -> web.Response:
        p = rt.projects.get(req.match_info["pid"])
        if not p:
            raise web.HTTPNotFound(text="no such project")
        data = await body(req)
        changes: dict[str, Any] = {k: str(data[k]) for k in ("name", "description", "instructions", "color") if k in data}
        if "name" in changes and not changes["name"].strip():
            raise web.HTTPBadRequest(text="name cannot be empty")
        if "path" in data and str(data["path"]) != p.path:
            if any(s.running and s.agent.project == p.id for s in hub.sessions.values()):
                raise web.HTTPConflict(text="stop the project's running chats before moving it")
            try:
                changes["path"] = str(check_path(str(data["path"]), create=bool(data.get("create"))))
            except ProjectError as exc:
                raise web.HTTPBadRequest(text=str(exc))
            for sid in [sid for sid, s in hub.sessions.items() if s.agent.project == p.id]:
                hub.sessions.pop(sid)  # reload with tools bound to the new directory
        if "archived" in data:
            if data["archived"] and p.id == rt.default_project.id:
                raise web.HTTPConflict(text="the launch folder is the default project and cannot be archived")
            changes["archived"] = bool(data["archived"])
            if changes["archived"]:
                for t in rt.tasks.all():  # stop work in progress; queued tasks stay queued for when it returns
                    if norm(t.project) == p.id and t.id in rt.runner.agents:
                        rt.runner.agents[t.id].interrupt()
                await rt.browser.get(p.id).close()
        rt.projects.update(p, **changes)
        await changed("projects")
        return web.json_response(project_dict(p))

    @r.delete("/api/projects/{pid}")
    async def delete_project(req: web.Request) -> web.Response:
        p = rt.projects.get(req.match_info["pid"])
        if not p:
            raise web.HTTPNotFound(text="no such project")
        if p.id == rt.default_project.id:
            raise web.HTTPConflict(text="the launch workspace is the default project and cannot be removed")
        rt.projects.delete(p.id)  # the directory and its files are left alone
        await rt.browser.get(p.id).close()
        await changed("projects")
        return web.json_response({"ok": True})

    # ───────────────────────────────────────────────────────────── goals
    def goal_dict(g: Goal) -> dict[str, Any]:
        d = asdict(g)
        d["project"] = norm(g.project)
        tasks = rt.tasks.list(goal=g.id)
        d["tasks"] = [{"id": t.id, "title": t.title, "status": t.status, "updated": t.updated, "priority": t.priority,
                       "due": t.due, "estimate_min": t.estimate_min, "started": t.started, "finished": t.finished} for t in tasks]
        open_ = sorted((t for t in tasks if t.status in ("proposed", "pending", "active")),
                       key=lambda t: (t.status != "active", -t.priority, t.created))
        d["next_task"] = d["tasks"][tasks.index(open_[0])] if open_ else None
        d["planning"] = bool(rt.daemon and g.id in rt.daemon.planning)
        return d

    @r.get("/api/goals")
    async def goals(req: web.Request) -> web.Response:
        pid = pid_of(req)
        items = [g for g in rt.goals.all() if pid is None or norm(g.project) == pid]
        return web.json_response([goal_dict(g) for g in sorted(items, key=lambda g: g.updated, reverse=True)])

    @r.post("/api/goals")
    async def add_goal(req: web.Request) -> web.Response:
        data = await body(req)
        title = str(data.get("title", "")).strip()
        if not title:
            raise web.HTTPBadRequest(text="title required")
        g = rt.goals.add(Goal(title=title[:160], description=str(data.get("description", "")), success=_strs(data.get("success")),
                              project=rt.project(str(data.get("project", ""))).id, priority=_prio(data.get("priority")),
                              status="paused" if data.get("paused") else "active"))
        rt.goals.log(g, "created", "goal set by the user")
        wake()
        await changed("goals", g.project)
        return web.json_response(goal_dict(g))

    @r.patch("/api/goals/{gid}")
    async def edit_goal(req: web.Request) -> web.Response:
        g = rt.goals.get(req.match_info["gid"])
        if not g:
            raise web.HTTPNotFound(text="no such goal")
        data = await body(req)
        changes: dict[str, Any] = {k: str(data[k]) for k in ("title", "description") if k in data}
        if "success" in data:
            changes["success"] = _strs(data["success"])
        if "status" in data:
            if data["status"] not in GOAL_STATUSES:
                raise web.HTTPBadRequest(text=f"status must be one of {', '.join(GOAL_STATUSES)}")
            changes["status"] = data["status"]
            if data["status"] == "active":
                changes["question"] = ""
        if "priority" in data:
            changes["priority"] = _prio(data["priority"], g.priority)
        if "progress" in data:
            changes["progress"] = max(0, min(100, int(data["progress"])))
        rt.goals.update(g, **changes)
        if changes:
            rt.goals.log(g, "edit", "edited by the user: " + ", ".join(sorted(changes)))
        wake()
        await changed("goals", g.project)
        return web.json_response(goal_dict(g))

    @r.post("/api/goals/{gid}/{action}")
    async def goal_action(req: web.Request) -> web.Response:
        g = rt.goals.get(req.match_info["gid"])
        if not g:
            raise web.HTTPNotFound(text="no such goal")
        action = req.match_info["action"]
        data = await body(req)
        if action == "plan":
            if rt.project(g.project).archived:
                raise web.HTTPConflict(text="this goal's project is archived: unarchive it and the daemon will plan it again")
            if g.status != "active":
                rt.goals.update(g, status="active", question="")
            if rt.daemon:
                rt.daemon.plan_now(g)
            else:
                await rt.planner.plan(g, rt.project(g.project).root)
        elif action == "note":  # also the answer to a planner question
            text = str(data.get("text", "")).strip()
            if not text:
                raise web.HTTPBadRequest(text="text required")
            rt.goals.log(g, "user", text)
            if g.status == "paused" and g.question:
                rt.goals.update(g, status="active", question="")
                if rt.daemon:
                    rt.daemon.plan_now(g)
        else:
            raise web.HTTPNotFound(text=f"unknown action {action}")
        await changed("goals", g.project)
        return web.json_response(goal_dict(g))

    @r.delete("/api/goals/{gid}")
    async def delete_goal(req: web.Request) -> web.Response:
        g = rt.goals.get(req.match_info["gid"])
        if not g:
            raise web.HTTPNotFound(text="no such goal")
        for t in rt.tasks.list(goal=g.id):
            if t.status == "active" and t.id in rt.runner.agents:
                rt.runner.agents[t.id].interrupt()
            if t.status in ("pending", "proposed"):
                rt.tasks.delete(t.id)  # queued work for a deleted goal goes too; finished history stays
            else:
                rt.tasks.update(t, goal="")
        rt.goals.delete(g.id)
        await changed("goals", g.project)
        return web.json_response({"ok": True})

    # ───────────────────────────────────────────────────────────── tasks
    def task_dict(t: Task) -> dict[str, Any]:
        d = asdict(t)
        d["project"] = norm(t.project)
        d["activity"] = hub.task_activity.get(t.id)
        goal = rt.goals.get(t.goal) if t.goal else None
        d["goal_title"] = goal.title if goal else ""
        return d

    @r.get("/api/tasks")
    async def tasks(req: web.Request) -> web.Response:
        pid = pid_of(req)
        items = [t for t in rt.tasks.all() if pid is None or norm(t.project) == pid]
        return web.json_response([task_dict(t) for t in reversed(items)])

    @r.post("/api/tasks")
    async def add_task(req: web.Request) -> web.Response:
        data = await body(req)
        title = str(data.get("title", "")).strip()
        if not title:
            raise web.HTTPBadRequest(text="title required")
        task = Task(title=title[:160], description=str(data.get("description", "") or title),
                    acceptance=_strs(data.get("acceptance")), verify_command=str(data.get("verify_command", "") or ""),
                    project=rt.project(str(data.get("project", ""))).id, goal=str(data.get("goal", "")),
                    priority=_prio(data.get("priority"), 4), estimate_min=int(data.get("estimate_min") or 0),  # typed by the user: outranks background work
                    due=float(data.get("due") or 0))
        (rt.daemon.submit(task) if rt.daemon else rt.tasks.add(task))
        await changed("tasks", task.project)
        return web.json_response(task_dict(task))

    @r.patch("/api/tasks/{tid}")
    async def edit_task(req: web.Request) -> web.Response:
        t = rt.tasks.get(req.match_info["tid"])
        if not t:
            raise web.HTTPNotFound(text="no such task")
        if t.status == "active":
            raise web.HTTPConflict(text="stop the task before editing it (or steer it instead)")
        data = await body(req)
        changes: dict[str, Any] = {k: str(data[k]) for k in ("title", "description", "verify_command") if k in data}
        if "acceptance" in data:
            changes["acceptance"] = _strs(data["acceptance"])
        if "priority" in data:
            changes["priority"] = _prio(data["priority"], t.priority)
        for k, cast in (("estimate_min", int), ("due", float)):
            if k in data:
                changes[k] = cast(data[k] or 0)
        rt.tasks.update(t, **changes)
        await changed("tasks", t.project)
        return web.json_response(task_dict(t))

    @r.delete("/api/tasks/{tid}")
    async def delete_task(req: web.Request) -> web.Response:
        t = rt.tasks.get(req.match_info["tid"])
        if not t:
            raise web.HTTPNotFound(text="no such task")
        if t.status == "active":
            raise web.HTTPConflict(text="stop the task first")
        rt.tasks.delete(t.id)
        await changed("tasks", t.project)
        return web.json_response({"ok": True})

    @r.post("/api/tasks/{tid}/{action}")
    async def task_action(req: web.Request) -> web.Response:
        t = rt.tasks.get(req.match_info["tid"])
        if not t:
            raise web.HTTPNotFound(text="no such task")
        action = req.match_info["action"]
        if action == "approve" and t.status == "proposed":
            rt.tasks.update(t, status="pending")
        elif action == "reject" and t.status in ("proposed", "pending"):
            rt.tasks.update(t, status="failed", result="rejected from the web UI")
        elif action == "retry" and t.status in ("failed", "blocked", "done"):
            rt.tasks.update(t, status="pending")
        elif action == "interrupt":
            live = rt.runner.agents.get(t.id)
            if live:
                live.interrupt()
        elif action == "steer":
            live = rt.runner.agents.get(t.id)
            text = str((await body(req)).get("text", "")).strip()
            if not live or not text:
                raise web.HTTPConflict(text="task is not running")
            live.steer(text)
        else:
            raise web.HTTPConflict(text=f"cannot {action} a {t.status} task")
        wake()
        await changed("tasks", t.project)
        return web.json_response(task_dict(t))

    @r.post("/api/daemon/{action}")
    async def daemon_action(req: web.Request) -> web.Response:
        if not rt.daemon:
            raise web.HTTPConflict(text="daemon not running (start `cadence web` without --no-daemon)")
        action = req.match_info["action"]
        if action not in ("pause", "resume"):
            raise web.HTTPNotFound()
        rt.daemon.paused = action == "pause"
        rt.daemon.wake()
        return web.json_response({"paused": rt.daemon.paused})

    # ───────────────────────────────────────────────────────────── ideas
    def idea_dict(i: Idea) -> dict[str, Any]:
        d = asdict(i)
        d["project"] = norm(i.project)
        return d

    @r.get("/api/ideas")
    async def ideas(req: web.Request) -> web.Response:
        pid = pid_of(req)
        items = [i for i in rt.ideas.all() if pid is None or norm(i.project) == pid]
        return web.json_response([idea_dict(i) for i in reversed(items)])

    @r.post("/api/ideas")
    async def add_idea(req: web.Request) -> web.Response:
        data = await body(req)
        title = str(data.get("title", "")).strip()
        if not title:
            raise web.HTTPBadRequest(text="title required")
        i = rt.ideas.add(Idea(title=title[:160], description=str(data.get("description", "")),
                              size="goal" if data.get("size") == "goal" else "task", acceptance=_strs(data.get("acceptance")),
                              verify_command=str(data.get("verify_command", "") or ""), tags=_strs(data.get("tags")),
                              priority=_prio(data.get("priority")),
                              project=rt.project(str(data.get("project", ""))).id))
        await changed("ideas", i.project)
        return web.json_response(idea_dict(i))

    @r.post("/api/ideas/generate")
    async def generate(req: web.Request) -> web.Response:
        data = await body(req)
        p = rt.project(str(data.get("project", "")))
        added = await ideate(rt.router, rt.ideas, rt.tasks, rt.memory, project=p.id, workspace=p.root,
                             goals=rt.goals.list(project=p.id), n=max(1, min(6, int(data.get("n", 3)))))
        await changed("ideas", p.id)
        return web.json_response([idea_dict(i) for i in added])

    @r.patch("/api/ideas/{iid}")
    async def edit_idea(req: web.Request) -> web.Response:
        i = rt.ideas.get(req.match_info["iid"])
        if not i:
            raise web.HTTPNotFound(text="no such idea")
        data = await body(req)
        changes: dict[str, Any] = {k: str(data[k]) for k in ("title", "description", "verify_command") if k in data}
        if "priority" in data:
            changes["priority"] = _prio(data["priority"], i.priority)
        if "size" in data:
            changes["size"] = "goal" if data["size"] == "goal" else "task"
        if "status" in data:
            if data["status"] not in IDEA_STATUSES:
                raise web.HTTPBadRequest(text=f"status must be one of {', '.join(IDEA_STATUSES)}")
            changes["status"] = data["status"]
        for k in ("acceptance", "tags"):
            if k in data:
                changes[k] = _strs(data[k])
        rt.ideas.update(i, **changes)
        await changed("ideas", i.project)
        return web.json_response(idea_dict(i))

    @r.delete("/api/ideas/{iid}")
    async def delete_idea(req: web.Request) -> web.Response:
        if not rt.ideas.delete(req.match_info["iid"]):
            raise web.HTTPNotFound(text="no such idea")
        await changed("ideas")
        return web.json_response({"ok": True})

    @r.post("/api/ideas/{iid}/promote")
    async def promote(req: web.Request) -> web.Response:
        i = rt.ideas.get(req.match_info["iid"])
        if not i:
            raise web.HTTPNotFound(text="no such idea")
        if i.status == "promoted":
            raise web.HTTPConflict(text=f"already promoted to {i.promoted_to}")
        to = (await body(req)).get("to", i.size)
        if to == "goal":
            g = rt.goals.add(Goal(title=i.title, description=i.description, success=list(i.acceptance), project=i.project, priority=i.priority))
            rt.goals.log(g, "created", f"promoted from idea {i.id}")
            rt.ideas.update(i, status="promoted", promoted_to=f"goal:{g.id}")
            result: dict[str, Any] = {"goal": goal_dict(g)}
        else:
            t = promote_to_task(rt.ideas, rt.tasks, i)
            result = {"task": task_dict(t)}
        wake()
        await changed("ideas", i.project)
        return web.json_response({**result, "idea": idea_dict(i)})
