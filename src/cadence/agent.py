"""The agent loop: streaming, tool use, steering, interrupts, dynamic compaction.

`Agent.run()` is an async generator of `Event`s so any front end (Rich CLI, daemon,
tests) can render or record the same stream.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .config import Config
from .hooks import Hooks
from .context import CompactionEvent, ContextManager
from .llm import Completion, LLMError, ModelRouter, StreamDelta, ToolCall, estimate_tokens
from .memory import KINDS, MemoryStore, Scratchpad
from .skills import SkillRegistry
from .tools import Tool, ToolRegistry, _schema, truncate
from .vision import ImageError, user_content

MAX_SUBAGENT_DEPTH = 2

SYSTEM_PROMPT = """You are Cadence, an autonomous coding agent working inside a local workspace.

## How you work
- Understand the goal, inspect the code before changing it, make focused edits, then VERIFY by running tests/linters/commands. Never claim success without evidence from a tool result.
- Keep a task list with `todo_write` for any work with 3+ steps; update it as you go.
- Prefer `grep`/`list_files`/`read_file` over guessing. Read before you edit. Use `edit_file` for surgical changes.
- Use `scratchpad_set` for notes you will need later; use `memory_add` for durable lessons (kind: procedural for how-tos, semantic for facts, episodic for outcomes).
- If a skill below matches the task, call `load_skill` first and follow it.
- Delegate self-contained side tasks with `spawn_subagent` when it keeps your own context small.
- If a tool errors, read the message, adjust, and retry differently. Do not repeat the same failing call.
- Be concise. When finished, give a short final report: what changed, how you verified, anything left open.

## Environment
Workspace root: {workspace}
"""


@dataclass
class Event:
    type: str  # status|thinking|token|tool_args|tool_call|tool_result|compaction|message_done|interrupted|error
    data: dict[str, Any] = field(default_factory=dict)


class _Interrupted(Exception):
    pass


SYSTEM_SHARE = 0.4  # at most this fraction of the context window goes to the system prompt


def fit_by_weight(parts: list[tuple[int, str]], budget: int) -> str:
    """Join prompt sections (weight 1-10, higher = more important). When over `budget` characters the lowest-weight
    sections are cut first (halved, then dropped), so instructions and the user's task list outlive skill indexes."""
    texts = [t for _w, t in parts]
    order = sorted(range(len(parts)), key=lambda i: (parts[i][0], -i))
    for stage in ("halve", "drop"):
        for i in order:
            if sum(map(len, texts)) <= budget or parts[i][0] >= 9:
                break
            if stage == "halve" and len(texts[i]) > 800:
                texts[i] = texts[i][: len(texts[i]) // 2] + "\n…(trimmed to fit the context window)"
            elif stage == "drop":
                texts[i] = ""
    return "\n\n".join(t for t in texts if t)


class Agent:
    def __init__(
        self,
        cfg: Config,
        router: ModelRouter,
        base_registry: ToolRegistry,
        skills: SkillRegistry,
        memory: MemoryStore,
        *,
        name: str = "main",
        depth: int = 0,
        extra_instructions: str = "",
        hooks: Hooks | None = None,
        project: str = "",
    ):
        self.cfg = cfg
        self.router = router
        self.skills = skills
        self.memory = memory
        self.name = name
        self.depth = depth
        self.extra_instructions = extra_instructions
        self.hooks = hooks or Hooks(workspace=cfg.workspace)
        self.project = project
        self._started = False
        self.session_id = uuid.uuid4().hex[:10]
        self.scratchpad = Scratchpad(cfg.state_dir / "scratch" / f"{self.session_id}.json")
        self.todos: list[dict[str, str]] = []
        self.ctx = ContextManager(cfg, router.small)
        self.registry = base_registry.fork()
        self._recalled = ""
        self._steer: asyncio.Queue[str] = asyncio.Queue()
        self._cancel = asyncio.Event()
        self._last_signature: tuple[str, str] | None = None
        self._repeat = 0
        self.last_compactions: list[CompactionEvent] = []
        self._register_agent_tools()

    # ------------------------------------------------------------ external API
    def steer(self, text: str) -> None:
        """Inject guidance that the agent will see before its next model call."""
        self._steer.put_nowait(text)

    def interrupt(self) -> None:
        self._cancel.set()

    def reset(self) -> None:
        self.ctx.reset()
        self.todos.clear()
        self.scratchpad.notes.clear()

    def save_session(self) -> Path:
        path = self.cfg.state_dir / "sessions" / f"{self.session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"summary": self.ctx.summary, "messages": self.ctx.messages, "todos": self.todos,
                        "project": self.project, "agent": self.name}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def load_session(self, session_id: str) -> None:
        path = self.cfg.state_dir / "sessions" / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.session_id = session_id
        self.ctx.summary = data.get("summary", "")
        self.ctx.messages = data.get("messages", [])
        self.todos = data.get("todos", [])
        self.project = data.get("project", self.project)

    # ---------------------------------------------------------- system prompt
    def system_prompt(self) -> str:
        parts: list[tuple[int, str]] = [(10, SYSTEM_PROMPT.format(workspace=self.cfg.workspace))]
        for label, path in (
            ("SOUL.md", self.cfg.workspace / "SOUL.md"),
            ("AGENTS.md", self.cfg.workspace / "AGENTS.md"),
            ("global SOUL.md", self.cfg.home / "SOUL.md"),
        ):
            if path.is_file():
                parts.append((8, f"## {label}\n{truncate(path.read_text(encoding='utf-8', errors='ignore'), 6000)}"))
        if self.extra_instructions:
            parts.append((9, self.extra_instructions))
        surfaces = []
        if self.registry.get("canvas_write"):
            surfaces.append("- Canvas: put documents the user will read, review or edit (plans, reports, specs, drafts, "
                            "HTML mockups) on the canvas with `canvas_write` and keep chat replies short. "
                            "`canvas_read` before revising: the user may have edited it.")
        if self.registry.get("browser_open"):
            surfaces.append("- Browser: `browser_*` tools drive a real browser the user watches and can use too. "
                            "If a page needs a login or captcha, ask the user to do it in the browser panel, then `browser_snapshot`.")
        if surfaces:
            parts.append((5, "## Shared with the user\n" + "\n".join(surfaces)))
        index = self.skills.index()
        if index:
            parts.append((4, "## Available skills (load with `load_skill`)\n" + index))
        if self.todos:
            parts.append((8, "## Current task list\n" + render_todos(self.todos)))
        notes = self.scratchpad.render()
        if notes:
            parts.append((5, "## Scratchpad\n" + notes))
        if self._recalled:
            parts.append((6, "## Relevant memory\n" + self._recalled))
        return fit_by_weight(parts, int(self.cfg.big.context_window * SYSTEM_SHARE * 3.5))

    async def _recall(self, query: str) -> None:
        hits = await self.memory.asearch(query, k=5, project=self.project)
        self._recalled = "\n".join(f"- ({h.kind}) {h.text}" for h in hits)

    # ------------------------------------------------------------------- loop
    async def run(self, user_message: str, images: list[Path] | None = None) -> AsyncIterator[Event]:
        """One turn. `images` are attached to the user message for the (vision-capable) big model."""
        self._cancel.clear()
        if not self._started:
            self._started = True
            await self.hooks.emit("session_start", {"agent": self.name})
        gate = await self.hooks.emit("user_prompt", {"agent": self.name, "prompt": user_message})
        if gate.block:
            yield Event("error", {"message": f"prompt blocked by hook: {gate.reason}"})
            return
        if gate.text:
            user_message = f"{user_message}\n\n[context from hooks]\n{gate.text}"
        try:
            content = user_content(user_message, list(images or []))
        except ImageError as exc:
            yield Event("error", {"message": f"cannot attach image: {exc}"})
            return
        self.ctx.add({"role": "user", "content": content})
        await self._recall(user_message)
        empty_nudges = 0
        schemas = self.registry.schemas()
        schema_tokens = estimate_tokens(json.dumps(schemas))

        for iteration in range(1, self.cfg.max_iterations + 1):
            if self._cancel.is_set():
                yield Event("interrupted")
                return
            self._drain_steering()
            system = self.system_prompt()

            compaction_events = await self._compact(system, schema_tokens)
            for ev in compaction_events:
                yield Event("compaction", {"stage": ev.stage, "before": ev.tokens_before, "after": ev.tokens_after, "detail": ev.detail})

            messages = self.ctx.build(system)
            yield Event("status", {"iteration": iteration, "tokens": self.ctx.usage(system, schema_tokens), "window": self.ctx.window})

            completion: Completion | None = None
            try:
                async for item in self._stream_completion(messages, schemas):
                    if isinstance(item, Completion):
                        completion = item
                    elif item.kind == "reasoning":
                        yield Event("thinking", {"text": item.text})
                    elif item.kind == "tool_args":
                        if item.name.startswith("canvas_write"):  # lets the UI draw the document while the model writes it
                            yield Event("tool_args", {"id": item.call_id, "name": item.name, "text": item.text})
                    else:
                        yield Event("token", {"text": item.text})
            except _Interrupted:
                yield Event("interrupted")
                return
            except LLMError as exc:
                yield Event("error", {"message": str(exc)})
                return
            assert completion is not None

            self.ctx.add(completion.as_message())

            if not completion.tool_calls:
                if not completion.content.strip() and empty_nudges < 2:
                    empty_nudges += 1
                    self.ctx.add({"role": "user", "content": "Your last reply was empty. Continue the task or give your final report."})
                    continue
                self.save_session()
                await self.hooks.emit("stop", {"agent": self.name, "text": completion.content})
                yield Event("message_done", {"text": completion.content, "iterations": iteration})
                return

            for call in completion.tool_calls:
                yield Event("tool_call", {"id": call.id, "name": call.name, "arguments": call.arguments})

            try:
                results = await self._execute_interruptible(completion.tool_calls)
            except _Interrupted:
                for call in completion.tool_calls:
                    self.ctx.add({"role": "tool", "tool_call_id": call.id, "content": "ERROR: interrupted by user"})
                yield Event("interrupted")
                return

            for call, output in zip(completion.tool_calls, results):
                output = self._guard_repeats(call, output)
                output = truncate(output, self.cfg.tool_output_limit)
                self.ctx.add({"role": "tool", "tool_call_id": call.id, "content": output})
                yield Event("tool_result", {"id": call.id, "name": call.name, "result": output})

        self.save_session()
        yield Event("message_done", {"text": f"Stopped: reached the iteration limit ({self.cfg.max_iterations}). Progress is saved; continue to resume.", "iterations": self.cfg.max_iterations})

    async def run_to_completion(self, user_message: str, images: list[Path] | None = None) -> str:
        """Drive `run()` to the end and return the final text (used by subagents/goals/channels)."""
        final = ""
        async for ev in self.run(user_message, images):
            if ev.type == "message_done":
                final = ev.data["text"]
            elif ev.type == "error":
                raise LLMError(ev.data["message"])
            elif ev.type == "interrupted":
                final = final or "(interrupted)"
        return final

    # ---------------------------------------------------------------- internals
    def _drain_steering(self) -> None:
        while not self._steer.empty():
            self.ctx.add({"role": "user", "content": f"[steering from user — adjust course accordingly] {self._steer.get_nowait()}"})

    async def _compact(self, system: str, schema_tokens: int) -> list[CompactionEvent]:
        events = await self.ctx.maybe_compact(system, schema_tokens)
        self.last_compactions = events
        return events

    async def _stream_completion(self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> AsyncIterator[StreamDelta | Completion]:
        queue: asyncio.Queue[StreamDelta] = asyncio.Queue()

        async def on_delta(delta: StreamDelta) -> None:
            await queue.put(delta)

        task = asyncio.create_task(self.router.big.complete(messages, schemas, on_delta=on_delta))
        cancel_wait = asyncio.create_task(self._cancel.wait())
        getter: asyncio.Task[StreamDelta] | None = None
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({getter, task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
                if cancel_wait in done:
                    task.cancel()
                    raise _Interrupted
                if getter in done:
                    yield getter.result()
                    getter = None
                    continue
                getter.cancel()
                getter = None
                while not queue.empty():
                    yield queue.get_nowait()
                yield task.result()  # raises LLMError if the call failed
                return
        finally:
            for t in (getter, task, cancel_wait):
                if t is not None and not t.done():
                    t.cancel()

    async def _execute_interruptible(self, calls: list[ToolCall]) -> list[str]:
        exec_task = asyncio.create_task(self._execute(calls))
        cancel_wait = asyncio.create_task(self._cancel.wait())
        try:
            done, _ = await asyncio.wait({exec_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
            if exec_task in done:
                return exec_task.result()
            exec_task.cancel()
            try:
                await exec_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            raise _Interrupted
        finally:
            cancel_wait.cancel()

    async def _execute(self, calls: list[ToolCall]) -> list[str]:
        async def one(call: ToolCall) -> str:
            try:
                args = call.parsed()
            except ValueError as exc:
                return f"ERROR: {exc}. Re-issue the call with valid JSON arguments."
            payload = {"agent": self.name, "tool": call.name, "arguments": args}
            pre = await self.hooks.emit("pre_tool", payload, key=call.name)
            if pre.block:
                return f"ERROR: blocked by hook: {pre.reason}. Choose a different approach."
            output = await self.registry.call(call.name, args, self.cfg.tool_timeout)
            post = await self.hooks.emit("post_tool", {**payload, "result": output}, key=call.name)
            return f"{output}\n\n[hook] {post.text}" if post.text else output

        tools = [self.registry.get(c.name) for c in calls]
        if len(calls) > 1 and all(t is not None and t.read_only for t in tools):
            return list(await asyncio.gather(*(one(c) for c in calls)))
        return [await one(c) for c in calls]

    def _guard_repeats(self, call: ToolCall, output: str) -> str:
        sig = (call.name, call.arguments)
        self._repeat = self._repeat + 1 if sig == self._last_signature else 1
        self._last_signature = sig
        if self._repeat >= 3:
            return output + "\n\nWARNING: you have issued this identical call 3+ times. Change your approach."
        return output

    # ------------------------------------------------------------ agent tools
    def _register_agent_tools(self) -> None:
        reg = self.registry

        async def load_skill(args: dict[str, Any]) -> str:
            skill = self.skills.get(str(args.get("name", "")))
            if skill is None:
                return "ERROR: unknown skill. Available: " + ", ".join(self.skills.skills) if self.skills.skills else "ERROR: no skills installed"
            return truncate(skill.render(), 12000)

        async def memory_add(args: dict[str, Any]) -> str:
            kind = str(args.get("kind", "procedural"))
            if kind not in KINDS:
                return f"ERROR: kind must be one of {', '.join(KINDS)}"
            item = self.memory.add(kind, str(args.get("text", "")), list(args.get("tags") or []), self.project)
            return f"stored {item.kind} memory {item.id}"

        async def memory_search(args: dict[str, Any]) -> str:
            hits = await self.memory.asearch(str(args.get("query", "")), k=int(args.get("k", 5) or 5), kind=args.get("kind"), project=self.project)
            return "\n".join(f"[{h.kind}] {h.text}" for h in hits) or "(nothing found)"

        async def scratchpad_set(args: dict[str, Any]) -> str:
            key, value = str(args.get("key", "")).strip(), args.get("value")
            if not key:
                return "ERROR: key required"
            if value in (None, ""):
                return "removed" if self.scratchpad.delete(key) else "no such key"
            self.scratchpad.set(key, str(value))
            return f"noted '{key}'"

        async def todo_write(args: dict[str, Any]) -> str:
            items = args.get("todos")
            if not isinstance(items, list):
                return "ERROR: 'todos' must be a list of {content, status}"
            clean: list[dict[str, str]] = []
            for it in items:
                if not isinstance(it, dict) or not str(it.get("content", "")).strip():
                    return "ERROR: each todo needs non-empty 'content'"
                status = str(it.get("status", "pending"))
                if status not in ("pending", "in_progress", "done"):
                    return "ERROR: status must be pending, in_progress or done"
                clean.append({"content": str(it["content"]).strip(), "status": status})
            self.todos = clean
            return render_todos(clean)

        async def spawn_subagent(args: dict[str, Any]) -> str:
            if self.depth >= MAX_SUBAGENT_DEPTH:
                return "ERROR: subagent depth limit reached; do this task yourself"
            task = str(args.get("task", "")).strip()
            if not task:
                return "ERROR: 'task' required"
            sub = Agent(self.cfg, self.router, self.registry, self.skills, self.memory,
                        name=f"{self.name}/sub", depth=self.depth + 1, hooks=self.hooks, project=self.project,
                        extra_instructions=(self.extra_instructions + "\n\n" if self.extra_instructions else "")
                        + "You are a focused subagent. Complete the task and return a compact report.")
            return truncate(await sub.run_to_completion(task), 6000)

        reg.register(Tool("load_skill", "Load the full instructions of a named skill.",
                          _schema({"name": {"type": "string"}}, ["name"]), load_skill, source="agent", read_only=True))
        reg.register(Tool("memory_add", "Persist a durable memory (episodic|semantic|procedural).",
                          _schema({"kind": {"type": "string", "enum": list(KINDS)}, "text": {"type": "string"},
                                   "tags": {"type": "array", "items": {"type": "string"}}}, ["kind", "text"]), memory_add, source="agent"))
        reg.register(Tool("memory_search", "Search long-term memory by meaning (semantic + keyword).",
                          _schema({"query": {"type": "string"}, "k": {"type": "integer"}, "kind": {"type": "string"}}, ["query"]),
                          memory_search, source="agent", read_only=True))
        reg.register(Tool("scratchpad_set", "Set a working note; empty value deletes it.",
                          _schema({"key": {"type": "string"}, "value": {"type": "string"}}, ["key"]), scratchpad_set, source="agent"))
        reg.register(Tool("todo_write", "Replace the task list (work breakdown). Statuses: pending|in_progress|done.",
                          _schema({"todos": {"type": "array", "items": _schema(
                              {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
                              ["content", "status"])}}, ["todos"]), todo_write, source="agent"))
        if self.depth < MAX_SUBAGENT_DEPTH:
            reg.register(Tool("spawn_subagent", "Run a self-contained task in a fresh-context subagent and get its report.",
                              _schema({"task": {"type": "string"}}, ["task"]), spawn_subagent, source="agent"))


def render_todos(todos: list[dict[str, str]]) -> str:
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    return "\n".join(f"{marks.get(t['status'], '[ ]')} {t['content']}" for t in todos)
