import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { api, fromRaw, type AttachedFile, type Item, type Notice, type Project, type SessionSummary, type Todo, type Usage } from "./api";
import { socket, type ServerMsg } from "./socket";

export type Attachment = { id: string; name: string; kind: "image" | "file"; preview?: string; size?: number };

export type DockTab = "plan" | "canvas" | "browser";
export type Dock = { tab: DockTab | null; canvasId: string | null };

/** Subscribe a component to raw server messages of some kinds (for views that refetch on change). */
export function useLive(kinds: string[], fn: (m: ServerMsg) => void) {
  const ref = useRef(fn);
  ref.current = fn;
  const key = kinds.join(",");
  useEffect(() => socket.on((m) => key.split(",").includes(m.kind) && ref.current(m)), [key]);
}

function readLocal(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function writeLocal(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable */
  }
}

export type Chat = {
  items: Item[];
  running: boolean;
  usage: Usage | null;
  todos: Todo[];
  compactions: number;
  loaded: boolean;
};

type State = {
  sessions: SessionSummary[];
  chats: Record<string, Chat>;
  connected: boolean;
  liveTick: number; // bumps on daemon/task activity so views can refetch
  notices: Notice[];
  projects: Project[];
  project: string; // active project id
  dock: Dock;
  draft: string | null; // text to drop into the composer (from "discuss in chat" buttons)
};

type AgentEvent = { type: string; data: Record<string, any> };

type Action =
  | { t: "sessions"; sessions: SessionSummary[] }
  | { t: "load"; sid: string; chat: Chat }
  | { t: "send"; sid: string; text: string; images: string[]; files: AttachedFile[] }
  | { t: "event"; sid: string; ev: AgentEvent }
  | { t: "steer"; sid: string; text: string }
  | { t: "turn"; sid: string; running: boolean; title?: string; project?: string }
  | { t: "todos"; sid: string; todos: Todo[] }
  | { t: "connected"; v: boolean }
  | { t: "notice"; n: Notice }
  | { t: "tick" }
  | { t: "drop"; sid: string }
  | { t: "projects"; projects: Project[] }
  | { t: "project"; id: string }
  | { t: "dock"; dock: Partial<Dock> }
  | { t: "draft"; text: string | null };

const emptyChat = (): Chat => ({ items: [], running: false, usage: null, todos: [], compactions: 0, loaded: false });

function settle(items: Item[]): Item[] {
  // close any streaming text/thinking block
  return items.map((it) => ("streaming" in it && it.streaming ? { ...it, streaming: false } : it));
}

function applyEvent(chat: Chat, ev: AgentEvent): Chat {
  const items = [...chat.items];
  const last = items[items.length - 1];
  const d = ev.data;
  switch (ev.type) {
    case "token":
      if (last?.kind === "assistant" && last.streaming) items[items.length - 1] = { ...last, text: last.text + d.text };
      else {
        const settled = settle(items);
        settled.push({ kind: "assistant", text: d.text, streaming: true });
        return { ...chat, items: settled };
      }
      return { ...chat, items };
    case "thinking":
      if (last?.kind === "thinking" && last.streaming) items[items.length - 1] = { ...last, text: last.text + d.text };
      else items.push({ kind: "thinking", text: d.text, streaming: true });
      return { ...chat, items };
    case "tool_call":
      return { ...chat, items: [...settle(items), { kind: "tool", id: d.id, name: d.name, arguments: d.arguments, result: null }] };
    case "tool_result": {
      const idx = items.findLastIndex((it) => it.kind === "tool" && it.id === d.id && it.result === null);
      if (idx >= 0) items[idx] = { ...(items[idx] as Extract<Item, { kind: "tool" }>), result: d.result };
      return { ...chat, items };
    }
    case "compaction":
      return {
        ...chat,
        compactions: chat.compactions + 1,
        items: [...items, { kind: "note", tone: "info", text: `Context compacted by the small model · ${d.stage}: ${d.detail} (${(d.before / 1000).toFixed(1)}k → ${(d.after / 1000).toFixed(1)}k tokens)` }],
      };
    case "message_done": {
      const settled = settle(items);
      const tail = settled[settled.length - 1];
      if (d.text && !(tail?.kind === "assistant")) settled.push({ kind: "assistant", text: d.text });
      return { ...chat, items: settled };
    }
    case "error":
      return { ...chat, items: [...settle(items), { kind: "note", tone: "bad", text: d.message }] };
    case "interrupted":
      return { ...chat, items: [...settle(items), { kind: "note", tone: "warn", text: "Interrupted — context kept. Send a message to continue or change course." }] };
    case "status":
      return { ...chat, usage: { tokens: d.tokens, window: d.window, iteration: d.iteration } };
    default:
      return chat;
  }
}

function reducer(s: State, a: Action): State {
  const chat = (sid: string) => s.chats[sid] ?? emptyChat();
  const put = (sid: string, c: Chat): State => ({ ...s, chats: { ...s.chats, [sid]: c } });
  switch (a.t) {
    case "sessions":
      return { ...s, sessions: a.sessions };
    case "load":
      return put(a.sid, a.chat);
    case "send": {
      const c = chat(a.sid);
      return put(a.sid, { ...c, running: true, items: [...settle(c.items), { kind: "user", text: a.text, images: a.images, files: a.files }] });
    }
    case "event":
      return put(a.sid, applyEvent(chat(a.sid), a.ev));
    case "steer": {
      const c = chat(a.sid);
      return put(a.sid, { ...c, items: [...settle(c.items), { kind: "steer", text: a.text }] });
    }
    case "turn": {
      const c = chat(a.sid);
      const sessions = s.sessions.some((x) => x.id === a.sid)
        ? s.sessions.map((x) => (x.id === a.sid ? { ...x, running: a.running, title: a.title || x.title, updated: Date.now() / 1000 } : x))
        : a.project && a.project !== s.project
          ? s.sessions
          : [{ id: a.sid, title: a.title || "New chat", updated: Date.now() / 1000, running: a.running, live: true, project: a.project ?? s.project }, ...s.sessions];
      return { ...s, sessions, chats: { ...s.chats, [a.sid]: { ...c, running: a.running, items: a.running ? c.items : settle(c.items) } } };
    }
    case "todos":
      return put(a.sid, { ...chat(a.sid), todos: a.todos });
    case "connected":
      return { ...s, connected: a.v };
    case "notice":
      return { ...s, notices: [...s.notices, a.n].slice(-50), liveTick: s.liveTick + 1 };
    case "tick":
      return { ...s, liveTick: s.liveTick + 1 };
    case "drop": {
      const chats = { ...s.chats };
      delete chats[a.sid];
      return { ...s, chats, sessions: s.sessions.filter((x) => x.id !== a.sid) };
    }
    case "projects":
      return { ...s, projects: a.projects, project: a.projects.some((p) => p.id === s.project) ? s.project : (a.projects.find((p) => p.default) ?? a.projects[0])?.id ?? "" };
    case "project":
      return { ...s, project: a.id, sessions: [], dock: { ...s.dock, canvasId: null } };
    case "dock":
      return { ...s, dock: { ...s.dock, ...a.dock } };
    case "draft":
      return { ...s, draft: a.text };
  }
}

type Ctx = State & {
  current: Project | undefined;
  setProject: (id: string) => void;
  refreshProjects: () => Promise<Project[]>;
  setDock: (d: Partial<Dock>) => void;
  setDraft: (text: string | null) => void;
  refreshSessions: () => Promise<void>;
  loadChat: (sid: string) => Promise<void>;
  send: (sid: string, text: string, uploads: Attachment[]) => void;
  steer: (sid: string, text: string) => void;
  interrupt: (sid: string) => void;
  remove: (sid: string) => Promise<void>;
  newSession: () => Promise<string>;
};

const StoreCtx = createContext<Ctx | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, {
    sessions: [], chats: {}, connected: false, liveTick: 0, notices: [], projects: [],
    project: readLocal("cadence.project", ""),
    dock: { tab: (readLocal("cadence.dock", "plan") || null) as DockTab | null, canvasId: null },
    draft: null,
  });
  const projectRef = useRef(state.project);
  projectRef.current = state.project;

  const refreshSessions = useCallback(async () => {
    const pid = projectRef.current;
    if (!pid) return;
    const sessions = await api.sessions(pid);
    if (projectRef.current === pid) dispatch({ t: "sessions", sessions });
  }, []);

  const refreshProjects = useCallback(async () => {
    const projects = await api.projects();
    dispatch({ t: "projects", projects });
    return projects;
  }, []);

  // Bootstrap: the server-side UI settings win over this browser's cache, so every browser agrees.
  useEffect(() => {
    Promise.all([api.settings().catch(() => null), refreshProjects()]).then(([settings, projects]) => {
      const saved = settings?.ui?.project as string | undefined;
      if (saved && projects.some((p) => p.id === saved) && saved !== projectRef.current) dispatch({ t: "project", id: saved });
    }).catch(() => undefined);
  }, [refreshProjects]);

  useEffect(() => {
    if (!state.project) return;
    writeLocal("cadence.project", state.project);
    refreshSessions().catch(() => undefined);
  }, [state.project, refreshSessions]);

  useEffect(() => writeLocal("cadence.dock", state.dock.tab ?? ""), [state.dock.tab]);

  useEffect(() => {
    const offState = socket.onState((v) => dispatch({ t: "connected", v }));
    const off = socket.on((m: ServerMsg) => {
      const sid = m.session as string;
      switch (m.kind) {
        case "agent":
          dispatch({ t: "event", sid, ev: m.event as AgentEvent });
          break;
        case "turn":
          dispatch({ t: "turn", sid, running: m.state === "started", title: m.title as string, project: m.project as string });
          break;
        case "steer":
          dispatch({ t: "steer", sid, text: m.text as string });
          break;
        case "todos":
          dispatch({ t: "todos", sid, todos: m.todos as Todo[] });
          break;
        case "notice":
          dispatch({ t: "notice", n: { level: m.level as string, message: m.message as string, at: m.at as number } });
          break;
        case "task_activity":
        case "changed":
        case "ideas":
        case "memory":
          dispatch({ t: "tick" });
          if (m.kind === "changed" && m.what === "projects") refreshProjects().catch(() => undefined);
          break;
        case "canvas":
          if (m.focus && m.project === projectRef.current) dispatch({ t: "dock", dock: { tab: "canvas", canvasId: (m.doc as { id: string }).id } });
          break;
        case "browser_open":
          if (m.project === projectRef.current) dispatch({ t: "dock", dock: { tab: "browser" } });
          break;
      }
    });
    socket.connect();
    return () => {
      off();
      offState();
    };
  }, [refreshSessions, refreshProjects]);

  const loadChat = useCallback(async (sid: string) => {
    const d = await api.session(sid);
    dispatch({
      t: "load",
      sid,
      chat: { items: fromRaw(d.items), running: d.running, usage: d.usage, todos: d.todos, compactions: d.compactions, loaded: true },
    });
  }, []);

  const value = useMemo<Ctx>(
    () => ({
      ...state,
      current: state.projects.find((p) => p.id === state.project),
      setProject: (id: string) => {
        if (id === state.project) return;
        dispatch({ t: "project", id });
        api.patchSettings({ ui: { project: id } }).catch(() => undefined);
      },
      refreshProjects,
      setDock: (dock: Partial<Dock>) => dispatch({ t: "dock", dock }),
      setDraft: (text: string | null) => dispatch({ t: "draft", text }),
      refreshSessions,
      loadChat,
      send: (sid, text, uploads) => {
        const running = state.chats[sid]?.running;
        const images = uploads.filter((u) => u.kind === "image");
        const files = uploads.filter((u) => u.kind === "file");
        if (!running) dispatch({ t: "send", sid, text, images: images.map((u) => u.preview ?? ""), files: files.map((f) => ({ name: f.name, kind: "file" })) });
        socket.send({ op: "send", session: sid, text, images: images.map((u) => u.id), files: files.map((u) => u.id) });
      },
      steer: (sid, text) => socket.send({ op: "steer", session: sid, text }),
      interrupt: (sid) => socket.send({ op: "interrupt", session: sid }),
      remove: async (sid) => {
        await api.deleteSession(sid);
        dispatch({ t: "drop", sid });
      },
      newSession: async () => {
        const s = await api.newSession(state.project);
        dispatch({ t: "load", sid: s.id, chat: { ...emptyChat(), loaded: true } });
        return s.id;
      },
    }),
    [state, refreshSessions, refreshProjects, loadChat],
  );

  return <StoreCtx.Provider value={value}>{children}</StoreCtx.Provider>;
}

export function useStore() {
  const ctx = useContext(StoreCtx);
  if (!ctx) throw new Error("useStore outside StoreProvider");
  return ctx;
}
