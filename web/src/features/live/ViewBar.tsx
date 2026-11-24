import * as Menu from "@radix-ui/react-dropdown-menu";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/api/client";
import { useDeleteView, useLiveViews, useSaveView, viewKeys } from "@/api/queries";
import type { Role } from "@/api/types";
import { toSaved, type ManualSize } from "./arrangement";
import { QUALITY_INTERVALS, lowDataActive, useViewingPrefs, type LowDataSetting } from "./bandwidth";
import { SaveAsDialog } from "./dialogs";
import { useCameraIds, useDirty, useLiveStore } from "./store";

const LAYOUTS: { label: string; size: ManualSize }[] = [
  { label: "1 × 1", size: 1 },
  { label: "2 × 2", size: 4 },
  { label: "3 × 3", size: 9 },
  { label: "4 × 4", size: 16 },
];

export function ViewBar({ role }: { role: Role }) {
  const arrangement = useLiveStore(store => store.arrangement);
  const view = useLiveStore(store => store.view);
  const dewarp = useLiveStore(store => store.dewarp);
  const { markSaved, setLayout, load } = useLiveStore.getState();
  const dirty = useDirty();
  const cameraIds = useCameraIds();
  const views = useLiveViews();
  const save = useSaveView();
  const remove = useDeleteView();
  const [saveAs, setSaveAs] = useState(false);
  const prefs = useViewingPrefs();
  const canSave = view !== null && (view.scope === "personal" || role === "administrator");
  const error = save.error ?? remove.error;

  return (
    <div className="view-bar">
      <span className="view-name" data-dirty={dirty}>
        {view ? (
          <>
            {view.name} <span className="muted">· {view.scope === "shared" ? "Shared view" : "My view"}</span>
            {dirty ? <span className="view-dirty" title="Changed since loaded"> · Unsaved changes</span> : null}
          </>
        ) : (
          <span className="muted">Unsaved arrangement</span>
        )}
      </span>
      {canSave && dirty ? (
        <button type="button" className="button" disabled={save.isPending}
                onClick={() => save.mutate({ name: view.name, scope: view.scope, layout: toSaved(arrangement, dewarp) }, { onSuccess: () => markSaved(view) })}>
          Save
        </button>
      ) : null}
      {view?.scope === "shared" && role === "administrator" ? <KeepStreaming name={view.name} /> : null}
      <button type="button" className="button" disabled={!cameraIds.length} onClick={() => setSaveAs(true)}>Save as…</button>
      <Menu.Root>
        <Menu.Trigger className="button" aria-label="Layout">Layout: {arrangement.mode === "auto" ? "Auto" : `${Math.sqrt(arrangement.slots.length)} × ${Math.sqrt(arrangement.slots.length)}`}</Menu.Trigger>
        <Menu.Portal>
          <Menu.Content className="menu" align="end" sideOffset={4}>
            <Menu.RadioGroup value={arrangement.mode === "auto" ? "auto" : String(arrangement.slots.length)}
                             onValueChange={value => setLayout(value === "auto" ? "auto" : (Number(value) as ManualSize))}>
              <Menu.RadioItem className="menu-item" value="auto"><Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>Auto</Menu.RadioItem>
              <Menu.Separator className="menu-separator" />
              <Menu.Label className="menu-label">Manual layout</Menu.Label>
              {LAYOUTS.map(layout => (
                <Menu.RadioItem key={layout.size} className="menu-item" value={String(layout.size)}>
                  <Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>{layout.label}
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>
      <Menu.Root>
        <Menu.Trigger className="button" aria-label="Data use">Data: {lowDataActive(prefs.lowData, prefs.mainOnly) ? "Low" : prefs.mainOnly ? "Main streams" : "Normal"}</Menu.Trigger>
        <Menu.Portal>
          <Menu.Content className="menu" align="end" sideOffset={4}>
            <Menu.Label className="menu-label">Data use</Menu.Label>
            <Menu.RadioGroup value={prefs.lowData} onValueChange={value => prefs.set({ lowData: value as LowDataSetting })}>
              <Menu.RadioItem className="menu-item" value="auto"><Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>Automatic (low on a cellular installed app)</Menu.RadioItem>
              <Menu.RadioItem className="menu-item" value="on"><Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>Low-data mode</Menu.RadioItem>
              <Menu.RadioItem className="menu-item" value="off"><Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>Normal</Menu.RadioItem>
            </Menu.RadioGroup>
            <Menu.Separator className="menu-separator" />
            <Menu.CheckboxItem className="menu-item" checked={prefs.mainOnly} onCheckedChange={checked => prefs.set({ mainOnly: checked })}>
              <Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>Main streams only (full quality in every tile, more data{prefs.lowData === "on" ? "; Low-data mode, chosen above, still wins" : ""})
            </Menu.CheckboxItem>
            <Menu.Separator className="menu-separator" />
            <Menu.Label className="menu-label">Auto quality changes at most every</Menu.Label>
            <Menu.RadioGroup value={String(prefs.intervalSeconds)} onValueChange={value => prefs.set({ intervalSeconds: Number(value) })}>
              {QUALITY_INTERVALS.map(seconds => (
                <Menu.RadioItem key={seconds} className="menu-item" value={String(seconds)}>
                  <Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>{seconds < 60 ? `${seconds} seconds` : `${seconds / 60} minute${seconds > 60 ? "s" : ""}`}
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>
      {canSave ? (
        <button type="button" className="button" data-variant="danger" disabled={remove.isPending}
                onClick={() => {
                  if (window.confirm(`Delete the view “${view.name}”? Cameras stay on screen.`)) {
                    remove.mutate(view, { onSuccess: () => load(arrangement, null, dewarp) });
                  }
                }}>
          Delete view
        </button>
      ) : null}
      {error ? <span className="view-error" role="alert">{error instanceof ApiError ? error.detail : "The view could not be changed."}</span> : null}
      <SaveAsDialog open={saveAs} onOpenChange={setSaveAs} arrangement={arrangement} dewarp={dewarp} role={role} existing={views.data ?? []}
                    onSaved={saved => markSaved(saved)} />
    </div>
  );
}

/** A shared view's "keep streaming when hidden" switch, for a control-room screen (#533): administrators only, off by default. */
function KeepStreaming({ name }: { name: string }) {
  const views = useLiveViews();
  const client = useQueryClient();
  const current = Boolean(views.data?.find(item => item.scope === "shared" && item.name === name)?.keep_streaming);
  const change = useMutation({
    mutationFn: (keep: boolean) => api(`/api/live/shared-layouts/${encodeURIComponent(name)}/keep-streaming`, { method: "PUT", body: { keep_streaming: keep } }),
    onSuccess: () => client.invalidateQueries({ queryKey: viewKeys.views }),
  });
  return (
    <label className="check" title="Otherwise a Live page hidden for 2 minutes gives its streams back until it is shown again">
      <input type="checkbox" checked={current} disabled={change.isPending || !views.data} onChange={event => change.mutate(event.target.checked)} />
      Keep streaming when hidden
    </label>
  );
}
