import { memo, useCallback, useDeferredValue, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { Camera } from "@/api/types";
import { namesKey, orderOf } from "./order";

export const HEALTH_TONE: Record<Camera["recording_health"]["state"], "good" | "warn" | "bad" | undefined> = {
  recording: "good",
  expected_idle: undefined,
  uncertain: "warn",
  motion_source_degraded: "warn",
  camera_unreachable: "bad",
  write_stalled: "bad",
};

export const CAMERA_DRAG_TYPE = "application/x-nvr-camera";

interface CameraListProps {
  cameras: Camera[] | undefined;
  loading: boolean;
  error: string | null;
  selected: string[];
  onToggle: (id: string) => void;
  /** Shift+Enter on a row: replace the active tile (keyboard alternative to dragging). */
  onReplaceActive?: (id: string) => void;
  capacity: number;
}

/** Alphabetized, searchable cameras; one click adds or removes a camera from the current view. */
export function CameraList({ cameras, loading, error, selected, onToggle, onReplaceActive, capacity }: CameraListProps) {
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query.trim().toLowerCase());
  const chosen = useMemo(() => new Set(selected), [selected]);
  // The rows are given callbacks that stay the same from one render to the next and call the latest ones, so a row is drawn again only when
  // what it shows changes, whatever the page passes (#539)
  const handlers = useRef({ onToggle, onReplaceActive });
  handlers.current = { onToggle, onReplaceActive };
  const toggleRow = useCallback((id: string) => handlers.current.onToggle(id), []);
  const replaceRow = useCallback((id: string) => handlers.current.onReplaceActive?.(id), []);
  const active = useMemo(() => (cameras ?? []).filter(camera => !camera.archived_at), [cameras]);
  // Sorted only when the cameras or their names change (#539): each 15-second refresh brings a new list with the same names
  const key = namesKey(active);
  // The key stands for the names and ids of ``active``, which are all the order reads
  const order = useMemo(() => orderOf(active), [key]);
  const visible = useMemo(() => {
    const byId = new Map(active.map(camera => [camera.id, camera]));
    const sorted = order.map(id => byId.get(id)).filter((camera): camera is Camera => Boolean(camera));
    return deferred ? sorted.filter(camera => camera.name.toLowerCase().includes(deferred) || (camera.camera_ip ?? "").includes(deferred)) : sorted;
  }, [active, order, deferred]);

  return (
    <div className="browser-section">
      <input className="browser-search" type="search" placeholder="Search name or address" aria-label="Search cameras by name or address"
             value={query} onChange={event => setQuery(event.target.value)} />
      <div className="browser-meta" aria-live="polite">
        {selected.length}/{capacity} in view{deferred ? ` · ${visible.length} match${visible.length === 1 ? "" : "es"}` : ""}
      </div>
      <div className="browser-list">
        {loading ? <p className="muted browser-empty">Loading cameras…</p> : null}
        {error ? <p className="notice browser-empty" data-tone="bad" role="alert">{error}</p> : null}
        {!loading && !error && !visible.length ? <p className="muted browser-empty">{deferred ? "No cameras match." : "No cameras have been added yet."}</p> : null}
        <ul role="list">
          {visible.map(camera => (
            <CameraRow key={camera.id} id={camera.id} name={camera.name} address={camera.camera_ip ?? null} health={camera.recording_health.state}
                       detail={camera.recording_health.detail} selected={chosen.has(camera.id)} onToggle={toggleRow} onReplaceActive={onReplaceActive ? replaceRow : undefined} />
          ))}
        </ul>
      </div>
    </div>
  );
}

interface RowProps {
  id: string;
  name: string;
  address: string | null;
  health: Camera["recording_health"]["state"];
  detail: string;
  selected: boolean;
  onToggle: (id: string) => void;
  onReplaceActive?: (id: string) => void;
}

/** One camera's row. Drawn again only when what it shows changes (#539): a refresh that brings the same values leaves every row alone. */
const CameraRow = memo(function CameraRow({ id, name, address, health, detail, selected, onToggle, onReplaceActive }: RowProps) {
  function onKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Enter" && event.shiftKey && onReplaceActive) {
      event.preventDefault();
      onReplaceActive(id);
    }
  }
  return (
    <li>
      <button type="button" className="browser-row" aria-pressed={selected} onClick={() => onToggle(id)} onKeyDown={onKey} title={detail}
              draggable onDragStart={event => event.dataTransfer.setData(CAMERA_DRAG_TYPE, id)}>
        <span className="browser-check" aria-hidden="true">{selected ? "✓" : ""}</span>
        <span className="status-dot" data-tone={HEALTH_TONE[health]} aria-hidden="true" />
        <span className="browser-name">{name}</span>
        {address ? <span className="browser-ip">{address}</span> : null}
        <span className="visually-hidden">{detail}</span>
      </button>
    </li>
  );
});

/** The collapsible browser frame shared by workspaces. */
export function BrowserFrame({ onCollapse, children }: { onCollapse: () => void; children: ReactNode }) {
  return (
    <aside className="browser" aria-label="Camera browser">
      <button type="button" className="button icon-button browser-collapse" data-variant="ghost" onClick={onCollapse}
              aria-label="Hide camera browser" title="Hide camera browser">⟨</button>
      {children}
    </aside>
  );
}
