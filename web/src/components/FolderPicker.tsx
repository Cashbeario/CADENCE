import { ArrowUp, Folder, Home } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { Modal } from "./ui";

type Listing = Awaited<ReturnType<typeof api.browseDirs>>;

export function FolderPicker({ open, start, onClose, onPick }: { open: boolean; start?: string; onClose: () => void; onPick: (path: string) => void }) {
  const [list, setList] = useState<Listing | null>(null);
  const [error, setError] = useState("");

  const go = useCallback(async (path: string) => {
    try {
      setList(await api.browseDirs(path));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (open) void go(start || "~");
  }, [open, start, go]);

  return (
    <Modal open={open} onClose={onClose} title="Choose a folder"
      footer={<><button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={!list} onClick={() => list && onPick(list.path)}>Use this folder</button></>}>
      <div className="flex items-center gap-1">
        <button className="btn-ghost px-1.5" title="Home" onClick={() => void go("~")}><Home size={14} /></button>
        <button className="btn-ghost px-1.5" title="Up one level" disabled={!list?.parent} onClick={() => list?.parent && void go(list.parent)}><ArrowUp size={14} /></button>
        <div className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted">{list?.path}</div>
      </div>
      {error && <p className="text-[13px] text-bad">{error}</p>}
      <div className="max-h-[50vh] overflow-y-auto rounded-lg border border-line">
        {list?.dirs.map((d) => (
          <div key={d.path} onClick={() => void go(d.path)} className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-[13px] hover:bg-raised">
            <Folder size={14} className="shrink-0 text-brand/80" /><span className="truncate">{d.name}</span>
          </div>
        ))}
        {list && !list.dirs.length && <div className="px-3 py-3 text-[13px] text-faint">No sub-folders here.</div>}
      </div>
    </Modal>
  );
}
