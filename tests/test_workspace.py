"""Projects, long-horizon goals, ideas, settings, extensions, files and canvas (0.3 features)."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import tool_call

from cadence.goals import Goal
from cadence.llm import Completion
from cadence.memory import MemoryStore
from cadence.projects import ProjectError, check_path, suggest_name
from cadence.runtime import Runtime
from cadence.tasks import Task
from cadence.web.server import build_app

ORIGIN = {"Origin": "http://127.0.0.1"}


async def _client(cfg, router):
    rt = await Runtime(cfg, router=router, memory=MemoryStore(cfg.state_dir / "m.jsonl")).start()
    client = TestClient(TestServer(build_app(rt)))
    await client.start_server()
    return rt, client


async def _finish(ws, sid):
    async with asyncio.timeout(10):
        async for msg in ws:
            m = msg.json()
            if m.get("session") == sid and m["kind"] == "turn" and m["state"] == "finished":
                return


# ─────────────────────────────────────────────────────────────── projects
def test_project_paths_are_validated(tmp_path):
    from pathlib import Path
    with pytest.raises(ProjectError):
        check_path("/")
    with pytest.raises(ProjectError):
        check_path(str(Path.home()))
    with pytest.raises(ProjectError):
        check_path("relative/dir")
    with pytest.raises(ProjectError):
        check_path(str(tmp_path / "missing"))
    assert check_path(str(tmp_path / "new"), create=True).is_dir()


async def test_small_model_names_projects_and_falls_back(tmp_path, make_router):
    d = tmp_path / "acme-billing_api"
    d.mkdir()
    (d / "README.md").write_text("# Billing\nInvoices for Acme")
    router = make_router(small_script=[Completion(content='"Acme Billing"\n'),
                                       Completion(content="Here is a long rambling answer that is not a name at all")])
    assert await suggest_name(router.small, d) == "Acme Billing"
    assert await suggest_name(router.small, d) == "Acme Billing Api"  # fallback from the folder name
    assert "Invoices for Acme" in json.dumps(router.small.calls[0])


async def test_chats_are_scoped_to_their_project_directory(cfg, make_router, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    router = make_router(big_script=[tool_call("write_file", path="note.txt", content="hi"), Completion(content="done")],
                         small_script=[Completion(content="Other Thing")])
    rt, client = await _client(cfg, router)
    try:
        proj = await (await client.post("/api/projects", json={"path": str(other)}, headers=ORIGIN)).json()
        assert proj["name"] == "Other Thing"  # named by the small model
        assert (await client.post("/api/projects", json={"path": str(other)}, headers=ORIGIN)).status == 409
        sid = (await (await client.post("/api/sessions", json={"project": proj["id"]}, headers=ORIGIN)).json())["id"]
        ws = await client.ws_connect("/api/ws", headers=ORIGIN)
        await ws.send_json({"op": "send", "session": sid, "text": "write a note"})
        await _finish(ws, sid)
        assert (other / "note.txt").read_text() == "hi" and not (cfg.workspace / "note.txt").exists()
        assert "## Project: Other Thing" in router.big.calls[0][0]["content"]
        listed = await (await client.get(f"/api/sessions?project={proj['id']}")).json()
        assert [s["id"] for s in listed] == [sid]
        assert await (await client.get(f"/api/sessions?project={rt.default_project.id}")).json() == []
        rt2 = Runtime(cfg, router=router)  # sessions remember their project across restarts
        assert rt2.load_agent(sid).cfg.workspace == other.resolve()
        renamed = await (await client.patch(f"/api/projects/{proj['id']}", json={"name": "Renamed"}, headers=ORIGIN)).json()
        assert renamed["name"] == "Renamed"
        assert (await client.delete(f"/api/projects/{rt.default_project.id}", headers=ORIGIN)).status == 409
        assert (await client.delete(f"/api/projects/{proj['id']}", headers=ORIGIN)).status == 200
        assert (other / "note.txt").exists()  # forgetting a project never deletes its files
        await ws.close()
    finally:
        await client.close()
        await rt.close()


# ────────────────────────────────────────────────────────────────── goals
def plan(progress, status="working", tasks=(), **extra):
    return Completion(content=json.dumps({"assessment": f"at {progress}%", "progress": progress, "status": status,
                                          "tasks": [{"title": t, "verify_command": "true"} for t in tasks], **extra}))


async def test_planner_drives_a_goal_through_tasks_to_achieved(cfg, make_router):
    from test_tasks import reflection

    router = make_router(big_script=[
        plan(10, tasks=["Write the parser"]),           # first planning pass
        Completion(content="parser written"), reflection("done"),
        plan(60, tasks=["Write the parser", "Add parser tests"]),  # duplicate is dropped
        Completion(content="tests added"), reflection("done"),
        plan(100, status="achieved"),
    ])
    rt = await Runtime(cfg, router=router, memory=MemoryStore(cfg.state_dir / "m.jsonl")).start()
    notes: list[str] = []

    async def notify(kind, msg):
        notes.append(msg)

    daemon = rt.make_daemon(notify)
    goal = rt.goals.add(Goal(title="Ship a config parser", success=["parser handles YAML", "tests pass"]))
    run = asyncio.create_task(daemon.run_forever())
    try:
        for _ in range(200):
            await asyncio.sleep(0.05)
            if rt.goals.get(goal.id).status == "achieved":
                break
    finally:
        daemon.stop()
        await asyncio.wait_for(run, 5)
        await rt.close()
    g = rt.goals.get(goal.id)
    assert g.status == "achieved" and g.progress == 100
    titles = [t.title for t in rt.tasks.list(goal=g.id)]
    assert titles == ["Write the parser", "Add parser tests"]
    assert all(t.status == "done" and t.source == "planner" for t in rt.tasks.list(goal=g.id))
    assert "Ship a config parser" in rt.tasks.list(goal=g.id)[0].context  # tasks know the goal they serve
    kinds = [e["kind"] for e in g.journal]
    assert kinds.count("plan") == 2 and kinds.count("task") == 2 and kinds[-1] == "achieved"
    assert any(n.startswith("★") for n in notes)


async def test_planner_question_pauses_goal_until_user_answers(cfg, make_router):
    router = make_router(big_script=[plan(5, status="needs_user", question="Which database?"), plan(20, tasks=["Set up Postgres"])])
    rt, client = await _client(cfg, router)
    try:
        g = await (await client.post("/api/goals", json={"title": "Persist data", "success": ["data survives restart"]},
                                     headers=ORIGIN)).json()
        await client.post(f"/api/goals/{g['id']}/plan", headers=ORIGIN)  # no daemon: plans inline
        g = await (await client.get("/api/goals")).json()
        assert g[0]["status"] == "paused" and g[0]["question"] == "Which database?"
        g = await (await client.post(f"/api/goals/{g[0]['id']}/note", json={"text": "Postgres"}, headers=ORIGIN)).json()
        assert g["status"] == "active" and g["question"] == ""
        await client.post(f"/api/goals/{g['id']}/plan", headers=ORIGIN)
        g = (await (await client.get("/api/goals")).json())[0]
        assert [t["title"] for t in g["tasks"]] == ["Set up Postgres"]
        assert "Postgres" in json.dumps(router.big.calls[-1])  # the planner saw the user's answer
        edited = await (await client.patch(f"/api/goals/{g['id']}", json={"success": ["a", "b"], "status": "paused"},
                                           headers=ORIGIN)).json()
        assert edited["success"] == ["a", "b"] and edited["status"] == "paused"
        assert (await client.delete(f"/api/goals/{g['id']}", headers=ORIGIN)).status == 200
        assert rt.goals.all() == [] and rt.tasks.all() == []  # its queued task went with it
    finally:
        await client.close()
        await rt.close()


async def test_ideas_can_be_edited_dismissed_and_promoted(cfg, make_router):
    rt, client = await _client(cfg, make_router())
    try:
        i = await (await client.post("/api/ideas", json={"title": "Add caching", "size": "task"}, headers=ORIGIN)).json()
        i = await (await client.patch(f"/api/ideas/{i['id']}", json={"title": "Add HTTP caching", "size": "goal"},
                                      headers=ORIGIN)).json()
        assert i["title"] == "Add HTTP caching" and i["size"] == "goal"
        res = await (await client.post(f"/api/ideas/{i['id']}/promote", json={}, headers=ORIGIN)).json()
        assert res["goal"]["title"] == "Add HTTP caching" and res["idea"]["promoted_to"] == f"goal:{res['goal']['id']}"
        assert (await client.post(f"/api/ideas/{i['id']}/promote", json={}, headers=ORIGIN)).status == 409
        j = await (await client.post("/api/ideas", json={"title": "Fix typo"}, headers=ORIGIN)).json()
        res = await (await client.post(f"/api/ideas/{j['id']}/promote", json={"to": "task"}, headers=ORIGIN)).json()
        assert res["task"]["source"] == "idea" and res["task"]["status"] == "pending"
        k = await (await client.post("/api/ideas", json={"title": "Rewrite in Rust"}, headers=ORIGIN)).json()
        assert (await (await client.patch(f"/api/ideas/{k['id']}", json={"status": "dismissed"}, headers=ORIGIN)).json())["status"] == "dismissed"
        assert (await client.delete(f"/api/ideas/{k['id']}", headers=ORIGIN)).status == 200
        assert len(await (await client.get("/api/ideas")).json()) == 2
    finally:
        await client.close()
        await rt.close()


# ─────────────────────────────────────────────────────────────── settings
async def test_settings_persist_and_apply_live(cfg, make_router):
    rt, client = await _client(cfg, make_router())
    try:
        res = await client.patch("/api/settings", json={
            "big": {"name": "qwen/qwen3.5-9b", "base_url": "http://127.0.0.1:1234"},
            "daemon": {"reflection_cycles": 3, "auto_approve_ideas": True},
            "agent": {"max_iterations": 12},
            "providers": [{"name": "Box", "kind": "ollama", "url": "http://10.0.0.5:11434/"}],
            "ui": {"theme": "light", "project": "abc"}}, headers=ORIGIN)
        view = await res.json()
        assert view["big"]["name"] == "qwen/qwen3.5-9b" and view["big"]["base_url"] == "http://127.0.0.1:1234/v1"
        assert rt.router.big.llm.name == "qwen/qwen3.5-9b"  # swapped without a restart
        assert rt.cfg.max_iterations == 12 and rt.cfg.reflection_cycles == 3 and rt.cfg.auto_approve_ideas is True
        assert view["ui"] == {"theme": "light", "project": "abc"}
        from cadence.config import Config, _apply, read_settings
        fresh = Config(workspace=cfg.workspace, home=cfg.home)
        _apply(fresh, read_settings(fresh), replace_lists=True)
        assert fresh.big.name == "qwen/qwen3.5-9b" and fresh.max_iterations == 12
        assert [(p.kind, p.url) for p in fresh.providers] == [("ollama", "http://10.0.0.5:11434")]
        bad = await client.patch("/api/settings", json={"sandbox": {"mode": "chroot"}}, headers=ORIGIN)
        assert bad.status == 400
    finally:
        await client.close()
        await rt.close()


# ───────────────────────────────────────────────────────────── extensions
async def test_skills_plugins_and_hooks_are_managed_live(cfg, make_router):
    cfg.plugin_dirs = [cfg.home / "plugins"]
    rt, client = await _client(cfg, make_router())
    try:
        assert (await client.post("/api/skills", json={"name": "deploy", "description": "How to deploy"}, headers=ORIGIN)).status == 200
        names = [s["name"] for s in (await (await client.get("/api/skills")).json())["skills"]]
        assert names == ["deploy"] and "deploy" in rt.scope().skills.skills
        src = (await (await client.get("/api/skills/deploy")).json())["content"]
        bad = await client.put("/api/skills/deploy", json={"content": "no frontmatter"}, headers=ORIGIN)
        assert bad.status == 400 and (cfg.home / "skills/deploy/SKILL.md").read_text() == src  # rejected edits are rolled back
        await client.put("/api/skills/deploy", json={"content": src.replace("How to deploy", "Ship it")}, headers=ORIGIN)
        assert rt.scope().skills.skills["deploy"].description == "Ship it"
        await client.post("/api/skills/deploy/toggle", json={"enabled": False}, headers=ORIGIN)
        assert "deploy" not in rt.scope().skills.skills and "deploy" in rt.scope().skills.found
        await client.delete("/api/skills/deploy", headers=ORIGIN)
        assert not (cfg.home / "skills/deploy").exists()

        res = await (await client.post("/api/plugins", json={"name": "greeter"}, headers=ORIGIN)).json()
        assert res["ok"] and rt.scope().registry.get("greeter_hello")  # visible to project scopes immediately
        agent = rt.new_agent()  # …and to agents created before an edit
        source = (await (await client.get("/api/plugins/greeter/source")).json())["source"]
        assert (await client.put("/api/plugins/greeter/source", json={"source": "def register(api) syntax error"},
                                 headers=ORIGIN)).status == 400
        new_src = source.replace('"greeter_hello"', '"greeter_wave"')
        assert (await (await client.put("/api/plugins/greeter/source", json={"source": new_src}, headers=ORIGIN)).json())["ok"]
        assert agent.registry.get("greeter_wave") and not agent.registry.get("greeter_hello")
        await client.post("/api/plugins/greeter/disable", headers=ORIGIN)
        assert not agent.registry.get("greeter_wave") and rt.cfg.disabled_plugins == ["greeter"]
        await client.delete("/api/plugins/greeter", headers=ORIGIN)
        assert not (cfg.home / "plugins/greeter.py").exists()

        hook = {"event": "pre_tool", "matcher": "bash", "command": "echo nope >&2; exit 2"}
        tested = await (await client.post("/api/hooks/test", json=hook, headers=ORIGIN)).json()
        assert tested["blocked"] and tested["reason"] == "nope"
        await client.put("/api/hooks", json={"hooks": [hook]}, headers=ORIGIN)
        assert [h.command for h in rt.scope().hooks.shell] == [hook["command"]]  # scoped hooks share the list
        assert (await client.put("/api/hooks", json={"hooks": [{"event": "nope", "command": "x"}]}, headers=ORIGIN)).status == 400
    finally:
        await client.close()
        await rt.close()


async def test_mcp_servers_are_added_and_removed_from_the_ui(cfg, make_router, tmp_path):
    pytest.importorskip("mcp")
    from test_infra import MCP_SERVER
    script = tmp_path / "srv.py"
    script.write_text(MCP_SERVER)
    rt, client = await _client(cfg, make_router())
    try:
        rows = await (await client.put("/api/mcp/demo", json={"command": sys.executable, "args": [str(script)]}, headers=ORIGIN)).json()
        assert rows[0]["connected"], rows
        agent = rt.new_agent()
        assert (await agent.registry.call("mcp__demo__add", {"a": 2, "b": 3}, 20)).strip() == "5"
        rows = await (await client.post("/api/mcp/demo/restart", headers=ORIGIN)).json()
        assert rows[0]["connected"]
        await client.delete("/api/mcp/demo", headers=ORIGIN)
        assert agent.registry.get("mcp__demo__add") is None and rt.cfg.mcp_servers == []
        imported = await (await client.post("/api/mcp-import", json={"mcpServers": {"d2": {"command": sys.executable,
                                                                                           "args": [str(script)]}}},
                                            headers=ORIGIN)).json()
        assert imported["added"] == ["d2"] and imported["servers"][0]["connected"]
    finally:
        await client.close()
        await rt.close()


# ─────────────────────────────────────────────────────────── files/canvas
async def test_file_explorer_is_confined_and_detects_conflicts(cfg, make_router):
    rt, client = await _client(cfg, make_router())
    try:
        await client.put("/api/files/write", json={"path": "src/a.py", "content": "print(1)\n"}, headers=ORIGIN)
        listing = await (await client.get("/api/files")).json()
        assert [(e["name"], e["dir"]) for e in listing["entries"]] == [("src", True)]
        f = await (await client.get("/api/files/read?path=src/a.py")).json()
        assert f["content"] == "print(1)\n" and not f["binary"]
        (cfg.workspace / "src/a.py").write_text("print(2)\n")  # the agent edits it meanwhile
        import os
        os.utime(cfg.workspace / "src/a.py", (f["mtime"] + 5, f["mtime"] + 5))
        stale = await client.put("/api/files/write", json={"path": "src/a.py", "content": "x", "mtime": f["mtime"]}, headers=ORIGIN)
        assert stale.status == 409
        assert (await client.get("/api/files/read?path=../../etc/passwd")).status == 403
        raw = await client.get("/api/files/raw?path=src/a.py")
        assert raw.headers["Content-Security-Policy"].startswith("sandbox")
        await client.post("/api/files/rename", json={"from": "src/a.py", "to": "src/b.py"}, headers=ORIGIN)
        assert (cfg.workspace / "src/b.py").exists()
        assert (await client.delete("/api/files?path=", headers=ORIGIN)).status == 403
        assert (await client.delete("/api/files?path=src", headers=ORIGIN)).status == 200 and not (cfg.workspace / "src").exists()
    finally:
        await client.close()
        await rt.close()


async def test_canvas_round_trip_between_agent_and_user(cfg, make_router):
    router = make_router(big_script=[
        tool_call("canvas_write", title="Plan", content="# Plan\n- step one"),
        Completion(content="wrote the plan"),
        lambda msgs: tool_call("canvas_read", id=_doc_id[0]),
        Completion(content="read it"),
    ])
    _doc_id: list[str] = []
    rt, client = await _client(cfg, router)
    try:
        sid = (await (await client.post("/api/sessions", headers=ORIGIN)).json())["id"]
        ws = await client.ws_connect("/api/ws", headers=ORIGIN)
        await ws.send_json({"op": "send", "session": sid, "text": "draft a plan"})
        events = []
        async with asyncio.timeout(10):
            async for msg in ws:
                m = msg.json()
                events.append(m)
                if m["kind"] == "turn" and m["state"] == "finished":
                    break
        canvas = [m for m in events if m["kind"] == "canvas"]
        assert canvas and canvas[0]["focus"] and canvas[0]["doc"]["title"] == "Plan"
        cid = canvas[0]["doc"]["id"]
        _doc_id.append(cid)
        doc = await (await client.put(f"/api/canvas/{cid}", json={"content": "# Plan\n- step one\n- user added this"},
                                      headers=ORIGIN)).json()
        assert doc["author"] == "user" and len(doc["versions"]) == 1
        await ws.send_json({"op": "send", "session": sid, "text": "check my edits"})
        await _finish(ws, sid)
        tool_results = [m for m in router.big.calls[-1] if m.get("role") == "tool"]
        assert "user added this" in tool_results[-1]["content"] and "last saved by the user" in tool_results[-1]["content"]
        restored = await (await client.post(f"/api/canvas/{cid}/restore", json={"index": 0}, headers=ORIGIN)).json()
        assert "user added" not in restored["content"]
        await ws.close()
    finally:
        await client.close()
        await rt.close()


# ─────────────────────────────────────────────────────────────── browser
@pytest.mark.skipif(not __import__("cadence.browser", fromlist=["find_chrome"]).find_chrome(), reason="no Chrome/Chromium")
async def test_agent_browser_snapshot_click_and_type(cfg):
    pytest.importorskip("playwright")
    from cadence.browser import BrowserManager, browser_tools

    events: list[dict] = []

    async def publish(m):
        events.append(m)

    mgr = BrowserManager(cfg, publish)
    tools = {t.name: t for t in browser_tools(mgr, "p1")}
    page = ("data:text/html,<title>T</title><input placeholder='Your name' id=n>"
            "<button onclick=\"document.title='hi '+document.getElementById('n').value\">Greet</button>")
    try:
        snap = await tools["browser_open"].handler({"url": page})
        assert '[1] input' in snap and '"Your name"' in snap and '[2] button "Greet"' in snap
        await tools["browser_type"].handler({"element": 1, "text": "Ada"})
        out = await tools["browser_click"].handler({"element": 2})
        assert "Page: hi Ada" in out
        await mgr.get("p1").act("user", "goto", url="data:text/html,<title>User drove</title>")
        assert "Page: User drove" in await tools["browser_snapshot"].handler({})
        assert any(e["kind"] == "browser_open" for e in events) and any(e["kind"] == "browser_state" for e in events)
        await mgr.get("p1").watch(True)
        frame = await mgr.get("p1").frame()
        assert frame and len(frame["data"]) > 100
    finally:
        await mgr.close()


async def test_canvas_document_streams_while_the_model_writes_it(cfg, make_router):
    from conftest import ScriptedLLM
    from cadence.llm import ModelRouter, StreamDelta, ToolCall

    full = json.dumps({"title": "Spec", "format": "markdown", "content": "# Spec\n\nline one\nline two"})

    class Streaming(ScriptedLLM):
        async def complete(self, messages, tools=None, *, on_delta=None, **kw):
            if on_delta and not self.script:
                self.script.append(Completion(content="done"))
            if on_delta and not self.calls:
                for i in range(0, len(full), 9):
                    await on_delta(StreamDelta("tool_args", full[i:i + 9], "c1", "canvas_write"))
                self.calls.append([])
                return Completion(tool_calls=[ToolCall(id="c1", name="canvas_write", arguments=full)])
            return await super().complete(messages, tools, on_delta=on_delta, **kw)

    router = ModelRouter(Streaming("big"), ScriptedLLM("small"))
    rt, client = await _client(cfg, router)
    try:
        sid = (await (await client.post("/api/sessions", headers=ORIGIN)).json())["id"]
        ws = await client.ws_connect("/api/ws", headers=ORIGIN)
        await ws.send_json({"op": "send", "session": sid, "text": "write a spec"})
        events = []
        async with asyncio.timeout(10):
            async for msg in ws:
                m = msg.json()
                events.append(m)
                if m["kind"] == "turn" and m["state"] == "finished":
                    break
        canvas = [m for m in events if m["kind"] == "canvas"]
        streamed = [m for m in canvas if m.get("streaming")]
        assert streamed and streamed[0]["focus"], "the canvas should open as soon as the model starts writing"
        assert streamed[0]["doc"]["title"] == "Spec" and full.startswith('{"title": "Spec"')
        docs = rt.canvas.list()
        assert len(docs) == 1 and docs[0].content.endswith("line two") and not docs[0].streaming  # claimed, not duplicated
        await ws.close()
    finally:
        await client.close()
        await rt.close()


def test_partial_args_reads_a_cut_off_tool_call():
    from cadence.canvas import partial_args
    raw = '{"title": "A \\"b\\"", "content": "x\\ny \\u00e9 and'
    assert partial_args(raw) == {"title": 'A "b"', "content": "x\ny é and"}


async def test_reflection_files_memories_and_goal_ideas(cfg, make_router):
    from cadence.reflect import reflect
    from cadence.ideas import IdeaStore
    from cadence.memory import MemoryStore
    from cadence.tasks import TaskStore
    reply = json.dumps({"memories": [{"kind": "procedural", "text": "Run pytest with PYTHONPATH=src"}],
                        "ideas": [{"title": "Add a CI workflow", "size": "goal"}]})
    router = make_router(small_script=[Completion(content=reply)])
    mem, ideas, tasks = MemoryStore(cfg.state_dir / "m.jsonl"), IdeaStore(cfg.state_dir / "i.json"), TaskStore(cfg.state_dir / "t.json")
    done = await reflect(router, mem, ideas, tasks, project="p", log_text="USER: fix the tests\n" * 10)
    assert done == {"memories": 1, "ideas": 1}
    assert mem.recent(5)[0].text.startswith("Run pytest") and ideas.list(project="p")[0].size == "goal"


async def test_reflection_files_all_three_memory_kinds(cfg, make_router):
    from cadence.reflect import reflect
    from cadence.ideas import IdeaStore
    from cadence.memory import MemoryStore
    from cadence.tasks import TaskStore
    reply = json.dumps({"summary": "Fixed the failing parser tests.", "facts": ["The user prefers pytest over unittest"],
                        "lessons": ["Run pytest with PYTHONPATH=src"], "ideas": []})
    router = make_router(small_script=[Completion(content=reply)])
    mem = MemoryStore(cfg.state_dir / "m.jsonl")
    done = await reflect(router, mem, IdeaStore(cfg.state_dir / "i.json"), TaskStore(cfg.state_dir / "t.json"),
                         project="p", log_text="USER: fix the tests\n" * 10)
    assert done["memories"] == 3
    assert {m.kind for m in mem.recent(5)} == {"episodic", "semantic", "procedural"}


def test_near_duplicate_memories_are_folded(cfg):
    from cadence.memory import MemoryStore
    mem = MemoryStore(cfg.state_dir / "m.jsonl")
    a = mem.add("procedural", "Always run the full test suite before reporting a task as done")
    b = mem.add("procedural", "Always run the full test suite before reporting the task as done.")
    assert a.id == b.id and len(mem) == 1
    mem.add("semantic", "Always run the full test suite before reporting a task as done")  # other kind: kept
    assert len(mem) == 2
    mem._items.append(type(a)(kind="procedural", text="Always run the whole test suite before reporting a task as done"))
    assert mem.dedupe() == 1 and len(mem) == 2


async def test_planner_sets_priority_due_and_always_leaves_a_next_task(cfg, make_router):
    router = make_router(big_script=[
        Completion(content=json.dumps({"assessment": "starting", "progress": 5, "status": "working",
                                       "tasks": [{"title": "Draft the schema", "priority": 5, "estimate_min": 20}]})),
        plan(10, tasks=()),  # working but proposes nothing, and nothing is queued: a fallback task is made
    ])
    rt = await Runtime(cfg, router=router, memory=MemoryStore(cfg.state_dir / "m.jsonl")).start()
    goal = rt.goals.add(Goal(title="Ship billing", priority=4))
    added = await rt.planner.plan(goal, cfg.workspace)
    assert added[0].priority == 5 and added[0].estimate_min == 20 and added[0].due > time.time()
    assert rt.goals.get(goal.id).next_review > time.time()  # the goal will be evaluated again on schedule
    rt.tasks.update(added[0], status="done")
    again = await rt.planner.plan(goal, cfg.workspace)
    assert len(again) == 1 and again[0].priority == 4 and again[0].estimate_min == 30
    await rt.close()


async def test_daemon_runs_user_and_high_priority_work_first(cfg, make_router):
    rt = await Runtime(cfg, router=make_router(), memory=MemoryStore(cfg.state_dir / "m.jsonl")).start()
    d = rt.make_daemon()
    low = rt.tasks.add(Task(title="low", priority=1, source="planner"))
    high = rt.tasks.add(Task(title="high", priority=5, source="planner"))
    user = rt.tasks.add(Task(title="user", priority=3, source="user"))
    order = sorted([low, high, user], key=d._weight, reverse=True)
    assert [t.title for t in order] == ["user", "high", "low"]
    await rt.close()


def test_system_prompt_sections_are_trimmed_lowest_weight_first():
    from cadence.agent import fit_by_weight
    out = fit_by_weight([(10, "CORE" * 50), (4, "SKILLS" * 400), (6, "MEMORY" * 100)], budget=1000)
    assert out.count("CORE") == 50 and "MEMORY" in out and len(out) <= 1100
    assert out.count("SKILLS") < 400  # the lowest-weight section took the cut


def test_reasoning_headroom_is_added_to_short_structured_calls(cfg):
    from cadence.llm import OpenAICompatLLM
    llm = OpenAICompatLLM(cfg.big)
    assert cfg.big.reasoning_headroom > 0 and llm.cfg is cfg.big
