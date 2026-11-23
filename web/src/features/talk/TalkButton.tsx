import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { api } from "@/api/client";
import { useMe } from "@/api/queries";
import { Talker, type TalkOutcome, type TalkPhase } from "./talker";
import { pollEvery } from "@/api/polling";

const TALK_REFRESH_MS = 5_000;

interface TalkCameras { cameras: Record<string, { talking: { username: string; since: string } | null }> }

const REASON: Record<string, string> = { idle: "Talk stopped: no sound for a while.", limit: "Talk stopped: the time limit was reached.", camera_failed: "The camera stopped answering.", bad_audio: "Talk stopped: bad audio." };

const talkQuery = {
  queryKey: ["talk"] as const,
  queryFn: ({ signal }: { signal: AbortSignal }) => api<TalkCameras>("/api/talk", { signal }).then(result => result.cameras),
  staleTime: 30_000,
};

/** The cameras this person may talk through, and who is talking through each: read by each tile's button, which does not poll it itself. */
export function useTalkCameras() {
  return useQuery(talkQuery);
}

/**
 * Keeps "who is talking" fresh while the Live page shows a tile that can talk: one poll for the page, every 5 seconds (the owner's decision on
 * #536: the indicator may be that old), none while the page is hidden. Each tile's button used to poll it every 3 seconds, so a page with
 * several such tiles asked several times over, and whenever any camera at all could talk.
 */
export function useTalkPolling(shownCameraIds: readonly string[]) {
  useQuery({
    ...talkQuery,
    refetchInterval: query => (query.state.data && shownCameraIds.some(id => id in query.state.data!) ? pollEvery(TALK_REFRESH_MS)(query) : false),
  });
}

function sentence(outcome: TalkOutcome): string | null {
  if (outcome.kind === "busy") return `${outcome.talker} is talking through this camera.`;
  if (outcome.kind === "refused" || outcome.kind === "no_microphone") return outcome.detail;
  if (outcome.kind === "ended") return REASON[outcome.reason] ?? null;
  return null;
}

/** Hold to talk through this camera's speaker, by pointer or the T key while the tile has focus. Shown only where talking is on for the signed-in role. */
export function TalkButton({ cameraId, name, keyTarget }: { cameraId: string; name: string; keyTarget?: RefObject<HTMLElement | null> }) {
  const talk = useTalkCameras();
  const me = useMe().data;
  const [phase, setPhase] = useState<TalkPhase>("idle");
  const [note, setNote] = useState<string | null>(null);
  const holding = useRef(false);
  const talker = useMemo(() => new Talker(setPhase, outcome => { holding.current = false; setNote(sentence(outcome)); }), []);
  const entry = talk.data?.[cameraId];
  const holder = entry?.talking ?? null;
  const mine = holder !== null && holder.username === me?.username;
  const others = holder !== null && !mine;

  const down = () => {
    if (holding.current || others) return;
    holding.current = true;
    setNote(null);
    void talker.press(cameraId);
  };
  const up = () => {
    if (!holding.current) return;
    holding.current = false;
    talker.release();
    void talk.refetch();
  };
  useEffect(() => {
    // Leaving the page or hiding the tab lets go, whatever the pointer or key is doing.
    const letGo = () => { if (document.visibilityState === "hidden") up(); };
    window.addEventListener("pagehide", up);
    document.addEventListener("visibilitychange", letGo);
    return () => { window.removeEventListener("pagehide", up); document.removeEventListener("visibilitychange", letGo); talker.release(); };
  }, [talker]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const target = keyTarget?.current;
    if (!target || !entry) return;
    const onDown = (event: KeyboardEvent) => { if ((event.key === "t" || event.key === "T") && !event.repeat && !event.altKey && !event.ctrlKey && !event.metaKey) { event.preventDefault(); down(); } };
    const onUp = (event: KeyboardEvent) => { if (event.key === "t" || event.key === "T") up(); };
    target.addEventListener("keydown", onDown);
    target.addEventListener("keyup", onUp);
    target.addEventListener("blur", up, true);
    return () => { target.removeEventListener("keydown", onDown); target.removeEventListener("keyup", onUp); target.removeEventListener("blur", up, true); };
  }); // eslint-disable-line react-hooks/exhaustive-deps
  if (!entry) return null;
  const label = phase === "talking" ? "Talking…" : phase === "connecting" ? "Connecting…" : others ? `${holder.username} is talking` : "Hold to talk";
  return (
    <>
      <button type="button" className="button tile-action tile-talk" data-phase={phase} data-busy={others || undefined} aria-label={`Talk through ${name}`} title="Hold to talk (or hold T)" disabled={others}
              onPointerDown={event => { event.preventDefault(); (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId); down(); }}
              onPointerUp={up} onPointerCancel={up} onLostPointerCapture={up} onContextMenu={event => event.preventDefault()}
              onKeyDown={event => { if ((event.key === " " || event.key === "Enter") && !event.repeat) { event.preventDefault(); down(); } }}
              onKeyUp={event => { if (event.key === " " || event.key === "Enter") up(); }}>
        <span aria-hidden="true">🎙</span> <span className="tile-talk-label">{label}</span>
      </button>
      <span className="tile-talk-status" role="status" aria-live="polite">{mine || phase === "talking" ? "You are talking" : others ? `${holder.username} is talking` : note ?? ""}</span>
    </>
  );
}
