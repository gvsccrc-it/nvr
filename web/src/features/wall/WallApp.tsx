import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useReducer, useState, type FormEvent } from "react";
import { api, ApiError, onSessionExpired } from "@/api/client";
import { dropSessionData, useCameras, useDisplaySettings, useLiveViews } from "@/api/queries";
import type { Camera, SavedView } from "@/api/types";
import { popInSummary } from "@/features/live/AlarmBanner";
import { useViewingPrefs } from "@/features/live/bandwidth";
import { useAlarmPopIns } from "@/features/live/popIns";
import { autoGrid, MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { formatSiteTime } from "@/lib/zonedTime";
import { INITIAL_WALL, MIN_ALARM_SECONDS, MIN_DWELL_SECONDS, MAX_DWELL_SECONDS, parseWallAddress, wallReducer, wallSearch, type WallAddress } from "./rotation";
import { WallTile } from "./WallTile";

interface WallSession { name: string; scopes: string[]; interface?: string }

const sessionKey = ["wall", "session"] as const;
/** How often a running wall checks its session and its views again. Nobody is there to reload it, and a kiosk never gets a focus event. */
const BACKGROUND_MS = 60_000;
/** The wait before asking an NVR that cannot be reached again: 2 s, 4 s, 8 s… up to half a minute. */
const waitBeforeRetry = (attempt: number) => Math.min(30_000, 2_000 * 2 ** attempt);

function Waiting() {
  return <main className="wall-setup"><p className="wall-panel" role="status"><span className="spinner" /> <span>Waiting for the NVR…</span></p></main>;
}

/**
 * Video wall mode at /wall: full screen video for a monitor, signed in once with an API
 * token limited to read and media. It rotates shared views and pops alarms in; there are no menus.
 */
export function WallApp() {
  const client = useQueryClient();
  // Only "not signed in" (401) is an answer. Anything else (no network, a 503 while the NVR starts) is tried again, for ever, with a
  // growing wait: a display that starts before the NVR after a power cut must not end on a form asking for a token shown once (#535).
  const session = useQuery({
    queryKey: sessionKey,
    queryFn: async ({ signal }): Promise<WallSession | null> => {
      try {
        return await api<WallSession>("/api/wall/session", { signal, quietUnauthorized: true });
      } catch (error) {
        if (error instanceof ApiError && error.unauthorized) return null;
        throw error;
      }
    },
    retry: true,
    retryDelay: waitBeforeRetry,
    refetchInterval: BACKGROUND_MS,
    refetchIntervalInBackground: true,
  });
  // The interface this page was loaded with, as the server named it then. A different name later means the NVR was upgraded under a
  // running wall, which would otherwise keep running the old interface against the new server.
  const serving = session.data?.interface || null;
  const [loadedWith, setLoadedWith] = useState<string | null>(null);
  useEffect(() => {
    if (serving && !loadedWith) setLoadedWith(serving);
  }, [serving, loadedWith]);
  const outdated = Boolean(serving && loadedWith && serving !== loadedWith);
  const [address, setAddress] = useState<WallAddress>(() => parseWallAddress(window.location.search));

  useEffect(() => {
    document.documentElement.dataset.theme = "dark";
    document.title = "Video wall";
  }, []);
  // A revoked or expired token ends the session: go back to the token form.
  useEffect(() => onSessionExpired(() => dropSessionData(client, sessionKey, [])), [client]);  // The wall's views and cameras go with it (#538)

  if (session.isPending) return session.failureCount ? <Waiting /> : <div className="app-loading" role="status"><span className="spinner" /> <span>Loading…</span></div>;
  if (!session.data) return <WallSignIn />;
  if (!address.views.length) {
    return <WallSetup onStart={next => {
      window.history.replaceState(null, "", `${window.location.pathname}${wallSearch(next)}`);
      setAddress(next);
      void document.documentElement.requestFullscreen?.().catch(() => undefined);
    }} />;
  }
  return <WallScreen address={address} outdated={outdated} />;
}

function WallSignIn() {
  const client = useQueryClient();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const signedIn = await api<WallSession>("/api/wall/session", { method: "POST", body: { token }, quietUnauthorized: true });
      await client.cancelQueries({ queryKey: sessionKey });  // A background check asked before the sign-in must not answer "signed out" after it
      client.setQueryData(sessionKey, signedIn);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "The video wall could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="wall-setup">
      <form className="wall-panel" aria-label="Video wall sign-in" onSubmit={event => void submit(event)}>
        <h1>Video wall</h1>
        <p className="muted">Sign this screen in with an API token that has only the <strong>read</strong> and <strong>media</strong> scopes. An administrator creates one in Settings → Users &amp; access.</p>
        <label className="field"><span>API token</span>
          <input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} required />
        </label>
        {error ? <p className="field-error" role="alert">{error}</p> : null}
        <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={busy || !token.trim()}>{busy ? "Signing in…" : "Sign in"}</button></div>
      </form>
    </main>
  );
}

function WallSetup({ onStart }: { onStart: (address: WallAddress) => void }) {
  const views = useLiveViews();
  const shared = (views.data ?? []).filter(view => view.scope === "shared");
  const [chosen, setChosen] = useState<string[]>([]);
  const [dwell, setDwell] = useState(30);
  return (
    <main className="wall-setup">
      <form className="wall-panel" aria-label="Video wall views" onSubmit={event => { event.preventDefault(); onStart({ views: chosen, dwellSeconds: dwell }); }}>
        <h1>Choose what this wall shows</h1>
        {views.isPending ? <p className="muted">Loading shared views…</p> : shared.length ? (
          <fieldset className="plain">
            <legend className="field-heading">Shared views, shown in this order</legend>
            <ul className="choice-list">
              {shared.map(view => (
                <li key={view.name}><label><input type="checkbox" checked={chosen.includes(view.name)}
                                                  onChange={event => setChosen(event.target.checked ? [...chosen, view.name] : chosen.filter(name => name !== view.name))} /> {view.name}</label></li>
              ))}
            </ul>
          </fieldset>
        ) : <p className="notice" data-tone="warn">There are no shared views yet. An administrator saves one from Live.</p>}
        <label className="field"><span>Seconds on each view</span>
          <input type="number" min={MIN_DWELL_SECONDS} max={MAX_DWELL_SECONDS} value={dwell} onChange={event => setDwell(Number(event.target.value))} />
        </label>
        <p className="muted field-hint">Bookmark the address after starting to open this wall straight to its views.</p>
        <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={!chosen.length || !(dwell >= MIN_DWELL_SECONDS)}>Start wall</button></div>
      </form>
    </main>
  );
}

function members(view: SavedView): string[] {
  return view.layout_mode === "manual" ? view.tile_camera_ids.filter((id): id is string => id !== null) : view.camera_ids;
}

function WallScreen({ address, outdated }: { address: WallAddress; outdated: boolean }) {
  const mainOnly = useViewingPrefs(prefs => prefs.mainOnly);
  const cameras = useCameras();
  const views = useLiveViews(BACKGROUND_MS);
  const display = useDisplaySettings();
  const [state, dispatch] = useReducer(wallReducer, INITIAL_WALL);
  const byName = useMemo(() => new Map((views.data ?? []).filter(view => view.scope === "shared").map(view => [view.name, view])), [views.data]);
  const rotation = address.views.map(name => byName.get(name)).filter((view): view is SavedView => Boolean(view));
  const viewCount = rotation.length;

  useAlarmPopIns(true, alarm => dispatch({ type: "alarm", alarm }), true); // The wall is watched though nobody touches it: it polls when hidden too
  useEffect(() => {
    if (state.alarm || viewCount < 2) return;
    const timer = window.setTimeout(() => dispatch({ type: "rotate", viewCount }), address.dwellSeconds * 1000);
    return () => window.clearTimeout(timer);
  }, [state.alarm, state.viewIndex, viewCount, address.dwellSeconds]);
  useEffect(() => {
    if (!state.alarm) return;
    const timer = window.setTimeout(() => dispatch({ type: "alarmDone" }), Math.max(address.dwellSeconds, MIN_ALARM_SECONDS) * 1000);
    return () => window.clearTimeout(timer);
  }, [state.alarm, address.dwellSeconds]);

  // A newer interface is loaded at a quiet moment: never over an alarm that is showing.
  useEffect(() => {
    if (outdated && !state.alarm) window.location.reload();
  }, [outdated, state.alarm]);

  const available = useMemo(() => new Map((cameras.data ?? []).filter(camera => !camera.archived_at).map(camera => [camera.id, camera])), [cameras.data]);
  const view = rotation[state.viewIndex % Math.max(1, viewCount)];
  const ids = state.alarm ? state.alarm.camera_ids : view ? members(view) : [];
  const shown = [...new Set(ids)].map(id => available.get(id)).filter((camera): camera is Camera => Boolean(camera)).slice(0, MAX_DISPLAYED_CAMERAS);
  const { columns, rows } = autoGrid(shown.length);
  const time = state.alarm && display.data ? formatSiteTime(Date.parse(state.alarm.occurred_at), display.data, true) : "";

  // Nothing to draw yet because the first fetch failed: say so, not an empty grid. Both are fetched again by themselves.
  if ((!views.data && views.isError) || (!cameras.data && cameras.isError)) return <Waiting />;
  if (views.isSuccess && !viewCount) {
    return <main className="wall-setup"><p className="notice wall-panel" data-tone="warn" role="alert">None of this wall's views exist any more. Open /wall again to choose views.</p></main>;
  }
  return (
    <main className="wall" aria-label="Video wall" data-alarm={state.alarm ? "true" : undefined}>
      <header className="wall-caption">
        {state.alarm ? (
          <p className="wall-alarm" role="alert"><span className="status-dot" data-tone="bad" aria-hidden="true" /> <strong>Alarm</strong> {popInSummary(state.alarm, time)}
            {state.queue.length ? <span className="muted"> · {state.queue.length} more waiting</span> : null}</p>
        ) : <p className="wall-view">{view?.name ?? ""}{viewCount > 1 ? <span className="muted"> · {state.viewIndex % viewCount + 1} of {viewCount}</span> : null}</p>}
      </header>
      <div className="wall-grid" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))` }}>
        {shown.map(camera => (
          <WallTile key={camera.id} camera={camera} highlighted={state.alarm?.camera_id === camera.id}
                    quality={shown.length > 1 && camera.live_substream_available && !mainOnly ? "low" : "high"} />
        ))}
      </div>
    </main>
  );
}
