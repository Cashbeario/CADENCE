export type Todo = { content: string; status: "pending" | "in_progress" | "done" };

export type AttachedFile = { name: string; kind: string; path?: string };

export type Item =
  | { kind: "user"; text: string; images: string[]; files?: AttachedFile[] }
  | { kind: "steer"; text: string }
  | { kind: "assistant"; text: string; streaming?: boolean }
  | { kind: "thinking"; text: string; streaming?: boolean }
  | { kind: "tool"; id: string; name: string; arguments: string; result: string | null }
  | { kind: "note"; tone: "info" | "warn" | "bad"; text: string };

export type SessionSummary = {
  id: string;
  title: string;
  kind?: "chat" | "task";
  updated: number;
  running: boolean;
  live: boolean;
  project: string;
};

export type Usage = { tokens: number; window: number; iteration?: number };

export type SessionDetail = SessionSummary & {
  items: RawItem[];
  todos: Todo[];
  usage: Usage;
  summary: string;
  compactions: number;
};

type RawItem =
  | { role: "user"; text: string; images: string[]; files?: AttachedFile[] }
  | { role: "steer"; text: string }
  | { role: "assistant"; text: string }
  | { role: "tool"; id: string; name: string; arguments: string; result: string | null };

export function fromRaw(raw: RawItem[]): Item[] {
  return raw.map((r) => {
    if (r.role === "tool") return { kind: "tool", id: r.id, name: r.name, arguments: r.arguments, result: r.result };
    if (r.role === "user") return { kind: "user", text: r.text, images: r.images, files: r.files ?? [] };
    return { kind: r.role, text: r.text } as Item;
  });
}

export type Project = {
  id: string;
  name: string;
  path: string;
  description: string;
  instructions: string;
  color: string;
  created: number;
  updated: number;
  exists: boolean;
  default: boolean;
  archived: boolean;
  counts: { goals: number; tasks: number; ideas: number };
};

export type TaskStatus = "proposed" | "pending" | "active" | "done" | "failed" | "blocked";

export type Task = {
  id: string;
  title: string;
  description: string;
  acceptance: string[];
  verify_command: string;
  status: TaskStatus;
  source: "user" | "auto" | "planner" | "idea";
  project: string;
  goal: string;
  goal_title: string;
  context: string;
  attempts: number;
  reflections: { attempt: number; verdict: string; critique: string; verify: { exit: number; output: string } | null; lessons?: string[] }[];
  result: string;
  session: string;
  priority: number;
  estimate_min: number;
  due: number;
  started: number;
  finished: number;
  created: number;
  updated: number;
  activity: { type: string; text: string; at: number } | null;
};

export type JournalEntry = { at: number; kind: string; text: string; task?: string; status?: string; progress?: number };

export type GoalTask = {
  id: string; title: string; status: TaskStatus; updated: number; priority: number; due: number; estimate_min: number; started: number; finished: number;
};

export type Goal = {
  id: string;
  title: string;
  description: string;
  success: string[];
  project: string;
  status: "active" | "paused" | "achieved" | "archived";
  progress: number;
  assessment: string;
  question: string;
  journal: JournalEntry[];
  created: number;
  updated: number;
  last_planned: number;
  tasks_today: number;
  priority: number;
  next_review: number;
  tasks: GoalTask[];
  next_task: GoalTask | null;
  planning: boolean;
};

export type Idea = {
  id: string;
  title: string;
  description: string;
  size: "task" | "goal";
  acceptance: string[];
  verify_command: string;
  project: string;
  source: "user" | "auto";
  status: "new" | "promoted" | "dismissed";
  promoted_to: string;
  tags: string[];
  created: number;
  updated: number;
};

export type MemoryItem = { id: string; kind: "episodic" | "semantic" | "procedural"; text: string; tags: string[]; created: number; hits: number; project: string };

export type Notice = { level: string; message: string; at: number };

export type Status = {
  version: string;
  workspace: string;
  models: Record<"big" | "small", { name: string; url: string; ok: boolean; error?: string }>;
  vision: string;
  memory: { total: number; episodic: number; semantic: number; procedural: number; indexed: number | null; backend: string; embedder: string; error: string };
  sandbox: { mode: string; network: boolean; problem: string };
  plugins: { name: string; origin: string; tools: string[] }[];
  plugin_errors: string[];
  hooks: { event: string; matcher: string; command: string }[];
  mcp: { name: string; connected: boolean; tools: number; error: string }[];
  skills: { name: string; description: string }[];
  tools: { name: string; source: string; description: string; read_only: boolean }[];
  channels: string[];
  restart_required: string[];
  daemon: null | {
    running: string[];
    planning: string[];
    cycles_today: number;
    cycles_per_day: number;
    interval_h: number;
    last_tick: string;
    paused: boolean;
    auto_approve: boolean;
    max_parallel: number;
  };
  notices: Notice[];
};

export type ModelCfg = { name: string; base_url: string; temperature: number; max_tokens: number; context_window: number; timeout: number; has_key: boolean };
export type Provider = { name: string; kind: "lmstudio" | "ollama" | "openai"; url: string; has_key?: boolean; api_key?: string };

export type Settings = {
  big: ModelCfg;
  small: ModelCfg;
  vision: ModelCfg | null;
  memory: { backend: string; embed_model: string; embed_url: string; qdrant_url: string; collection: string; semantic_weight: number; min_similarity: number; query_prefix: string; doc_prefix: string };
  sandbox: { mode: string; network: boolean; hide_home: boolean; ro_binds: string[]; rw_binds: string[]; image: string };
  agent: { max_iterations: number; tool_timeout: number; allow_shell: boolean; tool_output_limit: number; compact_trigger: number; keep_recent_messages: number };
  daemon: { reflection_cycles: number; auto_approve_ideas: boolean; max_parallel_goals: number; goal_daily_tasks: number; max_reflections_per_goal: number };
  providers: Provider[];
  browser: { enabled: boolean; headless: boolean; executable: string };
  ui: Record<string, unknown>;
  locked: Record<string, string>;
  restart_required: string[];
  paths: { settings: string; config: string };
};

export type DiscoveredModel = { id: string; type: string; state: string; context?: number; detail?: string; tools?: boolean; size?: number };
export type Discovery = {
  providers: { name: string; kind: string; url: string; base_url: string; ok: boolean; error: string; models: DiscoveredModel[] }[];
  current: Record<"big" | "small" | "embed", { name: string; base_url: string }> & { vision: { name: string; base_url: string } | null };
};

export type SkillRow = {
  name: string; description: string; path: string; root: string; scope: "user" | "project" | "other"; enabled: boolean; editable: boolean;
  files: string[]; counts: { references: number; scripts: number; assets: number; other: number }; meta: Record<string, string>; warnings: string[];
};
export type SkillFile = { path: string; dir: boolean; size: number; exec: boolean };
export type PluginRow = { name: string; origin: string; enabled: boolean; loaded: boolean; tools: string[]; hooks: number; error: string; editable: boolean; path: string };
export type McpRow = { name: string; command: string; args: string[]; env: Record<string, string>; enabled: boolean; connected: boolean; tools: string[]; error: string };
export type HookRow = { event: string; command: string; matcher: string; timeout: number };
export type Hooks = { events: string[]; shell: HookRow[]; plugin: { event: string; matcher: string; owner: string }[] };

export type Uploaded = { id: string; name: string; kind: "image" | "file"; size: number };

export type PreviewTable = { name: string; columns: string[]; rows: unknown[][]; total_rows: number };
export type Preview = {
  kind: "text" | "code" | "data" | "table" | "document" | "pdf" | "image" | "archive" | "database" | "media" | "binary";
  format: string;
  text: string;
  tables: PreviewTable[];
  pages: number | null;
  meta: Record<string, unknown>;
  warning: string;
};

export type FileEntry = { name: string; path: string; dir: boolean; size: number; mtime: number; hidden: boolean; heavy: boolean };
export type FileContent = { path: string; binary: boolean; size: number; mime: string; mtime?: number; content?: string };

export type CanvasSummary = {
  id: string;
  streaming?: boolean;
  title: string;
  format: "markdown" | "html" | "code" | "text";
  language: string;
  project: string;
  author: "agent" | "user";
  session: string;
  created: number;
  updated: number;
  size: number;
  version_count: number;
};
export type CanvasDoc = CanvasSummary & { content: string; versions: { at: number; author: string; content: string }[] };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json", ...init?.headers } : init?.headers,
  });
  if (!res.ok) throw new ApiError(res.status, (await res.text()) || `${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

const json = (method: string, data?: unknown): RequestInit => ({ method, body: JSON.stringify(data ?? {}) });
const q = (params: Record<string, string | undefined>) =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined) as [string, string][]).toString();

export const api = {
  status: (project?: string) => req<Status>(`/api/status?${q({ project })}`),

  // chat
  sessions: (project?: string) => req<SessionSummary[]>(`/api/sessions?${q({ project })}`),
  newSession: (project: string) => req<SessionSummary>("/api/sessions", json("POST", { project })),
  session: (id: string) => req<SessionDetail>(`/api/sessions/${id}`),
  deleteSession: (id: string) => req<{ ok: boolean }>(`/api/sessions/${id}`, { method: "DELETE" }),
  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return req<Uploaded>("/api/upload", { method: "POST", body: fd });
  },

  // projects
  projects: () => req<Project[]>("/api/projects"),
  suggestName: (path: string) => req<{ name: string; path: string }>("/api/projects/suggest-name", json("POST", { path })),
  addProject: (p: { path: string; name?: string; description?: string; instructions?: string; create?: boolean; color?: string }) =>
    req<Project>("/api/projects", json("POST", p)),
  editProject: (id: string, p: Partial<Pick<Project, "name" | "path" | "description" | "instructions" | "color" | "archived">> & { create?: boolean }) =>
    req<Project>(`/api/projects/${id}`, json("PATCH", p)),
  deleteProject: (id: string) => req<{ ok: boolean }>(`/api/projects/${id}`, { method: "DELETE" }),

  // goals
  goals: (project?: string) => req<Goal[]>(`/api/goals?${q({ project })}`),
  addGoal: (g: { title: string; description?: string; success?: string[]; project: string; paused?: boolean; priority?: number }) => req<Goal>("/api/goals", json("POST", g)),
  editGoal: (id: string, g: Partial<Pick<Goal, "title" | "description" | "success" | "status" | "progress" | "priority">>) => req<Goal>(`/api/goals/${id}`, json("PATCH", g)),
  goalAction: (id: string, action: "plan" | "note", text?: string) => req<Goal>(`/api/goals/${id}/${action}`, json("POST", { text })),
  deleteGoal: (id: string) => req<{ ok: boolean }>(`/api/goals/${id}`, { method: "DELETE" }),

  // tasks
  tasks: (project?: string) => req<Task[]>(`/api/tasks?${q({ project })}`),
  addTask: (t: { title: string; description?: string; verify_command?: string; acceptance?: string[]; project: string; goal?: string }) =>
    req<Task>("/api/tasks", json("POST", t)),
  editTask: (id: string, t: Partial<Pick<Task, "title" | "description" | "verify_command" | "acceptance">>) => req<Task>(`/api/tasks/${id}`, json("PATCH", t)),
  taskAction: (id: string, action: "approve" | "reject" | "retry" | "interrupt" | "steer", text?: string) =>
    req<Task>(`/api/tasks/${id}/${action}`, json("POST", { text })),
  deleteTask: (id: string) => req<{ ok: boolean }>(`/api/tasks/${id}`, { method: "DELETE" }),
  daemon: (action: "pause" | "resume") => req<{ paused: boolean }>(`/api/daemon/${action}`, { method: "POST" }),

  // ideas
  ideas: (project?: string) => req<Idea[]>(`/api/ideas?${q({ project })}`),
  addIdea: (i: { title: string; description?: string; size?: string; project: string; acceptance?: string[]; verify_command?: string }) =>
    req<Idea>("/api/ideas", json("POST", i)),
  editIdea: (id: string, i: Partial<Pick<Idea, "title" | "description" | "size" | "status" | "acceptance" | "verify_command" | "tags">>) =>
    req<Idea>(`/api/ideas/${id}`, json("PATCH", i)),
  deleteIdea: (id: string) => req<{ ok: boolean }>(`/api/ideas/${id}`, { method: "DELETE" }),
  promoteIdea: (id: string, to: "task" | "goal") => req<{ idea: Idea; task?: Task; goal?: Goal }>(`/api/ideas/${id}/promote`, json("POST", { to })),
  generateIdeas: (project: string, n = 3) => req<Idea[]>("/api/ideas/generate", json("POST", { project, n })),

  // memory
  memory: (query: string, kind?: string, project?: string) =>
    req<{ items: MemoryItem[]; fallback: string; stats: Status["memory"] }>(`/api/memory?${q({ q: query, kind: kind || undefined, project: project || undefined })}`),
  addMemory: (kind: string, text: string, project = "") => req<MemoryItem>("/api/memory", json("POST", { kind, text, project })),
  deleteMemory: (id: string) => req<{ ok: boolean }>(`/api/memory/${id}`, { method: "DELETE" }),

  // settings & models
  settings: () => req<Settings>("/api/settings"),
  patchSettings: (patch: Record<string, unknown>) => req<Settings>("/api/settings", json("PATCH", patch)),
  models: () => req<Discovery>("/api/models"),

  // extensions
  skills: (project: string) => req<{ skills: SkillRow[]; errors: string[]; dirs: string[] }>(`/api/skills?${q({ project })}`),
  skill: (name: string, project: string) => req<SkillRow & { content: string }>(`/api/skills/${name}?${q({ project })}`),
  createSkill: (s: { name: string; description: string; scope: "user" | "project"; project: string; content?: string; folders?: string[] }) =>
    req<{ ok: boolean; path: string }>("/api/skills", json("POST", s)),
  saveSkill: (name: string, project: string, content: string) => req<{ ok: boolean }>(`/api/skills/${name}?${q({ project })}`, json("PUT", { content })),
  deleteSkill: (name: string, project: string) => req<{ ok: boolean }>(`/api/skills/${name}?${q({ project })}`, { method: "DELETE" }),
  toggleSkill: (name: string, enabled: boolean) => req<{ ok: boolean }>(`/api/skills/${name}/toggle`, json("POST", { enabled })),
  importSkill: (source: string, scope: "user" | "project", project: string, overwrite = false) =>
    req<{ ok: boolean; installed: string[] }>("/api/skills-import", json("POST", { source, scope, project, overwrite })),
  uploadSkill: (files: File[], scope: "user" | "project", project: string, overwrite = false) => {
    const fd = new FormData();
    // a folder pick keeps relative paths (skill-name/references/x.md) so the layout survives
    files.forEach((f) => fd.append("file", f, (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name));
    return req<{ ok: boolean; installed: string[] }>(`/api/skills-upload?${q({ scope, project, overwrite: overwrite ? "1" : undefined })}`, { method: "POST", body: fd });
  },
  skillFiles: (name: string, project: string) => req<SkillFile[]>(`/api/skills/${name}/files?${q({ project })}`),
  skillFile: (name: string, project: string, path: string) =>
    req<{ path: string; binary: boolean; size: number; content?: string }>(`/api/skills/${name}/file?${q({ project, path })}`),
  skillFileUrl: (name: string, project: string, path: string) => `/api/skills/${name}/file?${q({ project, path, raw: "1" })}`,
  saveSkillFile: (name: string, project: string, path: string, content: string) =>
    req<{ ok: boolean }>(`/api/skills/${name}/file?${q({ project })}`, json("PUT", { path, content })),
  deleteSkillFile: (name: string, project: string, path: string) =>
    req<{ ok: boolean }>(`/api/skills/${name}/file?${q({ project, path })}`, { method: "DELETE" }),
  uploadSkillFiles: (name: string, project: string, dir: string, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("file", f, f.name));
    return req<{ ok: boolean; saved: string[] }>(`/api/skills/${name}/upload?${q({ project, dir })}`, { method: "POST", body: fd });
  },
  skillExportUrl: (name: string, project: string) => `/api/skills/${name}/export?${q({ project })}`,
  plugins: () => req<{ plugins: PluginRow[]; dirs: string[] }>("/api/plugins"),
  createPlugin: (name: string) => req<{ ok: boolean; plugins: PluginRow[] }>("/api/plugins", json("POST", { name })),
  pluginSource: (name: string) => req<{ path: string; source: string }>(`/api/plugins/${name}/source`),
  savePlugin: (name: string, source: string) => req<{ ok: boolean; plugin: PluginRow }>(`/api/plugins/${name}/source`, json("PUT", { source })),
  pluginAction: (name: string, action: "enable" | "disable" | "reload") => req<{ plugins: PluginRow[] }>(`/api/plugins/${name}/${action}`, { method: "POST" }),
  deletePlugin: (name: string) => req<{ plugins: PluginRow[] }>(`/api/plugins/${name}`, { method: "DELETE" }),
  mcp: () => req<McpRow[]>("/api/mcp"),
  putMcp: (name: string, s: { command: string; args: string[] | string; env: Record<string, string>; enabled: boolean }) => req<McpRow[]>(`/api/mcp/${name}`, json("PUT", s)),
  deleteMcp: (name: string) => req<McpRow[]>(`/api/mcp/${name}`, { method: "DELETE" }),
  restartMcp: (name: string) => req<McpRow[]>(`/api/mcp/${name}/restart`, { method: "POST" }),
  importMcp: (block: unknown) => req<{ added: string[]; servers: McpRow[] }>("/api/mcp-import", json("POST", block)),
  hooks: () => req<Hooks>("/api/hooks"),
  putHooks: (hooks: HookRow[]) => req<Hooks>("/api/hooks", json("PUT", { hooks })),
  testHook: (h: HookRow, project: string) => req<{ blocked: boolean; reason: string; context: string }>("/api/hooks/test", json("POST", { ...h, project })),

  // files
  browseDirs: (path = "") => req<{ path: string; parent: string | null; home: string; dirs: { name: string; path: string }[] }>(`/api/fs?${q({ path })}`),
  files: (project: string, path = "") => req<{ path: string; root: string; entries: FileEntry[]; truncated: boolean }>(`/api/files?${q({ project, path })}`),
  searchFiles: (project: string, query: string) => req<FileEntry[]>(`/api/files/search?${q({ project, q: query })}`),
  readFile: (project: string, path: string) => req<FileContent>(`/api/files/read?${q({ project, path })}`),
  preview: (project: string, path: string, opts: { pages?: string; sheet?: string } = {}) =>
    req<Preview>(`/api/files/preview?${q({ project, path, ...opts })}`),
  look: (project: string, path: string, question: string, page = 1) =>
    req<{ text: string }>("/api/files/look", json("POST", { project, path, question, page })),
  rawUrl: (project: string, path: string, download = false) => `/api/files/raw?${q({ project, path, download: download ? "1" : undefined })}`,
  writeFile: (project: string, path: string, content: string, mtime?: number, force?: boolean) =>
    req<FileEntry>("/api/files/write", json("PUT", { project, path, content, mtime, force })),
  mkdir: (project: string, path: string) => req<FileEntry>("/api/files/mkdir", json("POST", { project, path })),
  rename: (project: string, from: string, to: string) => req<FileEntry>("/api/files/rename", json("POST", { project, from, to })),
  deleteFile: (project: string, path: string) => req<{ ok: boolean }>(`/api/files?${q({ project, path })}`, { method: "DELETE" }),
  uploadFiles: (project: string, dir: string, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("file", f, f.name));
    return req<FileEntry[]>(`/api/files/upload?${q({ project, dir })}`, { method: "POST", body: fd });
  },

  // canvas
  canvas: (project: string) => req<CanvasSummary[]>(`/api/canvas?${q({ project })}`),
  canvasDoc: (id: string) => req<CanvasDoc>(`/api/canvas/${id}`),
  newCanvas: (d: { project: string; title: string; format: string; content?: string }) => req<CanvasDoc>("/api/canvas", json("POST", d)),
  saveCanvas: (id: string, d: { content?: string; title?: string; format?: string; base_updated?: number }) => req<CanvasDoc>(`/api/canvas/${id}`, json("PUT", d)),
  restoreCanvas: (id: string, index: number) => req<CanvasDoc>(`/api/canvas/${id}/restore`, json("POST", { index })),
  deleteCanvas: (id: string) => req<{ ok: boolean }>(`/api/canvas/${id}`, { method: "DELETE" }),
  exportCanvas: (id: string, path: string, overwrite = false) => req<FileEntry>(`/api/canvas/${id}/export`, json("POST", { path, overwrite })),
};
