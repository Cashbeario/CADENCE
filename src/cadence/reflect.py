"""Reflection: after a chat turn or a finished task, a short pass over what happened that files
durable memories, new ideas and goal proposals. Nothing here runs work: goal proposals land in the ideas
inbox (size=goal) for the user to promote, exactly like the daemon's own idea cycles.
"""

from __future__ import annotations

import logging
from typing import Any

from .ideas import Idea, IdeaStore
from .llm import LLMError, ModelRouter, content_text
from .memory import KINDS, MemoryStore
from .tasks import TaskStore, _jaccard, extract_json

log = logging.getLogger(__name__)

MAX_TRANSCRIPT = 14000

REFLECT_SYSTEM = (
    "You review one working session between a user and a coding agent, and record what is worth keeping. "
    'Reply with ONLY a JSON object: {"summary":"...","facts":["..."],"lessons":["..."],'
    '"ideas":[{"title":"...","description":"...","size":"task|goal","acceptance":["..."],"verify_command":""}]}. '
    "summary: ONE sentence on what was asked and how it ended (an episodic memory; always give one unless it was small talk). "
    "facts: durable things learned about the user, project, environment or preferences (semantic memory), at most 3. "
    "lessons: how-tos, conventions and pitfalls worth reusing (procedural memory), at most 3. "
    "Each is self-contained and under 300 characters. ideas: follow-up work the session revealed, at most 3; "
    "size=goal only for a multi-session objective. Skip anything already known or speculative. "
    'Use empty lists where nothing qualifies.'
)

SECTIONS = (("summary", "episodic"), ("facts", "semantic"), ("lessons", "procedural"))


def transcript(messages: list[dict[str, Any]], limit: int = MAX_TRANSCRIPT) -> str:
    """The last user turn onward as plain text, tool results clipped, newest content kept when over `limit`."""
    start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    lines: list[str] = []
    for m in messages[start:]:
        role = m.get("role")
        if role == "user":
            lines.append(f"USER: {content_text(m.get('content'))[:3000]}")
        elif role == "assistant":
            if m.get("content"):
                lines.append(f"AGENT: {content_text(m['content'])[:3000]}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                lines.append(f"AGENT called {fn.get('name')}({str(fn.get('arguments', ''))[:200]})")
        elif role == "tool":
            lines.append(f"RESULT: {content_text(m.get('content'))[:400]}")
    text = "\n".join(lines)
    return text if len(text) <= limit else "…" + text[-limit:]


async def reflect(router: ModelRouter, memory: MemoryStore, ideas: IdeaStore, tasks: TaskStore, *, project: str,
                  log_text: str, source: str = "reflection") -> dict[str, int]:
    """Returns how many memories and ideas were filed."""
    done = {"memories": 0, "ideas": 0}
    if len(log_text.strip()) < 80:
        return done
    known = [i.title for i in ideas.list(project=project)] + [t.title for t in tasks.list(project=project)]
    recent = "\n".join(f"- {m.text[:120]}" for m in memory.recent(10, project=project)) or "(none)"
    user = f"Already remembered:\n{recent}\n\nAlready on the list:\n" + ("\n".join(f"- {k}" for k in known[-25:]) or "(none)") + \
        f"\n\nSession:\n{log_text}"
    try:
        comp = await router.small.complete([{"role": "system", "content": REFLECT_SYSTEM}, {"role": "user", "content": user}],
                                           max_tokens=1500, temperature=0.3)
    except LLMError as exc:
        log.warning("reflection skipped: %s", exc)
        return done
    data = extract_json(comp.content)
    if not isinstance(data, dict):
        return done
    found: list[tuple[str, str]] = []
    for key, kind in SECTIONS:
        raw = data.get(key) or []
        for text in ([raw] if isinstance(raw, str) else raw)[:3]:
            found.append((kind, str(text)))
    for m in data.get("memories") or []:  # the older single-list shape
        if isinstance(m, dict):
            found.append((str(m.get("kind", "semantic")), str(m.get("text", ""))))
    for kind, text in found[:8]:
        if not text.strip():
            continue
        before = len(memory)
        try:
            memory.add(kind if kind in KINDS else "semantic", text[:600], [source], project)
        except ValueError:
            continue
        done["memories"] += len(memory) - before  # near-duplicates are folded into the existing memory
    for it in (data.get("ideas") or [])[:3]:
        if not isinstance(it, dict) or not str(it.get("title", "")).strip():
            continue
        title = str(it["title"]).strip()[:120]
        if any(_jaccard(title, k) >= 0.6 for k in known):
            continue
        ideas.add(Idea(title=title, description=str(it.get("description", "")), size="goal" if it.get("size") == "goal" else "task",
                       acceptance=[str(a) for a in it.get("acceptance") or []][:6],
                       verify_command=str(it.get("verify_command", "") or ""), project=project, source=source))
        known.append(title)
        done["ideas"] += 1
    return done
