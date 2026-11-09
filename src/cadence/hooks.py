"""Lifecycle hooks: shell commands from config and Python callbacks from plugins.

Events and their payloads (all also carry `event`, `workspace`, `agent`):

    session_start  {}                                   first turn of an agent
    user_prompt    {prompt}                             before the model sees a user message
    pre_tool       {tool, arguments}                    before a tool runs  (matcher: tool-name glob)
    post_tool      {tool, arguments, result}            after a tool runs   (matcher: tool-name glob)
    stop           {text}                               agent finished a turn
    task_done      {task_id, goal_id, title, status, result}   a task reached a terminal status ("goal_done" is an alias)
    goal_achieved  {goal_id, title, assessment}         the planner judged a long-horizon goal achieved
    goal_planned   {goal_id, title, progress, assessment, queued}   the planner evaluated a goal and queued its next tasks
    heartbeat      {cycle, ideas, lessons}              an idle daemon cycle finished (ideas brainstormed, lessons consolidated)
    reflection     {project, memories, ideas}           a reflection pass filed memories and ideas

Shell protocol (mirrors Claude Code): the payload is JSON on stdin.
  exit 0 → stdout, if any, is added as context (user_prompt: appended to the prompt;
           post_tool: appended to the tool result)
  exit 2 → block; stderr is the reason (pre_tool: the call is refused; user_prompt: the turn is refused)
  other  → logged, never blocks

Python hooks receive the payload dict and may return None, a context string, or a `HookResult`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import HookConfig

log = logging.getLogger(__name__)

EVENTS = ("session_start", "user_prompt", "pre_tool", "post_tool", "stop", "task_done", "goal_achieved",
          "goal_planned", "heartbeat", "reflection")
ALIASES = {"goal_done": "task_done"}  # ≤0.2 name
MATCHED = ("pre_tool", "post_tool")


@dataclass
class HookResult:
    block: bool = False
    reason: str = ""
    context: list[str] = field(default_factory=list)

    def merge(self, other: "HookResult") -> None:
        self.context.extend(other.context)
        if other.block and not self.block:
            self.block, self.reason = True, other.reason

    @property
    def text(self) -> str:
        return "\n".join(c for c in self.context if c.strip())


@dataclass
class _PyHook:
    event: str
    fn: Callable[[dict[str, Any]], Any]
    matcher: str = "*"
    owner: str = ""


class Hooks:
    def __init__(self, shell_hooks: list[HookConfig] | None = None, workspace: Path | None = None):
        self.workspace = Path(workspace or Path.cwd())
        self.shell: list[HookConfig] = []
        self.py: list[_PyHook] = []
        self.set_shell(shell_hooks or [])

    def set_shell(self, hooks: list[HookConfig]) -> None:
        """Replace the shell hooks in place (scoped copies share this list, so they see the change)."""
        for h in hooks:
            h.event = ALIASES.get(h.event, h.event)
            self._check(h.event)
        self.shell[:] = hooks

    def scoped(self, workspace: Path) -> "Hooks":
        """Same hooks, run with a different working directory (one per project)."""
        clone = Hooks.__new__(Hooks)
        clone.workspace, clone.shell, clone.py = Path(workspace), self.shell, self.py
        return clone

    @staticmethod
    def _check(event: str) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown hook event {event!r}; expected one of {', '.join(EVENTS)}")

    def on(self, event: str, fn: Callable[[dict[str, Any]], Any], matcher: str = "*", owner: str = "") -> None:
        event = ALIASES.get(event, event)
        self._check(event)
        self.py.append(_PyHook(event, fn, matcher, owner))

    def remove_owner(self, owner: str) -> None:
        self.py[:] = [h for h in self.py if h.owner != owner]

    def count(self) -> int:
        return len(self.shell) + len(self.py)

    @staticmethod
    def _matches(event: str, matcher: str, key: str) -> bool:
        return event not in MATCHED or fnmatch.fnmatch(key, matcher or "*")

    async def emit(self, event: str, payload: dict[str, Any], key: str = "") -> HookResult:
        """Run every hook for `event` (in registration order: shell first, then Python)."""
        result = HookResult()
        if not (self.shell or self.py):
            return result
        payload = {"event": event, "workspace": str(self.workspace), **payload}
        for h in self.shell:
            if h.event == event and self._matches(event, h.matcher, key):
                result.merge(await self._run_shell(h, payload))
                if result.block:
                    return result
        for h in self.py:
            if h.event == event and self._matches(event, h.matcher, key):
                result.merge(await self._run_py(h, payload))
                if result.block:
                    return result
        return result

    async def _run_shell(self, h: HookConfig, payload: dict[str, Any]) -> HookResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                h.command, cwd=self.workspace, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env={**os.environ, "CADENCE_HOOK_EVENT": payload["event"],
                                                                     "KESTREL_HOOK_EVENT": payload["event"]})  # pre-rename hook scripts
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload, default=str).encode()), h.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("hook %r timed out after %.0fs", h.command, h.timeout)
            return HookResult()
        except OSError as exc:
            log.warning("hook %r failed to start: %s", h.command, exc)
            return HookResult()
        stdout, stderr = out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()
        if proc.returncode == 0:
            return HookResult(context=[stdout] if stdout else [])
        if proc.returncode == 2:
            return HookResult(block=True, reason=stderr or stdout or f"blocked by hook: {h.command}")
        log.warning("hook %r exited %s: %s", h.command, proc.returncode, stderr[:200])
        return HookResult()

    @staticmethod
    async def _run_py(h: _PyHook, payload: dict[str, Any]) -> HookResult:
        try:
            value = h.fn(dict(payload))
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:  # noqa: BLE001 — a plugin bug must not take the agent down
            log.warning("plugin hook %s/%s raised %s: %s", h.owner, h.event, exc.__class__.__name__, exc)
            return HookResult()
        if isinstance(value, HookResult):
            return value
        if isinstance(value, str) and value.strip():
            return HookResult(context=[value])
        return HookResult()
