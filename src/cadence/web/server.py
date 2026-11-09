"""Web GUI backend: REST for state, one WebSocket for live events, static React build.

    cadence web                 # http://127.0.0.1:8765 — runs chat sessions, the daemon and channels

Security: binds to loopback by default. Every request must carry a loopback (or configured) Host header,
which defeats DNS rebinding, and state-changing requests/WebSockets must come from a matching Origin,
which stops other websites in your browser from driving the agent. Binding to a non-loopback address
requires a token (auto-generated and printed if not given).

Routes are split by area: work.py (projects, goals, tasks, ideas), extensions.py (settings, models,
skills, plugins, MCP, hooks), files.py (file explorer, canvas, browser).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import time
import uuid
from dataclasses import asdict
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from ..agent import Agent, Event
from ..canvas import FORMATS
from ..llm import LLMError, OpenAICompatLLM, content_text
from ..runtime import Runtime
from ..documents import ExtractError, extract
from ..vision import MAX_IMAGE_BYTES, MIME

MAX_ATTACHMENT = 50 * 1024 * 1024

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
STEER_PREFIX = "[steering from user — adjust course accordingly] "
HOOK_MARK = "\n\n[context from hooks]\n"
ATTACH_MARK = "\n\n[attached files]\n"
ATTACH_RE = re.compile(r"^### (.+?) \((.+?)\) — saved as (.+)$", re.M)
ATTACH_CHARS = 15000  # extracted text per attachment placed in the message; the agent can read_file the rest
WRITE_TOOLS = ("write_file", "edit_file", "canvas_export")


# ───────────────────────────────────────────────────────────── transcripts
def transcript(agent: Agent) -> list[dict[str, Any]]:
    """Flatten an agent's context into UI items: user / steer / assistant / tool (call + result)."""
    items: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    for m in agent.ctx.messages:
        role, content = m.get("role"), m.get("content")
        if role == "user":
            text = content_text(content)
            if text.startswith(STEER_PREFIX):
                items.append({"role": "steer", "text": text[len(STEER_PREFIX):]})
                continue
            images = [p["image_url"]["url"] for p in content if isinstance(p, dict) and p.get("type") == "image_url"] \
                if isinstance(content, list) else []
            text = text.replace("[image]", "").strip().split(HOOK_MARK)[0]
            text, _, block = text.partition(ATTACH_MARK)
            files = [{"name": n, "kind": k, "path": p} for n, k, p in ATTACH_RE.findall(block)]
            items.append({"role": "user", "text": text, "images": images, "files": files})
        elif role == "assistant":
            if content_text(content).strip():
                items.append({"role": "assistant", "text": content_text(content)})
            for tc in m.get("tool_calls") or []:
                item = {"role": "tool", "id": tc["id"], "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", ""), "result": None}
                tools[tc["id"]] = item
                items.append(item)
        elif role == "tool" and m.get("tool_call_id") in tools:
            tools[m["tool_call_id"]]["result"] = content_text(content)
    return items


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = content_text(m.get("content")).replace("[image]", "🖼").strip()
            if not text.startswith(STEER_PREFIX):
                head = text.split(HOOK_MARK)[0].split(ATTACH_MARK)[0].strip()
                if not head and ATTACH_MARK in text:
                    names = ATTACH_RE.findall(text)
                    head = "📎 " + ", ".join(n for n, _, _ in names)
                return head[:70] or "Image"
    return "New chat"


def _summary(sid: str, title: str, updated: float, running: bool, live: bool, project: str, agent: str = "") -> dict[str, Any]:
    """Sessions started by background tasks are flagged so the UI can label them."""
    kind = "task" if title.startswith(("TASK: ", "GOAL: ")) or agent.startswith(("task-", "goal-")) else "chat"
    return {"id": sid, "title": title.removeprefix("TASK: ").removeprefix("GOAL: "), "kind": kind, "updated": updated,
            "running": running, "live": live, "project": project}


def touched_path(name: str, arguments: Any) -> str:
    """The workspace path a file-writing tool call targets, so the explorer can mark agent edits."""
    if name not in WRITE_TOOLS:
        return ""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    except (ValueError, TypeError):
        return ""
    return str(args.get("path", ""))


# ──────────────────────────────────────────────────────────────── sessions
class LiveSession:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.task: asyncio.Task[None] | None = None
        self.usage: dict[str, int] = {}
        self.updated = time.time()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def summary(self) -> dict[str, Any]:
        a = self.agent
        return _summary(a.session_id, _first_user_text(a.ctx.messages), self.updated, self.running, True, a.project, a.name)


class Hub:
    """Owns live sessions and fans events out to every connected WebSocket."""

    def __init__(self, rt: Runtime):
        self.rt = rt
        self.sessions: dict[str, LiveSession] = {}
        self.clients: set[web.WebSocketResponse] = set()
        self._bg: set[asyncio.Task[None]] = set()  # fire-and-forget work (reflection) kept alive until done
        self.watching: dict[web.WebSocketResponse, set[str]] = {}  # ws → projects whose browser it streams
        self.notices: list[dict[str, Any]] = []
        self.task_activity: dict[str, dict[str, Any]] = {}
        self.inbox = rt.cfg.state_dir / "uploads"
        rt.listeners.append(self.broadcast)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        if msg.get("kind") == "browser_frame":  # frames are large: only to viewers of that browser
            targets = [ws for ws, ps in self.watching.items() if msg.get("project") in ps]
        else:
            targets = list(self.clients)
        data = json.dumps(msg, default=str)
        for ws in targets:
            try:
                await ws.send_str(data)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    # ------------------------------------------------------------ sessions
    def session_dir(self) -> Path:
        return self.rt.cfg.state_dir / "sessions"

    def get(self, sid: str) -> LiveSession:
        live = self.sessions.get(sid)
        if live:
            return live
        live = LiveSession(self.rt.load_agent(sid))  # raises OSError/ValueError if unknown
        live.updated = (self.session_dir() / f"{sid}.json").stat().st_mtime
        self.sessions[sid] = live
        return live

    def create(self, project: str = "") -> LiveSession:
        live = LiveSession(self.rt.new_agent("web", project))
        self.sessions[live.agent.session_id] = live
        return live

    def list(self, project: str | None = None) -> list[dict[str, Any]]:
        out = {sid: s.summary() for sid, s in self.sessions.items() if s.agent.ctx.messages or s.running}
        d = self.session_dir()
        if d.is_dir():
            for f in d.glob("*.json"):
                if f.stem in out or f.stem in self.sessions:
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if data.get("messages"):
                    out[f.stem] = _summary(f.stem, _first_user_text(data["messages"]), f.stat().st_mtime, False, False,
                                           data.get("project", ""), data.get("agent", ""))
        default = self.rt.default_project.id
        items = [s for s in out.values() if project is None or (s["project"] or default) == project]
        return sorted(items, key=lambda s: s["updated"], reverse=True)

    def delete(self, sid: str) -> None:
        live = self.sessions.pop(sid, None)
        if live and live.running:
            live.agent.interrupt()
        path = self.session_dir() / f"{sid}.json"
        if path.is_file():
            path.unlink()

    async def attach(self, sid: str, files: list[Path]) -> str:
        """Copy uploads into the project (so the agent can read_file them further) and inline their contents."""
        project = self.rt.project(self.get(sid).agent.project)
        folder = project.root / ".cadence" / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        blocks = []
        for src in files:
            original = src.name.split("-", 1)[1] if "-" in src.name else src.name
            dest = folder / original
            n = 1
            while dest.exists():
                n += 1
                dest = folder / f"{Path(original).stem}-{n}{Path(original).suffix}"
            shutil.copy2(src, dest)
            rel = str(dest.relative_to(project.root))
            try:
                ex = await extract(dest)
                kind = ex.kind + (f", {ex.pages} pages" if ex.pages else "")
                body = ex.text if len(ex.text) <= ATTACH_CHARS else (
                    ex.text[:ATTACH_CHARS] + f"\n…[{len(ex.text) - ATTACH_CHARS} more chars: read_file {rel} with offset/pages]")
                if ex.warning:
                    body = f"NOTE: {ex.warning}\n{body}"
            except ExtractError as exc:
                kind, body = "unreadable", f"(could not extract: {exc})"
            blocks.append(f"### {original} ({kind}) — saved as {rel}\n{body}")
        return ATTACH_MARK + "\n\n".join(blocks)

    # --------------------------------------------------------------- turns
    async def send(self, sid: str, text: str, images: list[Path]) -> str:
        live = self.get(sid)
        if live.running:
            live.agent.steer(text)
            await self.broadcast({"kind": "steer", "session": sid, "text": text})
            return "steered"
        live.task = asyncio.create_task(self._turn(live, text, images), name=f"web-turn-{sid}")
        return "started"

    async def _turn(self, live: LiveSession, text: str, images: list[Path]) -> None:
        a = live.agent
        sid = a.session_id
        live.updated = time.time()
        await self.broadcast({"kind": "turn", "session": sid, "state": "started", "project": a.project,
                              "title": _first_user_text(a.ctx.messages) if a.ctx.messages else text[:70]})
        streams: dict[str, dict[str, Any]] = {}
        daemon = self.rt.daemon
        if daemon:
            daemon.user_turns += 1  # a user directive in flight outranks background tasks (see Daemon._launch_ready)
        try:
            async for ev in a.run(text, images):
                if ev.type == "tool_args":  # a canvas document being written: draw it as it arrives
                    await self._stream_canvas(a.project, sid, streams, ev.data)
                    continue
                if ev.type == "status":
                    live.usage = {"tokens": ev.data["tokens"], "window": ev.data["window"], "iteration": ev.data["iteration"]}
                await self.broadcast({"kind": "agent", "session": sid, "event": {"type": ev.type, "data": ev.data}})
                await self._side_effects(a.project, ev, sid, a)
        except Exception as exc:  # noqa: BLE001 — surface, never kill the server
            log.exception("turn crashed")
            await self.broadcast({"kind": "agent", "session": sid, "event": {"type": "error", "data": {"message": f"{exc.__class__.__name__}: {exc}"}}})
        finally:
            if daemon:
                daemon.user_turns = max(0, daemon.user_turns - 1)
                daemon.wake()
            await self._end_streams(a.project, streams)
            live.updated = time.time()
            a.save_session()
            t = asyncio.create_task(self.rt.reflect_agent(a), name=f"reflect-{sid}")
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
            await self.broadcast({"kind": "turn", "session": sid, "state": "finished", "title": _first_user_text(a.ctx.messages),
                                  "project": a.project})

    async def _stream_canvas(self, project: str, sid: str, streams: dict[str, dict[str, Any]], d: dict[str, Any]) -> None:
        """Create the document as soon as the model starts a canvas_write, then keep it up to date (≈4×/s)."""
        from ..canvas import CanvasDoc, partial_args

        st = streams.setdefault(d["id"], {"raw": "", "doc": None, "sent": 0.0, "n": 0})
        st["raw"] += d["text"]
        f = partial_args(st["raw"])
        if f.get("id") or "content" not in f:  # editing an existing document, or the content has not started yet
            return
        now = time.monotonic()
        doc = st["doc"]
        if doc is None:
            fmt = f.get("format", "markdown")
            doc = st["doc"] = self.rt.canvas.add(CanvasDoc(title=(f.get("title") or "Untitled").strip()[:120] or "Untitled", format=fmt if fmt in FORMATS else "markdown",
                                                           project=project, session=sid, streaming=True))
            first = True
        elif now - st["sent"] < 0.25:
            return
        else:
            first = False
        st["sent"] = now
        self.rt.canvas.update(doc, content=f["content"], title=(f.get("title") or doc.title).strip()[:120] or doc.title)
        await self.broadcast({"kind": "canvas", "project": project, "doc": doc.summary(), "focus": first, "streaming": True,
                              "content": doc.content})

    async def _end_streams(self, project: str, streams: dict[str, dict[str, Any]]) -> None:
        """A turn ended with live drafts still open (interrupted or errored): keep what was written, or drop an empty one."""
        for st in streams.values():
            doc = st["doc"]
            if doc is None or self.rt.canvas.get(doc.id) is None or not doc.streaming:
                continue
            if doc.content.strip():
                self.rt.canvas.update(doc, streaming=False)
                await self.broadcast({"kind": "canvas", "project": project, "doc": doc.summary(), "focus": False})
            else:
                self.rt.canvas.delete(doc.id)
                await self.broadcast({"kind": "canvas_deleted", "project": project, "id": doc.id})

    async def _side_effects(self, project: str, ev: Event, sid: str = "", agent: Agent | None = None) -> None:
        if ev.type == "tool_call":
            path = touched_path(ev.data.get("name", ""), ev.data.get("arguments"))
            if path:
                await self.broadcast({"kind": "file_changed", "project": project or self.rt.default_project.id,
                                      "path": path, "by": "agent"})
        elif ev.type == "tool_result" and ev.data.get("name") == "todo_write" and agent is not None:
            await self.broadcast({"kind": "todos", "session": sid, "todos": agent.todos})

    # --------------------------------------------------------------- daemon
    async def notify(self, level: str, message: str) -> None:
        n = {"kind": "notice", "level": level, "message": message, "at": time.time()}
        self.notices = (self.notices + [n])[-100:]
        await self.broadcast(n)
        if level in ("task", "goal", "cycle"):  # daemon activity changed goals/tasks/ideas/memory: refresh open views
            await self.broadcast({"kind": "changed", "what": "work"})

    async def task_event(self, tid: str, ev: Event) -> None:
        task = self.rt.tasks.get(tid)
        if task:
            await self._side_effects(task.project, ev)
        if ev.type in ("tool_call", "message_done", "error", "compaction"):
            act = {"type": ev.type, "at": time.time(),
                   "text": ev.data.get("name") or ev.data.get("text", "")[:200] or ev.data.get("message", "") or ev.data.get("detail", "")}
            self.task_activity[tid] = act
            await self.broadcast({"kind": "task_activity", "task": tid, "activity": act})


HUB_KEY: web.AppKey["Hub"] = web.AppKey("hub")
CHANNELS_KEY: web.AppKey[list] = web.AppKey("channels")


# ──────────────────────────────────────────────────────────────── security
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname(value: str) -> str:
    return (urlsplit(f"//{value}").hostname or "") if value else ""


def security_middleware(allowed_hosts: set[str], token: str | None):
    @web.middleware
    async def mw(request: web.Request, handler):
        host = _hostname(request.headers.get("Host", ""))
        if not (_is_loopback(host) or host in allowed_hosts):
            return web.Response(status=403, text="host not allowed")
        if token:
            supplied = request.cookies.get("cadence_token") or request.query.get("token") or \
                request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied or "", token):
                return web.Response(status=401, text="token required: open the URL printed by `cadence web`")
        is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
        if request.method not in ("GET", "HEAD", "OPTIONS") or is_ws:
            origin = request.headers.get("Origin")
            if origin:
                o = urlsplit(origin).hostname or ""
                if not (_is_loopback(o) or o in allowed_hosts or o == host):
                    return web.Response(status=403, text="cross-origin request refused")
        resp = await handler(request)
        if token and request.query.get("token"):
            resp.set_cookie("cadence_token", token, httponly=True, samesite="Strict")
        return resp
    return mw


async def body(req: web.Request) -> dict[str, Any]:
    """The JSON body as a dict; a malformed one is a 400, not a 500."""
    if not req.can_read_body:
        return {}
    try:
        data = await req.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="expected a JSON body")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return data


# ────────────────────────────────────────────────────────────────── routes
def build_app(rt: Runtime, *, allowed_hosts: set[str] | None = None, token: str | None = None) -> web.Application:
    from . import extensions, files, work

    hub = Hub(rt)
    app = web.Application(middlewares=[security_middleware(allowed_hosts or set(), token)],
                          client_max_size=max(MAX_IMAGE_BYTES, files.MAX_UPLOAD) + 1024 * 1024)
    app[HUB_KEY] = hub
    r = web.RouteTableDef()

    @r.get("/api/status")
    async def status(req: web.Request) -> web.Response:
        cfg, d = rt.cfg, rt.daemon
        models = {}
        for role, mc in (("big", cfg.big), ("small", cfg.small)):
            probe = OpenAICompatLLM(mc)
            try:
                models[role] = {"name": mc.name, "url": mc.base_url, "ok": mc.name in await probe.list_models()}
            except LLMError as exc:
                models[role] = {"name": mc.name, "url": mc.base_url, "ok": False, "error": str(exc)[:160]}
            finally:
                await probe.aclose()
        scope = rt.scope(req.query.get("project", ""))
        return web.json_response({
            "version": __import__("cadence").__version__,
            "workspace": str(scope.project.path),
            "models": models,
            "vision": cfg.vision.name if cfg.vision else cfg.big.name,
            "memory": {**rt.memory.stats(), "backend": cfg.memory.backend, "embedder": cfg.memory.embed_model,
                       "error": rt.memory.last_error},
            "sandbox": {"mode": rt.sandbox.mode, "network": cfg.sandbox.network, "problem": rt.sandbox.check()},
            "plugins": [{"name": p.name, "origin": p.origin, "tools": p.tools} for p in rt.plugins.loaded.values()],
            "plugin_errors": rt.plugins.errors,
            "hooks": [{"event": h.event, "matcher": h.matcher, "command": h.command} for h in rt.hooks.shell]
                     + [{"event": h.event, "matcher": h.matcher, "command": f"plugin:{h.owner}"} for h in rt.hooks.py],
            "mcp": [{"name": n, "connected": c, "tools": t, "error": e} for n, c, t, e in rt.mcp.status()],
            "skills": [{"name": s.name, "description": s.description} for s in scope.skills.skills.values()],
            "tools": [{"name": t.name, "source": t.source, "description": t.description, "read_only": t.read_only}
                      for t in rt.new_agent("status", scope.project.id).registry.all().values()],  # agents add their own (memory, todos, subagents)
            "channels": [c.name for c in app.get(CHANNELS_KEY, [])],
            "restart_required": sorted(rt.restart_required),
            "daemon": None if d is None else {
                "running": d.running, "planning": d.planning, "cycles_today": d.cycles_today,
                "cycles_per_day": cfg.reflection_cycles, "interval_h": d.interval / 3600, "last_tick": d.last_tick,
                "paused": d.paused, "auto_approve": cfg.auto_approve_ideas, "max_parallel": cfg.max_parallel_goals},
            "notices": hub.notices[-30:],
        })

    @r.get("/api/sessions")
    async def sessions(req: web.Request) -> web.Response:
        return web.json_response(hub.list(req.query.get("project") or None))

    @r.post("/api/sessions")
    async def new_session(req: web.Request) -> web.Response:
        data = await body(req)
        return web.json_response(hub.create(str(data.get("project", ""))).summary())

    @r.get("/api/sessions/{sid}")
    async def get_session(req: web.Request) -> web.Response:
        try:
            live = hub.get(req.match_info["sid"])
        except (OSError, ValueError):
            raise web.HTTPNotFound(text="no such session")
        return web.json_response({**live.summary(), "items": transcript(live.agent), "todos": live.agent.todos,
                                  "usage": live.usage or {"tokens": live.agent.ctx.usage(live.agent.system_prompt()),
                                                          "window": live.agent.ctx.window},
                                  "summary": live.agent.ctx.summary, "compactions": live.agent.ctx.compactions})

    @r.delete("/api/sessions/{sid}")
    async def delete_session(req: web.Request) -> web.Response:
        hub.delete(req.match_info["sid"])
        return web.json_response({"ok": True})

    @r.post("/api/upload")
    async def upload(req: web.Request) -> web.Response:
        """Any file for a chat message. Images go to the vision model; everything else is extracted to text."""
        reader = await req.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            raise web.HTTPBadRequest(text="expected a 'file' field")
        name = re.sub(r"[^\w.\- ()]+", "_", Path(part.filename or "file").name).strip() or "file"
        is_image = Path(name).suffix.lower() in MIME
        limit = MAX_IMAGE_BYTES if is_image else MAX_ATTACHMENT
        hub.inbox.mkdir(parents=True, exist_ok=True)
        dest = hub.inbox / f"{uuid.uuid4().hex[:12]}-{name}"
        size = 0
        with dest.open("wb") as fh:
            while chunk := await part.read_chunk():
                size += len(chunk)
                if size > limit:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=size)
                fh.write(chunk)
        return web.json_response({"id": dest.name, "name": name, "kind": "image" if is_image else "file", "size": size})

    @r.get("/api/memory")
    async def memory(req: web.Request) -> web.Response:
        q, kind = req.query.get("q", "").strip(), req.query.get("kind") or None
        scope = rt.project(req.query["project"]).id if req.query.get("project") else None  # absent = every project's memories
        hits = await rt.memory.asearch(q, k=int(req.query.get("k", 30)), kind=kind, project=scope) if q \
            else rt.memory.recent(60, kind, scope)[::-1]
        return web.json_response({"items": [asdict(h) for h in hits], "fallback": rt.memory.last_error if q else "",
                                  "stats": rt.memory.stats()})

    @r.post("/api/memory")
    async def add_memory(req: web.Request) -> web.Response:
        data = await body(req)
        try:
            item = rt.memory.add(str(data.get("kind", "semantic")), str(data.get("text", "")), list(data.get("tags") or []),
                                  rt.project(str(data["project"])).id if data.get("project") else "")  # "" = shared
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response(asdict(item))

    @r.delete("/api/memory/{mid}")
    async def delete_memory(req: web.Request) -> web.Response:
        if not rt.memory.delete(req.match_info["mid"]):
            raise web.HTTPNotFound(text="no such memory")
        return web.json_response({"ok": True})

    @r.get("/api/ws")
    async def ws_handler(req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(req)
        hub.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data: Any = {}
                try:
                    data = json.loads(msg.data)
                    await _ws_op(hub, ws, data)
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    await ws.send_str(json.dumps({"kind": "error", "message": str(exc), "ref": data.get("ref") if isinstance(data, dict) else None}))
        finally:
            hub.clients.discard(ws)
            for pid in hub.watching.pop(ws, set()):
                await rt.browser.get(pid).watch(False)
        return ws

    work.routes(r, rt, hub)
    extensions.routes(r, rt, hub)
    files.routes(r, rt, hub)
    app.add_routes(r)

    async def spa(req: web.Request) -> web.StreamResponse:
        rel = req.match_info.get("path", "")
        if rel.startswith("api/"):
            raise web.HTTPNotFound(text="no such API route")
        target = (STATIC / rel).resolve()
        if rel and target.is_file() and STATIC.resolve() in target.parents:
            return web.FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                    if "/assets/" in f"/{rel}" else {})
        index = STATIC / "index.html"
        if not index.is_file():
            return web.Response(text="Cadence web UI is not built. Run: cd web && npm install && npm run build", status=503)
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/", spa)
    app.router.add_get("/{path:.*}", spa)
    return app


async def _ws_op(hub: Hub, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
    op, sid = data.get("op"), str(data.get("session", ""))
    ref = data.get("ref")
    if op == "send":
        text = str(data.get("text", "")).strip()
        images, files = [], []
        for name in [*(data.get("images") or []), *(data.get("files") or [])]:
            p = (hub.inbox / str(name)).resolve()
            if hub.inbox.resolve() not in p.parents or not p.is_file():
                raise ValueError(f"unknown upload {name}")
            (images if p.suffix.lower() in MIME else files).append(p)
        if not text and not images and not files:
            raise ValueError("empty message")
        if files:
            text = (text or "Take a look at the attached file(s).") + await hub.attach(sid, files)
        state = await hub.send(sid, text or "Describe the attached image.", images)
        await ws.send_str(json.dumps({"kind": "ack", "ref": ref, "session": sid, "state": state}))
    elif op == "steer":
        live = hub.get(sid)
        text = str(data.get("text", "")).strip()
        if live.running and text:
            live.agent.steer(text)
            await hub.broadcast({"kind": "steer", "session": sid, "text": text})
    elif op == "interrupt":
        live = hub.sessions.get(sid)
        if live and live.running:
            live.agent.interrupt()
    elif op == "browser_watch":
        await _browser_watch(hub, ws, hub.rt.project(str(data.get("project", ""))).id, bool(data.get("on")))
    elif op == "browser":
        await _browser_input(hub, ws, data)
    else:
        raise ValueError(f"unknown op {op!r}")


async def _browser_watch(hub: Hub, ws: web.WebSocketResponse, pid: str, on: bool) -> None:
    mine = hub.watching.setdefault(ws, set())
    if on == (pid in mine):
        return
    (mine.add if on else mine.discard)(pid)
    session = hub.rt.browser.get(pid)
    await session.watch(on)
    if on:
        await ws.send_str(json.dumps(session.state(), default=str))
        frame = await session.frame() if session.started else None
        if frame:
            await ws.send_str(json.dumps(frame))


async def _browser_input(hub: Hub, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
    from ..browser import BrowserError

    pid = hub.rt.project(str(data.get("project", ""))).id
    session = hub.rt.browser.get(pid)
    action = str(data.get("action", ""))
    args = {k: v for k, v in data.items() if k not in ("op", "project", "action", "ref")}
    try:
        if action == "close":
            await session.close()
            return
        was_started = session.started
        await session.act("user", action, **args)
        if not was_started:
            await session.resume_cast()  # viewers may have opened the panel before the browser existed
    except BrowserError as exc:
        await ws.send_str(json.dumps({"kind": "browser_error", "project": pid, "message": str(exc)}))
    except Exception as exc:  # noqa: BLE001 — navigation timeouts etc. are user-visible, not fatal
        await ws.send_str(json.dumps({"kind": "browser_error", "project": pid, "message": f"{exc.__class__.__name__}: {str(exc)[:300]}"}))


# ───────────────────────────────────────────────────────────────── runner
async def serve(rt: Runtime, host: str, port: int, *, daemon: bool = True, channels: bool = True,
                token: str | None = None, on_ready=None) -> None:
    from ..channels import build_channels

    loopback = _is_loopback(host)
    if not loopback and not token:
        token = secrets.token_urlsafe(18)
    allowed = set() if loopback or host in ("0.0.0.0", "::") else {host}
    app = build_app(rt, allowed_hosts=allowed, token=token)
    hub = app[HUB_KEY]

    stop = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    chans = build_channels(rt) if channels else []
    app[CHANNELS_KEY] = chans
    if daemon:
        async def notify(kind: str, message: str) -> None:
            await hub.notify(kind, message)
            if kind in ("task", "goal") and message[:1] in "✔✖★?":
                for ch in chans:
                    await ch.broadcast(message)
        d = rt.make_daemon(notify, hub.task_event)
        tasks.append(asyncio.create_task(d.run_forever(), name="daemon"))
    for ch in chans:
        async def run_channel(c=ch) -> None:
            try:
                await c.run(stop)
            except Exception as exc:  # noqa: BLE001
                await hub.notify("task", f"✖ channel {c.name} stopped: {exc}")
        tasks.append(asyncio.create_task(run_channel(), name=f"channel-{ch.name}"))

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/" + (f"?token={token}" if token else "")
    if on_ready:
        on_ready(url, token)
    try:
        await asyncio.Event().wait()
    finally:
        stop.set()
        if rt.daemon:
            rt.daemon.stop()
        for live in hub.sessions.values():
            if live.running:
                live.agent.interrupt()
        await asyncio.gather(*tasks, return_exceptions=True)
        for ch in chans:
            ch.bridge.save_all()
        for ws in list(hub.clients):
            await ws.close()
        await runner.cleanup()
