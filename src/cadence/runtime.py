"""Runtime: wires config, models, tools, MCP, skills, memory, hooks, plugins, projects, goals and the daemon.

Tools are layered: the global registry holds MCP and plugin tools; each project's *scope* forks it and
adds the built-in file/shell tools, canvas and browser bound to that project's directory; each agent
forks its scope and adds its own (todos, memory, subagents). Forks see their parent live, so servers
and plugins added from the UI reach agents that are already running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .agent import Agent
from .autonomy import Daemon, Notify
from .browser import BrowserManager, browser_tools
from .canvas import CanvasStore, canvas_tools
from .config import Config, McpServerConfig, _apply, read_settings, write_settings
from .goals import GoalStore, Planner
from .hooks import Hooks
from .ideas import IdeaStore, ideate
from .llm import ModelRouter
from .mcp_host import McpHost
from .memory import MemoryStore, build_memory
from .reflect import reflect, transcript
from .migrate import migrate_project
from .plugins import PluginManager
from .projects import Project, ProjectStore
from .sandbox import Sandbox
from .skills import SkillRegistry, skill_tools
from .tasks import TaskRunner, TaskStore
from .tools import ToolRegistry, Workspace, builtin_tools

Listener = Callable[[dict[str, Any]], Awaitable[None]]


log = logging.getLogger(__name__)

class ScopedConfig:
    """The global config seen from one project: `workspace` and `skill_dirs` differ, everything else is
    read live from the global config (so settings changed in the UI apply to every project)."""

    def __init__(self, base: Config, workspace: Path, skill_dirs: list[Path]):
        self.__dict__["_base"] = base
        self.__dict__["workspace"] = workspace
        self.__dict__["skill_dirs"] = skill_dirs

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._base, name, value)


class Scope:
    def __init__(self, rt: "Runtime", project: Project):
        self.project = project
        root = project.root
        base = rt.cfg
        global_dirs = [d for d in base.skill_dirs if base.workspace not in Path(d).resolve().parents]
        self.cfg = ScopedConfig(base, root, global_dirs + [root / ".cadence" / "skills", root / ".agents" / "skills"])
        self.workspace = Workspace(root)
        self.sandbox = Sandbox(base.sandbox, root)  # shares the SandboxConfig object: mode changes apply live
        self.hooks = rt.hooks.scoped(root)
        self.skills = SkillRegistry(self.cfg.skill_dirs, base.disabled_skills)
        self.skills.scan()
        self.registry = rt.registry.fork()
        for tool in builtin_tools(self.workspace, allow_shell=base.allow_shell, output_limit=base.tool_output_limit,
                                  sandbox=self.sandbox, vision=rt.router.vision):
            self.registry.register(tool)
        for tool in skill_tools(self.skills, self.workspace, self.sandbox, timeout=base.tool_timeout,
                                output_limit=base.tool_output_limit, allow_shell=base.allow_shell):
            self.registry.register(tool)
        for tool in canvas_tools(rt.canvas, project.id, rt.publish, self.workspace):
            self.registry.register(tool)
        if base.browser.get("enabled", True):
            for tool in browser_tools(rt.browser, project.id, rt.router.vision):
                self.registry.register(tool)

    def instructions(self) -> str:
        p = self.project
        out = f"## Project: {p.name}\nAll work happens in {p.path}."
        if p.description:
            out += f"\n{p.description}"
        if p.instructions:
            out += f"\n\n{p.instructions}"
        return out


class Runtime:
    def __init__(self, cfg: Config, router: ModelRouter | None = None, memory: MemoryStore | None = None):
        self.cfg = cfg
        self.router = router or ModelRouter.from_config(cfg)
        self.registry = ToolRegistry()  # global: MCP + plugin tools
        self.skills = SkillRegistry(cfg.skill_dirs, cfg.disabled_skills)
        self.memory = memory or build_memory(cfg)
        self.projects = ProjectStore(cfg.state_dir / "projects.json")
        self.tasks = TaskStore(cfg.state_dir / "tasks.json")
        self.goals = GoalStore(cfg.state_dir / "goals.json")
        self.ideas = IdeaStore(cfg.state_dir / "ideas.json")
        self.canvas = CanvasStore(cfg.state_dir / "canvas.json")
        self.mcp = McpHost(cfg.mcp_servers, self.registry)
        self.sandbox = Sandbox(cfg.sandbox, cfg.workspace)
        self.hooks = Hooks(cfg.hooks, cfg.workspace)
        self.plugins = PluginManager(cfg.plugin_dirs, cfg.disabled_plugins)
        self.browser = BrowserManager(cfg, self.publish)
        self.listeners: list[Listener] = []
        self._scopes: dict[str, Scope] = {}
        for p in self.projects.all():  # one-time Kestrel → Cadence: <project>/.kestrel → .cadence
            migrate_project(p.root)
        self.default_project = self.projects.ensure(cfg.workspace)
        self.planner = Planner(self.router, self.goals, self.tasks, self.memory, cfg)
        self.runner = TaskRunner(cfg, self.router, self.tasks, self.memory, self.new_agent, self.sandbox, self.hooks,
                                 sandbox_for=lambda pid: self.scope(pid).sandbox)
        self.runner.on_finish = lambda agent, _task: self.reflect_agent(agent)
        self.daemon: Daemon | None = None
        self._idea_turn = 0
        self._reflect_lock = asyncio.Lock()
        self.restart_required: set[str] = set()

    async def start(self) -> "Runtime":
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.skills.scan()
        for d in self.canvas.all():  # drafts left open by a crash
            if d.streaming:
                self.canvas.update(d, streaming=False)
        dropped = self.memory.dedupe()  # consolidation used to pile up near-identical lessons
        if dropped:
            log.info("memory: dropped %d near-duplicate memories", dropped)
        await self.mcp.start()
        self.plugins.load(self.registry, self.hooks, self.cfg, self.memory)  # last, so plugins may wrap anything
        return self

    # ---------------------------------------------------------------- events
    async def publish(self, msg: dict[str, Any]) -> None:
        for fn in list(self.listeners):
            await fn(msg)

    # -------------------------------------------------------------- projects
    def project(self, pid: str = "") -> Project:
        return self.projects.get(pid) if pid and self.projects.get(pid) else self.default_project

    def scope(self, pid: str = "") -> Scope:
        project = self.project(pid)
        cached = self._scopes.get(project.id)
        if cached is None or cached.project.path != project.path:
            cached = self._scopes[project.id] = Scope(self, project)
        cached.project = project  # name/instructions edits apply without a rebuild
        return cached

    def forget_scopes(self) -> None:
        """Rebuild scopes on next use (built-in tools captured settings such as allow_shell)."""
        self._scopes.clear()

    def new_agent(self, name: str = "main", project: str = "") -> Agent:
        sc = self.scope(project)
        return Agent(sc.cfg, self.router, sc.registry, sc.skills, self.memory, name=name, hooks=sc.hooks,  # type: ignore[arg-type]
                     project=sc.project.id, extra_instructions=sc.instructions())

    def load_agent(self, session_id: str, name: str = "web") -> Agent:
        path = self.cfg.state_dir / "sessions" / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))  # OSError / ValueError if unknown
        agent = self.new_agent(data.get("agent") or name, data.get("project", ""))
        agent.load_session(session_id)
        return agent

    def reload_skills(self) -> None:
        self.skills.scan()
        for sc in self._scopes.values():
            sc.skills.scan()

    # --------------------------------------------------------------- settings
    async def apply_settings(self, patch: dict[str, Any]) -> set[str]:
        """Persist a partial config (same keys as config.yaml) to settings.json and apply it live.
        Returns the sections that only take effect after a restart."""
        stored = read_settings(self.cfg)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(stored.get(key), dict) and key not in ("mcp_servers",):
                stored[key] = {**stored[key], **value}
            else:
                stored[key] = value
        write_settings(self.cfg, stored)
        old_memory = (self.cfg.memory.backend, self.cfg.memory.embed_model, self.cfg.memory.embed_url, self.cfg.memory.qdrant_url)
        _apply(self.cfg, patch, replace_lists=True)
        keys = set(patch)
        if keys & {"big", "small", "vision"}:
            self.router.reconfigure(self.cfg)
        if "hooks" in keys:
            self.hooks.set_shell(self.cfg.hooks)
        if "mcp_servers" in keys:
            await self.mcp.sync(self.cfg.mcp_servers)
        if "plugins" in keys:
            self.plugins.sync(self.cfg.disabled_plugins)
        if "skills" in keys:
            self.reload_skills()
        if keys & {"allow_shell", "tool_output_limit", "browser"}:
            self.forget_scopes()
        if "memory" in keys and old_memory != (self.cfg.memory.backend, self.cfg.memory.embed_model,
                                                self.cfg.memory.embed_url, self.cfg.memory.qdrant_url):
            self.restart_required.add("memory")
        if self.daemon:
            self.daemon.wake()
        return set(self.restart_required)

    async def set_mcp(self, servers: list[McpServerConfig]) -> None:
        await self.apply_settings({"mcp_servers": {s.name: {"command": s.command, "args": s.args, "env": s.env,
                                                             "enabled": s.enabled} for s in servers}})

    # ----------------------------------------------------------------- daemon
    async def _ideate(self) -> int:
        """One idea cycle, rotating through projects (those with active goals first)."""
        projects = [p for p in self.projects.all() if p.root.is_dir() and not p.archived]
        if not projects:
            return 0
        projects.sort(key=lambda p: (not self.goals.list("active", project=p.id), -p.updated))
        p = projects[self._idea_turn % len(projects)]
        self._idea_turn += 1
        added = await ideate(self.router, self.ideas, self.tasks, self.memory, project=p.id, workspace=p.root,
                             goals=self.goals.list(project=p.id), auto_approve=self.cfg.auto_approve_ideas)
        if added:
            await self.publish({"kind": "ideas", "project": p.id, "count": len(added)})
        return len(added)

    async def reflect_agent(self, agent: Agent) -> None:
        """Reflection pass over an agent's latest turn: files memories and ideas (see reflect.py). Never raises."""
        if not self.cfg.reflect_after_turns:
            return
        try:
            log_text = transcript(agent.ctx.messages)
            async with self._reflect_lock:  # one at a time: it is a model call and the server may be busy
                done = await reflect(self.router, self.memory, self.ideas, self.tasks, project=agent.project, log_text=log_text)
            if done["memories"] or done["ideas"]:
                bits = [f"{n} {w}" for w, n in (("memory" if done["memories"] == 1 else "memories", done["memories"]),
                                                ("idea" if done["ideas"] == 1 else "ideas", done["ideas"])) if n]
                await self.hooks.emit("reflection", {"agent": agent.name, "project": agent.project, **done})
                await self.publish({"kind": "ideas", "project": agent.project, "count": done["ideas"]})
                await self.publish({"kind": "memory", "project": agent.project})
                await self.publish({"kind": "notice", "level": "info", "message": f"Reflection filed {' and '.join(bits)}", "at": time.time()})
        except Exception:  # noqa: BLE001
            log.exception("reflection failed")

    def make_daemon(self, notify: Notify | None = None, event_sink=None) -> Daemon:
        self.daemon = Daemon(self.cfg, self.runner, self.tasks, notify, event_sink, planner=self.planner,
                             workspace_for=lambda pid: self.project(pid).root, ideate=self._ideate,
                             archived=lambda: {p.id for p in self.projects.all() if p.archived})
        return self.daemon

    async def close(self) -> None:
        await self.browser.close()
        await self.mcp.stop()
        await self.memory.aclose()
        await self.router.aclose()
