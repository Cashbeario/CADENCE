"""Configuration: defaults < ~/.cadence/config.yaml < ./.cadence/config.yaml < ~/.cadence/settings.json < env vars.

`settings.json` is written by the web UI. It uses the same keys as config.yaml, but lists in it
(hooks, mcp_servers, providers) replace the lists from the YAML files instead of extending them.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .migrate import legacy_env, migrate_home, migrate_project

legacy_env()  # KESTREL_* (pre-rename) variables still work
HOME_DIR = Path(os.environ.get("CADENCE_HOME", Path.home() / ".cadence"))


@dataclass
class ModelConfig:
    """One OpenAI-compatible model endpoint (LM Studio by default)."""

    name: str
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    temperature: float = 0.2
    max_tokens: int = 8192
    context_window: int = 32768
    reasoning_headroom: int = 6000  # extra tokens granted to short structured calls (planner, ideas, reflection) so a thinking model can finish reasoning and still answer
    timeout: float = 900.0  # seconds to wait on the server (a big context can take minutes to prefill)


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class MemoryConfig:
    """Long-term memory retrieval. `local` keeps vectors in a JSONL sidecar; `qdrant` uses a Qdrant server."""

    backend: str = "local"  # local | qdrant | keyword
    embed_model: str = "text-embedding-nomic-embed-text-v1.5"
    embed_url: str = ""     # empty = same endpoint as the big model
    query_prefix: str = "search_query: "      # nomic-style task prefixes; set both to "" for other embedders
    doc_prefix: str = "search_document: "
    qdrant_url: str = "http://localhost:6333"
    collection: str = "cadence_memory"
    semantic_weight: float = 0.75  # blend of vector similarity vs keyword overlap
    min_similarity: float = 0.55   # cosine floor; nomic-embed scores unrelated text around 0.45-0.5


@dataclass
class SandboxConfig:
    """Isolation for `bash` and goal verify commands."""

    mode: str = "none"  # none | bwrap | docker
    network: bool = True
    hide_home: bool = True  # bwrap: mount an empty tmpfs over $HOME (the workspace is re-bound on top)
    ro_binds: list[str] = field(default_factory=list)
    rw_binds: list[str] = field(default_factory=list)
    image: str = "python:3.12-slim"  # docker mode


@dataclass
class HookConfig:
    event: str
    command: str
    matcher: str = "*"  # glob on the tool name (pre_tool/post_tool); ignored for other events
    timeout: float = 30.0


@dataclass
class Provider:
    """A local model server to discover models on (LM Studio, Ollama, or any OpenAI-compatible URL)."""

    name: str
    kind: str = "lmstudio"  # lmstudio | ollama | openai
    url: str = "http://127.0.0.1:1234"
    api_key: str = ""


DEFAULT_PROVIDERS = [
    Provider("LM Studio", "lmstudio", "http://127.0.0.1:1234"),
    Provider("Ollama", "ollama", "http://127.0.0.1:11434"),
]


@dataclass
class Config:
    big: ModelConfig = field(default_factory=lambda: ModelConfig(name="ornith-1.5-9b", context_window=32768))
    small: ModelConfig = field(
        default_factory=lambda: ModelConfig(name="liquid/lfm2-1.2b", temperature=0.1, max_tokens=1024, context_window=32768)
    )
    workspace: Path = field(default_factory=Path.cwd)
    home: Path = HOME_DIR
    skill_dirs: list[Path] = field(default_factory=list)
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    max_iterations: int = 40
    tool_timeout: float = 120.0
    compact_trigger: float = 0.70   # fraction of the context window that triggers compaction
    compact_target: float = 0.40    # fraction we compact down to
    keep_recent_messages: int = 8
    tool_output_limit: int = 8000   # chars kept from any single tool result
    reflection_cycles: int = 6      # autonomous idea/reflection cycles per day
    max_reflections_per_goal: int = 3
    allow_shell: bool = True
    reflect_after_turns: bool = True  # after each chat turn / finished task, a reflection pass files memories, ideas and goal proposals
    auto_approve_ideas: bool = False  # autonomously generated goals wait for `goals approve` unless true
    max_parallel_goals: int = 2
    goal_review_minutes: float = 60.0  # how often the planner re-evaluates each active goal, even while its tasks are queued
    goal_daily_tasks: int = 6       # tasks the planner may start per long-horizon goal per day
    vision: ModelConfig | None = None  # image understanding; None = use the big model
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    hooks: list[HookConfig] = field(default_factory=list)
    plugin_dirs: list[Path] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)
    disabled_skills: list[str] = field(default_factory=list)
    providers: list[Provider] = field(default_factory=lambda: [Provider(**vars(p)) for p in DEFAULT_PROVIDERS])
    browser: dict[str, Any] = field(default_factory=dict)  # executable, headless, width, height
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)  # telegram / whatsapp settings

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self.home = Path(self.home)
        self.skill_dirs = [Path(p) for p in self.skill_dirs] or [
            self.home / "skills",
            self.workspace / ".cadence" / "skills",
            self.workspace / ".agents" / "skills",
        ]
        self.plugin_dirs = [Path(p) for p in self.plugin_dirs] or [
            self.home / "plugins",
            self.workspace / ".cadence" / "plugins",
        ]

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def settings_path(self) -> Path:
        return self.home / "settings.json"


def _merge_model(base: ModelConfig, data: dict[str, Any]) -> ModelConfig:
    allowed = {f.name for f in fields(ModelConfig)}
    for key, value in data.items():
        if key in allowed:
            setattr(base, key, value)
    return base


def _merge(base: Any, data: dict[str, Any]) -> None:
    allowed = {f.name for f in fields(base)}
    for key, value in (data or {}).items():
        if key in allowed:
            setattr(base, key, value)


def expand_env(value: Any) -> Any:
    """`$NAME` / `${NAME}` strings are read from the environment so secrets stay out of config files."""
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value.strip("${}"), "")
    return value


SCALARS = {
    "max_iterations", "tool_timeout", "compact_trigger", "compact_target", "keep_recent_messages",
    "tool_output_limit", "reflection_cycles", "max_reflections_per_goal", "allow_shell",
    "auto_approve_ideas", "max_parallel_goals", "goal_daily_tasks", "goal_review_minutes", "reflect_after_turns",
}


def _apply(cfg: Config, data: dict[str, Any], *, replace_lists: bool = False) -> None:
    """Layer one config dict onto `cfg` in place (nested objects are mutated, so live holders see changes)."""
    if not isinstance(data, dict):
        return
    _merge_model(cfg.big, data.get("big", {}) or {})
    _merge_model(cfg.small, data.get("small", {}) or {})
    for key in SCALARS & data.keys():
        setattr(cfg, key, data[key])
    if "skill_dirs" in data:
        cfg.skill_dirs = [Path(p).expanduser() for p in data["skill_dirs"]]
    if "vision" in data:
        if not data["vision"]:
            cfg.vision = None
        else:
            cfg.vision = _merge_model(cfg.vision or ModelConfig(name=cfg.big.name, base_url=cfg.big.base_url,
                                                                api_key=cfg.big.api_key), data["vision"])
    _merge(cfg.memory, data.get("memory") or {})
    _merge(cfg.sandbox, data.get("sandbox") or {})
    if "hooks" in data and replace_lists:
        cfg.hooks.clear()
    for spec in data.get("hooks") or []:
        cfg.hooks.append(HookConfig(event=spec["event"], command=spec["command"], matcher=str(spec.get("matcher", "*")),
                                    timeout=float(spec.get("timeout", 30.0))))
    plugins = data.get("plugins") or {}
    if "dirs" in plugins:
        cfg.plugin_dirs = [Path(p).expanduser() for p in plugins["dirs"]]
    # in place: registries hold references to these lists
    cfg.disabled_plugins[:] = list(plugins.get("disabled", cfg.disabled_plugins))
    cfg.disabled_skills[:] = list((data.get("skills") or {}).get("disabled", cfg.disabled_skills))
    for name, spec in (data.get("channels") or {}).items():
        cfg.channels[name] = {**cfg.channels.get(name, {}), **(spec or {})}
    if "mcp_servers" in data and replace_lists:
        cfg.mcp_servers.clear()
    for name, spec in (data.get("mcp_servers") or {}).items():
        cfg.mcp_servers[:] = [s for s in cfg.mcp_servers if s.name != name]
        cfg.mcp_servers.append(
            McpServerConfig(
                name=name,
                command=spec["command"],
                args=[str(a) for a in spec.get("args", [])],
                env={k: str(v) for k, v in (spec.get("env") or {}).items()},
                enabled=bool(spec.get("enabled", True)),
            )
        )
    if "providers" in data:
        cfg.providers[:] = [Provider(name=str(p.get("name") or p.get("kind", "server")), kind=str(p.get("kind", "openai")),
                                     url=str(p.get("url", "")).rstrip("/"), api_key=str(p.get("api_key", "")))
                            for p in data["providers"] or [] if p.get("url")]
    if "browser" in data:
        cfg.browser.update(data["browser"] or {})


def read_settings(cfg: Config) -> dict[str, Any]:
    try:
        data = json.loads(cfg.settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(cfg: Config, data: dict[str, Any]) -> None:
    cfg.settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cfg.settings_path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, cfg.settings_path)


ENV_LOCKS = {  # env var → the setting it pins, so the UI can say why an edit will not survive a restart
    "LMSTUDIO_URL": "big.base_url", "CADENCE_BIG_MODEL": "big.name", "CADENCE_SMALL_MODEL": "small.name",
    "CADENCE_BIG_URL": "big.base_url", "CADENCE_SMALL_URL": "small.base_url", "CADENCE_SANDBOX": "sandbox.mode",
    "CADENCE_MEMORY_BACKEND": "memory.backend",
}


def load_config(workspace: Path | None = None) -> Config:
    cfg = Config(workspace=workspace or Path.cwd())
    migrate_home(cfg.home)        # one-time Kestrel → Cadence move; no-op afterwards
    migrate_project(cfg.workspace)
    for path in (cfg.home / "config.yaml", cfg.workspace / ".cadence" / "config.yaml"):
        if path.is_file():
            _apply(cfg, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    _apply(cfg, read_settings(cfg), replace_lists=True)

    env = os.environ
    if env.get("LMSTUDIO_URL"):
        url = env["LMSTUDIO_URL"].rstrip("/")
        url = url if url.endswith("/v1") else f"{url}/v1"
        cfg.big.base_url = cfg.small.base_url = url
    if env.get("CADENCE_BIG_MODEL"):
        cfg.big.name = env["CADENCE_BIG_MODEL"]
    if env.get("CADENCE_SMALL_MODEL"):
        cfg.small.name = env["CADENCE_SMALL_MODEL"]
    if env.get("CADENCE_BIG_URL"):
        cfg.big.base_url = env["CADENCE_BIG_URL"]
    if env.get("CADENCE_SMALL_URL"):
        cfg.small.base_url = env["CADENCE_SMALL_URL"]
    if env.get("CADENCE_SANDBOX"):
        cfg.sandbox.mode = env["CADENCE_SANDBOX"]
    if env.get("CADENCE_MEMORY_BACKEND"):
        cfg.memory.backend = env["CADENCE_MEMORY_BACKEND"]

    # Workspace-local skill dirs are resolved after overrides.
    cfg.__post_init__()
    return cfg
