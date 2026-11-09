"""Canvas: documents the agent writes for the user (plans, reports, specs, HTML mockups, code drafts).

The agent creates and revises documents with the `canvas_*` tools; the web UI shows them beside the
chat, where the user can edit them too. The agent re-reads a document before revising it, so it sees the
user's edits. Each save keeps the previous version (last 20), so nothing either side writes is lost.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .store import JsonStore
from .tools import Tool, ToolError, Workspace, _schema

FORMATS = ("markdown", "html", "code", "text")
MAX_VERSIONS = 20


@dataclass
class CanvasDoc:
    title: str
    content: str = ""
    format: str = "markdown"  # markdown | html | code | text
    language: str = ""        # for format=code
    project: str = ""
    author: str = "agent"     # who saved last: agent | user
    session: str = ""         # chat session that created it
    streaming: bool = False   # the agent is still writing it (a draft shown live; canvas_write claims it when the call completes)
    versions: list[dict[str, Any]] = field(default_factory=list)  # [{at, author, content}], oldest first
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("versions")
        d.pop("content")
        d["size"] = len(self.content)
        d["version_count"] = len(self.versions)
        return d


class CanvasStore(JsonStore[CanvasDoc]):
    def __init__(self, path: Path):
        super().__init__(path, CanvasDoc)

    def list(self, project: str | None = None) -> list[CanvasDoc]:
        return sorted((d for d in self.all() if project is None or d.project == project), key=lambda d: d.updated, reverse=True)

    def save(self, doc: CanvasDoc, content: str, author: str, **changes: Any) -> CanvasDoc:
        if content != doc.content and doc.content:
            doc.versions = (doc.versions + [{"at": doc.updated, "author": doc.author, "content": doc.content}])[-MAX_VERSIONS:]
        return self.update(doc, content=content, author=author, **changes)


_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/", "\\": "\\", '"': '"'}


def partial_args(raw: str) -> dict[str, str]:
    """String fields of a JSON object that may be cut off mid-value (a tool call still being generated)."""
    out: dict[str, str] = {}
    i, n = 0, len(raw)
    while i < n:
        if raw[i] != '"':
            i += 1
            continue
        j, key = i + 1, []
        while j < n and raw[j] != '"':
            key.append(raw[j])
            j += 2 if raw[j] == "\\" else 1
        if j >= n:
            break
        k = j + 1
        while k < n and raw[k] in " \t\r\n":
            k += 1
        if k >= n or raw[k] != ":":  # a string value, not a key: skip past it
            i = j + 1
            continue
        k += 1
        while k < n and raw[k] in " \t\r\n":
            k += 1
        if k >= n:
            break
        if raw[k] != '"':  # non-string value: skip to the next top-level separator
            while k < n and raw[k] not in ",}":
                k += 1
            i = k
            continue
        k += 1
        val: list[str] = []
        while k < n and raw[k] != '"':
            c = raw[k]
            if c == "\\":
                if k + 1 >= n:
                    break
                e = raw[k + 1]
                if e == "u":
                    if k + 6 > n:
                        break
                    try:
                        val.append(chr(int(raw[k + 2:k + 6], 16)))
                    except ValueError:
                        pass
                    k += 6
                    continue
                val.append(_ESC.get(e, e))
                k += 2
                continue
            val.append(c)
            k += 1
        out[("".join(key))] = "".join(val)
        if k >= n:
            break
        i = k + 1
    return out


Publish = Callable[[dict[str, Any]], Awaitable[None]]


def canvas_tools(store: CanvasStore, project: str, publish: Publish | None, workspace: Workspace,
                 session: Callable[[], str] = lambda: "") -> list[Tool]:
    async def announce(doc: CanvasDoc) -> None:
        if publish:
            await publish({"kind": "canvas", "project": project, "doc": doc.summary(), "focus": True})

    def find(ref: str) -> CanvasDoc:
        doc = store.get(ref)
        if doc is None or doc.project != project:
            doc = next((d for d in store.list(project) if d.title.lower() == ref.lower()), None)
        if doc is None:
            raise ToolError(f"no canvas document '{ref}'. Use canvas_list to see them.")
        return doc

    async def canvas_write(args: dict[str, Any]) -> str:
        title = str(args.get("title", "")).strip()
        content = str(args.get("content", ""))
        fmt = str(args.get("format", "markdown"))
        if fmt not in FORMATS:
            raise ToolError(f"format must be one of {', '.join(FORMATS)}")
        ref = str(args.get("id", "")).strip()
        drafts = [d for d in store.list(project) if d.streaming]  # newest first; a draft is being drawn live by this very call
        if ref:  # updating an existing document: a live draft from this call was a misread, drop it
            for draft in drafts:
                store.delete(draft.id)
        if ref:
            doc = find(ref)
            store.save(doc, content, "agent", title=title or doc.title, format=fmt, language=str(args.get("language", doc.language)))
            verb = "updated"
        else:
            if not title:
                raise ToolError("title is required for a new document")
            draft = next((d for d in drafts if d.title == title[:120]), drafts[0] if drafts else None)
            if draft:
                doc = store.update(draft, title=title[:120], content=content, format=fmt, language=str(args.get("language", "")),
                                   streaming=False, author="agent")
            else:
                doc = store.add(CanvasDoc(title=title[:120], content=content, format=fmt, language=str(args.get("language", "")),
                                      project=project, session=session()))
            verb = "created"
        await announce(doc)
        return f"{verb} canvas document {doc.id} '{doc.title}' ({len(content)} chars). The user can see and edit it."

    async def canvas_edit(args: dict[str, Any]) -> str:
        doc = find(str(args.get("id", "")))
        old, new = str(args.get("old_text", "")), str(args.get("new_text", ""))
        count = doc.content.count(old) if old else 0
        if count != 1:
            raise ToolError(f"old_text must occur exactly once (found {count}). canvas_read the document first; the user may have edited it.")
        store.save(doc, doc.content.replace(old, new, 1), "agent")
        await announce(doc)
        return f"edited canvas document {doc.id}"

    async def canvas_read(args: dict[str, Any]) -> str:
        doc = find(str(args.get("id", "")))
        who = "the user" if doc.author == "user" else "you"
        return f"# {doc.title}  (id {doc.id}, {doc.format}, last saved by {who})\n\n{doc.content}"

    async def canvas_list(_args: dict[str, Any]) -> str:
        docs = store.list(project)
        return "\n".join(f"{d.id}  {d.title}  [{d.format}, {len(d.content)} chars, last by {d.author}]" for d in docs) or "(no canvas documents)"

    async def canvas_export(args: dict[str, Any]) -> str:
        doc = find(str(args.get("id", "")))
        path = workspace.resolve(str(args.get("path", "")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc.content, encoding="utf-8")
        return f"wrote {workspace.rel(path)} ({len(doc.content)} chars)"

    return [
        Tool("canvas_write", "Create a document on the canvas the user sees beside the chat (plans, reports, specs, docs, "
             "HTML mockups, code drafts), or replace one by id. Prefer this over long chat replies for anything the user "
             "will read, review or edit. format: markdown|html|code|text.",
             _schema({"title": {"type": "string"}, "content": {"type": "string"}, "format": {"type": "string", "enum": list(FORMATS)},
                      "language": {"type": "string"}, "id": {"type": "string", "description": "existing document to replace"}},
                     ["content"]), canvas_write, source="canvas"),
        Tool("canvas_edit", "Replace one exact, unique snippet in a canvas document (read it first: the user may have edited it).",
             _schema({"id": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
                     ["id", "old_text", "new_text"]), canvas_edit, source="canvas"),
        Tool("canvas_read", "Read a canvas document (by id or title), including the user's edits.",
             _schema({"id": {"type": "string"}}, ["id"]), canvas_read, source="canvas", read_only=True),
        Tool("canvas_list", "List this project's canvas documents.", _schema({}, []), canvas_list, source="canvas", read_only=True),
        Tool("canvas_export", "Save a canvas document as a file in the project.",
             _schema({"id": {"type": "string"}, "path": {"type": "string"}}, ["id", "path"]), canvas_export, source="canvas"),
    ]
