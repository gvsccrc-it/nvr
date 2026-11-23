import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import { failingDelay } from "@/api/polling";
import type { PopIn } from "./store";

const POLL_MS = 2_000;

/**
 * Follow alarms that pop into Live, from the moment this starts: alarms raised
 * earlier are never replayed. Each new alarm is handed over once, oldest first.
 *
 * Every 2 seconds while the page is visible; not at all while it is hidden, unless ``inBackground`` (the video wall, which is watched
 * though nobody touches it), and it catches up from its cursor when the page comes back. A failing poll slows down as every other poll
 * does, up to a minute between tries (#536): it used to try every 2 seconds whatever happened, and kept on in hidden tabs.
 */
export function useAlarmPopIns(enabled: boolean, onAlarm: (alarm: PopIn) => void, inBackground = false) {
  const handler = useRef(onAlarm);
  handler.current = onAlarm;

  useEffect(() => {
    if (!enabled) return;
    let cursor: string | null = null;
    let stopped = false;
    let failures = 0;
    let timer = 0;
    let controller: AbortController | null = null;
    const schedule = (delay: number) => {
      window.clearTimeout(timer);
      if (!stopped) timer = window.setTimeout(poll, delay);
    };
    const poll = async () => {
      if (stopped) return;
      if (document.hidden && !inBackground) return; // Picked up again when the page is shown
      controller = new AbortController();
      try {
        const query = cursor === null ? "" : `?after=${encodeURIComponent(cursor)}`;
        const body = await api<{ cursor: string; alarms: PopIn[] }>(`/api/live/alarm-pop-ins${query}`, { signal: controller.signal });
        if (stopped) return;
        cursor = body.cursor;
        failures = 0;
        body.alarms.forEach(alarm => handler.current(alarm));
      } catch {
        if (stopped) return;
        failures += 1; // Tried again later; a missed poll never loses an alarm, the cursor holds
      }
      schedule(failingDelay(POLL_MS, failures));
    };
    const onVisibility = () => {
      if (!document.hidden) schedule(0);
    };
    document.addEventListener("visibilitychange", onVisibility);
    void poll();
    return () => {
      stopped = true;
      controller?.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, inBackground]);
}
