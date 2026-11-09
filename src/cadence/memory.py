"""Three-type persistent memory plus a per-session scratchpad.

* episodic   — what happened (task outcomes, reflections)
* semantic   — facts about the project/user/environment
* procedural — how to do things (lessons, recipes, conventions)

Items are append-only JSONL (the source of truth). Retrieval is hybrid: vector similarity from an
embedding model blended with keyword overlap and recency. Vectors live in a pluggable index —
`LocalVectorIndex` (JSONL sidecar, no services) or `QdrantIndex` (HTTP, e.g. the docker-compose in
deploy/). Indexing is lazy: `add()` stays synchronous and `asearch()` embeds anything not yet indexed.
If the embedder or index is unavailable, `asearch()` degrades to keyword search.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from .store import atomic_write

log = logging.getLogger(__name__)

KINDS = ("episodic", "semantic", "procedural")
_WORD = re.compile(r"[a-z0-9_]{3,}")
_STOP = frozenset(
    "the and for are was were with that this from have has had not but you your our use used using what which who "
    "how why when where can could should would will into onto about than then them they their there these those its "
    "all any some get got does did done just also only very more most such over under out off per via".split()
)
EMBED_BATCH = 32


NEAR_DUPLICATE = 0.7  # token-overlap (Jaccard) at which a new memory counts as one we already have


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@dataclass
class MemoryItem:
    kind: str
    text: str
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created: float = field(default_factory=time.time)
    hits: int = 0
    project: str = ""  # project id it was learned in; "" = shared by every project (also every memory from before scoping)


class VectorError(RuntimeError):
    """The vector index could not be read or written."""


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class VectorIndex(Protocol):
    async def ids(self) -> set[str]: ...

    async def upsert(self, rows: list[tuple[MemoryItem, list[float]]]) -> None: ...

    async def query(self, vector: list[float], k: int, kind: str | None = None) -> list[tuple[str, float]]: ...

    async def aclose(self) -> None: ...


class LocalVectorIndex:
    """Normalized vectors in a JSONL sidecar; brute-force cosine. Fine into the tens of thousands of items.

    Each row records the embedding model, so switching models re-embeds instead of mixing spaces.
    """

    def __init__(self, path: Path, model: str):
        self.path = Path(path)
        self.model = model
        self._vecs: dict[str, tuple[str, list[float]]] = {}  # id -> (kind, unit vector)
        self._lock = threading.Lock()
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("model") == model and isinstance(row.get("v"), list):
                    self._vecs[row["id"]] = (row.get("kind", ""), row["v"])

    async def ids(self) -> set[str]:
        return set(self._vecs)

    async def upsert(self, rows: list[tuple[MemoryItem, list[float]]]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                for item, vec in rows:
                    unit = _normalize(vec)
                    self._vecs[item.id] = (item.kind, unit)
                    fh.write(json.dumps({"id": item.id, "kind": item.kind, "model": self.model, "v": unit}) + "\n")

    async def query(self, vector: list[float], k: int, kind: str | None = None) -> list[tuple[str, float]]:
        q = _normalize(vector)
        scored = [
            (mid, sum(a * b for a, b in zip(q, v)))
            for mid, (k_, v) in self._vecs.items()
            if not kind or k_ == kind
        ]
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:k]

    async def aclose(self) -> None:
        pass


class QdrantIndex:
    """Qdrant over its REST API (no client library). The collection is created on first upsert."""

    def __init__(self, url: str, collection: str, model: str, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.collection = collection
        self.model = model
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=timeout, transport=transport)
        self._ready = False

    @staticmethod
    def _point_id(mid: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cadence:{mid}"))

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            resp = await self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise VectorError(f"qdrant unreachable: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise VectorError(f"qdrant {method} {path}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def _ensure(self, dim: int) -> None:
        if self._ready:
            return
        try:
            await self._request("GET", f"/collections/{self.collection}")
        except VectorError:
            await self._request("PUT", f"/collections/{self.collection}",
                                json={"vectors": {"size": dim, "distance": "Cosine"}})
        self._ready = True

    def _filter(self, kind: str | None) -> dict[str, Any]:
        must = [{"key": "model", "match": {"value": self.model}}]
        if kind:
            must.append({"key": "kind", "match": {"value": kind}})
        return {"must": must}

    async def ids(self) -> set[str]:
        out: set[str] = set()
        offset: Any = None
        try:
            await self._request("GET", f"/collections/{self.collection}")
        except VectorError as exc:
            if "HTTP 404" in str(exc):
                return out  # nothing indexed yet
            raise
        while True:
            body: dict[str, Any] = {"limit": 512, "with_payload": ["mid"], "with_vector": False, "filter": self._filter(None)}
            if offset is not None:
                body["offset"] = offset
            data = (await self._request("POST", f"/collections/{self.collection}/points/scroll", json=body))["result"]
            out.update(p["payload"]["mid"] for p in data.get("points", []) if p.get("payload", {}).get("mid"))
            offset = data.get("next_page_offset")
            if offset is None:
                return out

    async def upsert(self, rows: list[tuple[MemoryItem, list[float]]]) -> None:
        if not rows:
            return
        await self._ensure(len(rows[0][1]))
        points = [{"id": self._point_id(item.id), "vector": vec,
                   "payload": {"mid": item.id, "kind": item.kind, "model": self.model}} for item, vec in rows]
        await self._request("PUT", f"/collections/{self.collection}/points", params={"wait": "true"}, json={"points": points})

    async def query(self, vector: list[float], k: int, kind: str | None = None) -> list[tuple[str, float]]:
        body = {"vector": vector, "limit": k, "with_payload": ["mid"], "filter": self._filter(kind)}
        try:
            data = await self._request("POST", f"/collections/{self.collection}/points/search", json=body)
        except VectorError as exc:
            if "HTTP 404" in str(exc):
                return []
            raise
        return [(p["payload"]["mid"], float(p["score"])) for p in data.get("result", []) if p.get("payload")]

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class SemanticIndex:
    """An embedder plus a vector index, with the embedder's task prefixes."""

    embedder: Embedder
    index: VectorIndex
    query_prefix: str = ""
    doc_prefix: str = ""


class MemoryStore:
    def __init__(self, path: Path, semantic: SemanticIndex | None = None, semantic_weight: float = 0.75,
                 min_similarity: float = 0.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.semantic = semantic
        self.semantic_weight = semantic_weight
        self.min_similarity = min_similarity  # cosine below this counts as unrelated (embedders have a baseline)
        self.last_error = ""  # why the last asearch fell back to keywords, if it did
        self._lock = threading.Lock()
        self._items: list[MemoryItem] = []
        self._indexed: set[str] | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                self._items.append(MemoryItem(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # tolerate partial/corrupt lines

    @staticmethod
    def _visible(item: MemoryItem, project: str | None) -> bool:
        """`project=None` sees everything; otherwise a project sees its own memories plus the shared ones."""
        return project is None or item.project in ("", project)

    def add(self, kind: str, text: str, tags: list[str] | None = None, project: str = "") -> MemoryItem:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        text = text.strip()
        if not text:
            raise ValueError("memory text is empty")
        words = set(_tokens(text))
        for existing in self._items:  # exact or near-duplicate (same kind and project): keep the first
            if existing.kind == kind and existing.project == project and (
                    existing.text == text or self._overlap(words, set(_tokens(existing.text))) >= NEAR_DUPLICATE):
                return existing
        item = MemoryItem(kind=kind, text=text, tags=tags or [], project=project)
        with self._lock:
            self._items.append(item)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
        return item

    @staticmethod
    def _overlap(a: set[str], b: set[str]) -> float:
        return len(a & b) / len(a | b) if a and b else 0.0

    def dedupe(self) -> int:
        """Drop near-duplicate memories (same kind and project), keeping the oldest. Returns how many went."""
        keep: list[MemoryItem] = []
        sets: list[set[str]] = []
        for item in self._items:
            words = set(_tokens(item.text))
            if any(k.kind == item.kind and k.project == item.project and (k.text == item.text or self._overlap(words, w) >= NEAR_DUPLICATE)
                   for k, w in zip(keep, sets)):
                continue
            keep.append(item)
            sets.append(words)
        removed = len(self._items) - len(keep)
        if removed:
            with self._lock:
                self._items = keep
                atomic_write(self.path, "".join(json.dumps(asdict(i), ensure_ascii=False) + "\n" for i in keep))
        return removed

    def delete(self, mid: str) -> bool:
        """Forget one memory. Its vector stays in the index but is ignored (ranking skips unknown ids)."""
        with self._lock:
            keep = [i for i in self._items if i.id != mid]
            if len(keep) == len(self._items):
                return False
            self._items = keep
            atomic_write(self.path, "".join(json.dumps(asdict(i), ensure_ascii=False) + "\n" for i in keep))
        return True

    # ------------------------------------------------------------- retrieval
    def _keyword_scores(self, query: str, kind: str | None, project: str | None = None) -> dict[str, float]:
        q = set(_tokens(query))
        if not q:
            return {}
        scores: dict[str, float] = {}
        for item in self._items:
            if (kind and item.kind != kind) or not self._visible(item, project):
                continue
            words = set(_tokens(item.text)) | {t.lower() for t in item.tags}
            overlap = len(q & words)
            if overlap:
                scores[item.id] = overlap / math.sqrt(len(words) + 1)
        return scores

    @staticmethod
    def _recency(item: MemoryItem, now: float) -> float:
        return math.exp(-(now - item.created) / (60 * 60 * 24 * 30))

    def _rank(self, scores: dict[str, float], k: int) -> list[MemoryItem]:
        by_id = {i.id: i for i in self._items}
        now = time.time()
        ranked = sorted(((s + 0.15 * self._recency(by_id[mid], now), by_id[mid]) for mid, s in scores.items() if mid in by_id),
                        key=lambda p: p[0], reverse=True)
        hits = [i for _, i in ranked[:k]]
        for h in hits:
            h.hits += 1
        return hits

    def search(self, query: str, k: int = 5, kind: str | None = None, project: str | None = None) -> list[MemoryItem]:
        """Keyword-only retrieval (synchronous; no model calls)."""
        return self._rank(self._keyword_scores(query, kind, project), k)

    async def asearch(self, query: str, k: int = 5, kind: str | None = None, project: str | None = None) -> list[MemoryItem]:
        """Hybrid vector + keyword retrieval; falls back to `search` if the embedder/index is down."""
        if not self.semantic or not query.strip():
            return self.search(query, k, kind, project)
        try:
            await self.sync_index()
            [qvec] = await self.semantic.embedder.embed([self.semantic.query_prefix + query])
            vec_hits = await self.semantic.index.query(qvec, max(k * 4, 20), kind)
        except Exception as exc:  # noqa: BLE001 — retrieval must never break the agent
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("semantic memory unavailable, using keywords: %s", self.last_error)
            return self.search(query, k, kind, project)
        self.last_error = ""
        kw = self._keyword_scores(query, kind, project)
        visible = {i.id for i in self._items if self._visible(i, project)}
        top_kw = max(kw.values(), default=0.0) or 1.0
        w, floor = self.semantic_weight, self.min_similarity
        scores: dict[str, float] = {}
        for mid, sim in vec_hits:
            if sim > floor and mid in visible:
                scores[mid] = w * (sim - floor) / (1.0 - floor)
        for mid, s in kw.items():
            scores[mid] = scores.get(mid, 0.0) + (1 - w) * (s / top_kw)
        return self._rank(scores, k)

    async def sync_index(self) -> int:
        """Embed and index every item the vector index does not have yet. Returns how many were added."""
        if not self.semantic:
            return 0
        if self._indexed is None:
            self._indexed = await self.semantic.index.ids()
        missing = [i for i in self._items if i.id not in self._indexed]
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start : start + EMBED_BATCH]
            vecs = await self.semantic.embedder.embed([self.semantic.doc_prefix + i.text for i in batch])
            await self.semantic.index.upsert(list(zip(batch, vecs)))
            self._indexed.update(i.id for i in batch)
        return len(missing)

    def recent(self, k: int = 5, kind: str | None = None, project: str | None = None) -> list[MemoryItem]:
        items = [i for i in self._items if (not kind or i.kind == kind) and self._visible(i, project)]
        return items[-k:]

    def stats(self) -> dict[str, Any]:
        counts = {kind: sum(1 for i in self._items if i.kind == kind) for kind in KINDS}
        return {"total": len(self._items), **counts, "indexed": len(self._indexed) if self._indexed is not None else None}

    async def aclose(self) -> None:
        if self.semantic:
            await self.semantic.embedder.aclose()
            await self.semantic.index.aclose()

    def __len__(self) -> int:
        return len(self._items)


def build_memory(cfg: Any) -> MemoryStore:
    """Construct the store described by `cfg.memory` (a `cadence.config.Config`)."""
    from .llm import OpenAICompatEmbedder

    mc = cfg.memory
    path = cfg.state_dir / "memory.jsonl"
    if mc.backend == "keyword":
        return MemoryStore(path)
    if mc.backend not in ("local", "qdrant"):
        raise ValueError(f"memory.backend must be local, qdrant or keyword (got {mc.backend!r})")
    embedder = OpenAICompatEmbedder(mc.embed_url or cfg.big.base_url, mc.embed_model, api_key=cfg.big.api_key)
    if mc.backend == "qdrant":
        index: VectorIndex = QdrantIndex(mc.qdrant_url, mc.collection, mc.embed_model)
    else:
        index = LocalVectorIndex(cfg.state_dir / "memory.vectors.jsonl", mc.embed_model)
    return MemoryStore(path, SemanticIndex(embedder, index, mc.query_prefix, mc.doc_prefix), mc.semantic_weight,
                       mc.min_similarity)


class Scratchpad:
    """Volatile working notes the agent keeps for itself; persisted for crash recovery."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.notes: dict[str, str] = {}
        if path and path.is_file():
            try:
                self.notes = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.notes = {}

    def set(self, key: str, value: str) -> None:
        self.notes[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        removed = self.notes.pop(key, None) is not None
        self._save()
        return removed

    def render(self, limit: int = 2500) -> str:
        if not self.notes:
            return ""
        text = "\n".join(f"- {k}: {v}" for k, v in self.notes.items())
        return text if len(text) <= limit else text[-limit:]

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.notes, ensure_ascii=False, indent=2), encoding="utf-8")
