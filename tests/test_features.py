from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import shutil

import httpx
import pytest
from conftest import ScriptedLLM, tool_call

from cadence.agent import Agent
from cadence.channels import Bridge, chunk
from cadence.channels.telegram import TelegramChannel
from cadence.channels.whatsapp import WhatsAppChannel
from cadence.config import HookConfig, SandboxConfig
from cadence.context import deterministic_summary, render_transcript
from cadence.hooks import Hooks
from cadence.llm import Completion, LLMError, content_text, message_tokens
from cadence.memory import LocalVectorIndex, MemoryStore, QdrantIndex, SemanticIndex
from cadence.plugins import PluginManager
from cadence.runtime import Runtime
from cadence.sandbox import Sandbox
from cadence.skills import SkillRegistry
from cadence.tools import ToolRegistry, Workspace, builtin_tools

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf00000301010018dd8db00000000049454e44ae426082"
)

# ─────────────────────────────────────────────────────────────── vector memory
CONCEPTS = {"database": ["postgres", "postgresql", "database", "db", "sql"], "web": ["fastapi", "http", "endpoint", "framework"],
            "tests": ["flaky", "test", "tests", "unreliable"]}


class ConceptEmbedder:
    """Maps text onto hand-picked concept axes, so synonyms land close without a real model."""

    name = "concept-v1"

    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, 0

    async def embed(self, texts):
        if self.fail:
            raise LLMError("embedder down")
        self.calls += 1
        out = []
        for t in texts:
            words = set(re.findall(r"[a-z]+", t.lower()))
            out.append([float(len(words & set(v))) for v in CONCEPTS.values()])
        return out

    async def aclose(self):
        pass


def _store(tmp_path, embedder=None, model="concept-v1"):
    index = LocalVectorIndex(tmp_path / "vec.jsonl", model)
    return MemoryStore(tmp_path / "m.jsonl", SemanticIndex(embedder or ConceptEmbedder(), index), min_similarity=0.3)


async def test_semantic_search_finds_meaning_not_just_words(tmp_path):
    m = _store(tmp_path)
    m.add("semantic", "Production runs PostgreSQL 16")
    m.add("semantic", "The API is built on FastAPI")
    m.add("episodic", "Fixed a flaky login test")
    # "db" shares no keyword with "PostgreSQL" — only the vector side can find it
    assert (await m.asearch("which db do we run", k=1))[0].text == "Production runs PostgreSQL 16"
    assert (await m.asearch("unreliable tests", k=1, kind="episodic"))[0].kind == "episodic"
    assert m.search("which db do we run") == []  # keyword search alone misses it


async def test_vectors_persist_and_are_only_embedded_once(tmp_path):
    emb = ConceptEmbedder()
    m = _store(tmp_path, emb)
    m.add("semantic", "Production runs PostgreSQL 16")
    await m.asearch("database")
    first = emb.calls
    emb2 = ConceptEmbedder()
    m2 = _store(tmp_path, emb2)  # reload from disk
    await m2.asearch("database")
    assert emb2.calls == 1 and first == 2  # reload: only the query was embedded, not the stored item
    m3 = _store(tmp_path, ConceptEmbedder(), model="other-model")  # model change → re-embed
    assert await m3.sync_index() == 1


async def test_embedder_outage_falls_back_to_keywords(tmp_path):
    m = _store(tmp_path, ConceptEmbedder(fail=True))
    m.add("procedural", "Run ruff before committing")
    hits = await m.asearch("ruff committing")
    assert hits and hits[0].text.startswith("Run ruff") and "embedder down" in m.last_error


async def test_unrelated_query_returns_nothing(tmp_path):
    m = _store(tmp_path)
    m.add("semantic", "Production runs PostgreSQL 16")
    assert await m.asearch("banana bread recipe") == []


class FakeQdrant:
    def __init__(self):
        self.points: dict[str, dict] = {}
        self.created = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, body = req.url.path, json.loads(req.content or b"{}")
        if path.endswith("/points/search"):
            want = {m["key"]: m["match"]["value"] for m in body["filter"]["must"]}
            hits = []
            for pid, p in self.points.items():
                if all(p["payload"].get(k) == v for k, v in want.items()):
                    a, b = p["vector"], body["vector"]
                    na, nb = sum(x * x for x in a) ** 0.5, sum(x * x for x in b) ** 0.5
                    hits.append({"id": pid, "score": sum(x * y for x, y in zip(a, b)) / (na * nb), "payload": p["payload"]})
            hits.sort(key=lambda h: -h["score"])
            return httpx.Response(200, json={"result": hits[: body["limit"]]})
        if path.endswith("/points/scroll"):
            return httpx.Response(200, json={"result": {"points": [{"payload": p["payload"]} for p in self.points.values()],
                                                        "next_page_offset": None}})
        if path.endswith("/points"):
            for p in body["points"]:
                self.points[p["id"]] = p
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if req.method == "PUT":
            self.created = True
            return httpx.Response(200, json={"result": True})
        return httpx.Response(200 if self.created else 404, json={"result": {}})


async def test_qdrant_backend_round_trip(tmp_path):
    fake = FakeQdrant()
    index = QdrantIndex("http://qdrant", "mem", "concept-v1", transport=httpx.MockTransport(fake))
    m = MemoryStore(tmp_path / "m.jsonl", SemanticIndex(ConceptEmbedder(), index), min_similarity=0.3)
    m.add("semantic", "Production runs PostgreSQL 16")
    m.add("semantic", "The API is built on FastAPI")
    assert (await m.asearch("db", k=1))[0].text.startswith("Production")
    assert fake.created and len(fake.points) == 2
    m2 = MemoryStore(tmp_path / "m.jsonl", SemanticIndex(ConceptEmbedder(), index))
    assert await m2.sync_index() == 0  # already in qdrant


# ─────────────────────────────────────────────────────────────────── images
def test_image_content_never_leaks_base64_into_text_paths():
    msg = {"role": "user", "content": [{"type": "text", "text": "what is this"},
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 50000}}]}
    assert content_text(msg["content"]) == "what is this\n[image]"
    assert "AAAA" not in render_transcript([msg]) and "AAAA" not in deterministic_summary("", [msg])
    assert 1000 < message_tokens(msg) < 1300


def _agent(cfg, router, vision=None, hooks=None):
    reg = ToolRegistry()
    for t in builtin_tools(Workspace(cfg.workspace), allow_shell=True, output_limit=4000, vision=vision):
        reg.register(t)
    skills = SkillRegistry(cfg.skill_dirs)
    return Agent(cfg, router, reg, skills, MemoryStore(cfg.state_dir / "m.jsonl"), hooks=hooks)


async def test_attached_image_reaches_big_model_as_image_part(cfg, make_router):
    (cfg.workspace / "shot.png").write_bytes(PNG)
    router = make_router(big_script=[Completion(content="a 1px image")])
    agent = _agent(cfg, router)
    final = await agent.run_to_completion("look", [cfg.workspace / "shot.png"])
    user = router.big.calls[0][-1]
    assert final == "a 1px image" and user["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_view_image_tool_uses_vision_model(cfg, make_router):
    (cfg.workspace / "ui.png").write_bytes(PNG)
    vision = ScriptedLLM("vlm", [Completion(content="A login form with a red error banner")])
    router = make_router(big_script=[tool_call("view_image", path="ui.png", question="any errors?"), Completion(content="done")])
    agent = _agent(cfg, router, vision=vision)
    events = [e async for e in agent.run("check the screenshot")]
    result = next(e for e in events if e.type == "tool_result")
    assert "red error banner" in result.data["result"]
    assert vision.calls[0][1]["content"][0]["text"] == "any errors?"


async def test_view_image_rejects_non_images(cfg, make_router):
    (cfg.workspace / "notes.txt").write_text("hi")
    router = make_router(big_script=[tool_call("view_image", path="notes.txt"), Completion(content="ok")])
    events = [e async for e in _agent(cfg, router, vision=ScriptedLLM("vlm")).run("x")]
    assert "is a text file: use read_file" in next(e for e in events if e.type == "tool_result").data["result"]


# ───────────────────────────────────────────────────────── hooks and plugins
async def test_shell_pre_tool_hook_blocks_and_post_tool_adds_context(cfg, make_router):
    block = HookConfig(event="pre_tool", matcher="bash",
                       command="python3 -c \"import sys,json; d=json.load(sys.stdin); "
                               "sys.exit(0) if 'rm ' not in d['arguments']['command'] else (print('no rm allowed', file=sys.stderr), sys.exit(2))\"")
    note = HookConfig(event="post_tool", matcher="bash", command="echo audited")
    hooks = Hooks([block, note], cfg.workspace)
    router = make_router(big_script=[tool_call("bash", command="rm -rf build"), tool_call("bash", command="echo hi"),
                                     Completion(content="ok")])
    events = [e async for e in _agent(cfg, router, hooks=hooks).run("clean")]
    results = [e.data["result"] for e in events if e.type == "tool_result"]
    assert results[0].startswith("ERROR: blocked by hook: no rm allowed")
    assert "hi" in results[1] and results[1].endswith("[hook] audited")


async def test_user_prompt_hook_can_add_context_or_block(cfg, make_router):
    hooks = Hooks([HookConfig(event="user_prompt", command="echo 'branch: main'")], cfg.workspace)
    router = make_router(big_script=[Completion(content="ok")])
    await _agent(cfg, router, hooks=hooks).run_to_completion("status?")
    assert "branch: main" in router.big.calls[0][-1]["content"]

    deny = Hooks([HookConfig(event="user_prompt", command="echo nope >&2; exit 2")], cfg.workspace)
    events = [e async for e in _agent(cfg, make_router(), hooks=deny).run("x")]
    assert events[-1].type == "error" and "nope" in events[-1].data["message"]


PLUGIN = '''
from cadence.hooks import HookResult

def register(api):
    @api.tool("shout", "Upper-case text.", {"type": "object", "properties": {"text": {"type": "string"}}}, read_only=True)
    def shout(args):
        return args["text"].upper()

    @api.on("pre_tool", matcher="write_file")
    def guard(payload):
        if payload["arguments"]["path"].endswith(".env"):
            return HookResult(block=True, reason="secrets files are off-limits")
'''


async def test_plugin_adds_tool_and_hook_and_bad_plugin_is_reported(cfg, make_router):
    pdir = cfg.workspace / ".cadence" / "plugins"
    pdir.mkdir(parents=True)
    (pdir / "demo.py").write_text(PLUGIN)
    (pdir / "broken.py").write_text("raise RuntimeError('boom')")
    reg, hooks = ToolRegistry(), Hooks(workspace=cfg.workspace)
    for t in builtin_tools(Workspace(cfg.workspace), allow_shell=True, output_limit=4000):
        reg.register(t)
    pm = PluginManager([pdir])
    pm.load(reg, hooks, cfg)
    assert "demo" in pm.loaded and pm.loaded["demo"].tools == ["shout"]
    assert any("broken" in e and "boom" in e for e in pm.errors)

    router = make_router(big_script=[tool_call("shout", text="hey"), tool_call("write_file", path=".env", content="X=1"),
                                     Completion(content="ok")])
    agent = Agent(cfg, router, reg, SkillRegistry([]), MemoryStore(cfg.state_dir / "m.jsonl"), hooks=hooks)
    results = [e.data["result"] for e in [e async for e in agent.run("go")] if e.type == "tool_result"]
    assert results[0] == "HEY" and "secrets files are off-limits" in results[1]
    assert not (cfg.workspace / ".env").exists()


def test_config_parses_new_sections_and_env_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("CADENCE_HOME", str(tmp_path))
    monkeypatch.setenv("TG_TOKEN", "123:abc")
    (tmp_path / "config.yaml").write_text(
        "memory: {backend: qdrant, min_similarity: 0.6}\nsandbox: {mode: bwrap, network: false}\n"
        "vision: {name: qwen/qwen3.5-9b}\nhooks:\n  - {event: pre_tool, matcher: bash, command: ./guard.sh}\n"
        "plugins: {disabled: [noisy]}\nchannels:\n  telegram: {token: $TG_TOKEN, allowed_chats: [42]}\n")
    import importlib
    import cadence.config as c
    importlib.reload(c)
    cfg = c.load_config(tmp_path)
    assert cfg.memory.backend == "qdrant" and cfg.memory.min_similarity == 0.6
    assert cfg.sandbox.mode == "bwrap" and cfg.sandbox.network is False
    assert cfg.vision.name == "qwen/qwen3.5-9b" and cfg.vision.base_url == cfg.big.base_url
    assert cfg.hooks[0].matcher == "bash" and cfg.disabled_plugins == ["noisy"]
    assert c.expand_env(cfg.channels["telegram"]["token"]) == "123:abc"


# ─────────────────────────────────────────────────────────────────── sandbox
needs_bwrap = pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")


@needs_bwrap
async def test_bwrap_confines_writes_hides_home_and_cuts_network(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    sb = Sandbox(SandboxConfig(mode="bwrap", network=False), ws)
    res = await sb.run(f"touch inside.txt; touch {outside} 2>/dev/null; touch /etc/cadence_x 2>/dev/null && echo ETC; "
                       "ls -A ~ | wc -l; timeout 3 bash -c 'echo > /dev/tcp/1.1.1.1/53' 2>/dev/null && echo NET || echo nonet", 20)
    assert res.exit == 0 and (ws / "inside.txt").exists()
    # /tmp inside the sandbox is a private tmpfs: writes there never reach the host
    assert not outside.exists() and "ETC" not in res.output
    lines = res.output.split()
    assert "nonet" in lines


@needs_bwrap
async def test_bwrap_timeout_kills_command(tmp_path):
    res = await Sandbox(SandboxConfig(mode="bwrap"), tmp_path).run("sleep 30", 1)
    assert res.timed_out


@needs_bwrap
async def test_goal_verify_runs_inside_sandbox(cfg, make_router):
    from test_tasks import make_runner, reflection
    from cadence.tasks import Task

    router = make_router(big_script=[Completion(content="did it"), reflection("done")])
    runner, store, _ = make_runner(cfg, router)
    runner.sandbox = Sandbox(SandboxConfig(mode="bwrap"), cfg.workspace)
    goal = store.add(Task(title="t", verify_command="touch /etc/cadence_should_fail"))
    await runner.run(goal)
    assert goal.reflections[0]["verify"]["exit"] != 0  # /etc is read-only inside the sandbox


def test_docker_argv_mounts_workspace_and_drops_network(tmp_path):
    argv = Sandbox(SandboxConfig(mode="docker", network=False, image="img:1"), tmp_path).argv("make test", "n1")
    ws = str(tmp_path.resolve())
    assert argv[:3] == ["docker", "run", "--rm"] and f"{ws}:{ws}" in argv and "--network" in argv
    assert argv[-4:] == ["img:1", "bash", "-c", "make test"]


# ────────────────────────────────────────────────────────────────── channels
def test_chunk_splits_long_text_on_line_boundaries():
    parts = chunk("\n".join(f"line {i} " + "x" * 50 for i in range(200)), 1000)
    assert all(len(p) <= 1000 for p in parts) and all(p.startswith("line") for p in parts)


async def _runtime(cfg, router):
    return await Runtime(cfg, router=router, memory=MemoryStore(cfg.state_dir / "m.jsonl")).start()


class FakeTelegram:
    def __init__(self, updates):
        self.updates, self.sent = list(updates), []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content or b"{}")
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "cadence_bot"}})
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if method == "sendMessage":
            self.sent.append((str(body["chat_id"]), body["text"]))
        return httpx.Response(200, json={"ok": True, "result": True})


async def test_telegram_routes_allowed_chat_to_agent_and_rejects_others(cfg, make_router):
    rt = await _runtime(cfg, make_router(big_script=[Completion(content="hello from cadence")]))
    fake = FakeTelegram([
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "hi"}},
        {"update_id": 2, "message": {"chat": {"id": 666}, "text": "let me in"}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/goals"}},
    ])
    ch = TelegramChannel(Bridge(rt, "telegram"), "T", {"42"}, poll_timeout=0, api_base="http://tg",
                         transport=httpx.MockTransport(fake))
    stop = asyncio.Event()
    task = asyncio.create_task(ch.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if len([s for s in fake.sent if s[0] == "42"]) >= 2:
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    await rt.close()
    assert ("42", "hello from cadence") in fake.sent and ("42", "no goals") in fake.sent
    assert any(c == "666" and "Not authorized" in t for c, t in fake.sent)


def test_telegram_requires_allowlist(cfg):
    with pytest.raises(ValueError, match="allowed_chats"):
        TelegramChannel(Bridge.__new__(Bridge), "T", set())


async def _http(port: int, raw: bytes) -> str:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw)
    await w.drain()
    data = await r.read()
    w.close()
    return data.decode()


async def test_whatsapp_webhook_verifies_signature_and_replies(cfg, make_router):
    rt = await _runtime(cfg, make_router(big_script=[Completion(content="pong")]))
    sent = []

    def graph(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    ch = WhatsAppChannel(Bridge(rt, "whatsapp"), access_token="tok", phone_number_id="99", app_secret="s3cret",
                         verify_token="vt", allowed_numbers={"15551234567"}, port=0, graph_base="http://graph",
                         transport=httpx.MockTransport(graph))
    stop = asyncio.Event()
    await ch.start()
    task = asyncio.create_task(ch.run(stop))

    ok = await _http(ch.port, b"GET /whatsapp?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=abc HTTP/1.1\r\n\r\n")
    bad = await _http(ch.port, b"GET /whatsapp?hub.mode=subscribe&hub.verify_token=no&hub.challenge=abc HTTP/1.1\r\n\r\n")
    assert ok.startswith("HTTP/1.1 200") and ok.endswith("abc") and bad.startswith("HTTP/1.1 403")

    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"id": "m1", "from": "15551234567", "type": "text", "text": {"body": "ping"}},
        {"id": "m2", "from": "19998887777", "type": "text", "text": {"body": "intruder"}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    forged = await _http(ch.port, b"POST /whatsapp HTTP/1.1\r\nContent-Length: %d\r\nX-Hub-Signature-256: sha256=00\r\n\r\n%s"
                         % (len(body), body))
    assert forged.startswith("HTTP/1.1 403")
    good = await _http(ch.port, b"POST /whatsapp HTTP/1.1\r\nContent-Length: %d\r\nX-Hub-Signature-256: %s\r\n\r\n%s"
                       % (len(body), sig.encode(), body))
    assert good.startswith("HTTP/1.1 200")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if sent:
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    await rt.close()
    assert sent == [{"messaging_product": "whatsapp", "to": "15551234567", "type": "text", "text": {"body": "pong"}}]


async def test_bridge_steers_a_busy_chat_instead_of_starting_a_second_turn(cfg, make_router):
    router = make_router(big_script=[Completion(content="first done")])
    rt = await _runtime(cfg, router)
    bridge = Bridge(rt, "test")
    replies: list[str] = []

    async def reply(t):
        replies.append(t)

    st = bridge._state("c1")
    st.busy = True  # simulate an in-flight turn
    await bridge.handle("c1", "also check the docs", reply)
    assert replies == ["↪ steering the running turn"] and not st.agent._steer.empty()
    st.busy = False
    await bridge.handle("c1", "go", reply)
    assert replies[-1] == "first done"
    assert any("also check the docs" in str(m.get("content")) for m in router.big.calls[0])
    await rt.close()


async def test_memory_is_scoped_to_its_project_but_shared_items_reach_everyone(tmp_path):
    from cadence.memory import MemoryStore
    m = MemoryStore(tmp_path / "m.jsonl")
    m.add("semantic", "garden uses eleventy for the static site", [], "garden")
    m.add("semantic", "the mcp server speaks stdio json-rpc", [], "mcp")
    m.add("procedural", "always run the formatter before committing a site", [])  # shared
    seen = {i.text for i in await m.asearch("site eleventy mcp formatter", k=10, project="garden")}
    assert seen == {"garden uses eleventy for the static site", "always run the formatter before committing a site"}
    assert len(m.recent(10, project="mcp")) == 2 and len(m.recent(10)) == 3
    # persisted with the project, and a legacy line without one loads as shared
    assert {i.project for i in MemoryStore(tmp_path / "m.jsonl").recent(10)} == {"garden", "mcp", ""}
