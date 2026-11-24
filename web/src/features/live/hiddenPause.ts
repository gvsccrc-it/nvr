import { useEffect, useState } from "react";

/**
 * A Live page hidden for a while gives its streams back (#533).
 *
 * A hidden tab or a minimised window kept every tile's stream going for ever: its socket, the server's stream (and FFmpeg, where the
 * picture is converted or masked) and the browser's buffer, counting against everyone's live-view places. After ``HIDDEN_GRACE_MS`` hidden
 * (the owner's decision: 2 minutes) the tiles stop, keep their last picture and say so; they start again when the page is shown. The video
 * wall, a tile someone is listening to, and a shared view an administrator marked to keep streaming are left alone (the callers' part).
 */
export const HIDDEN_GRACE_MS = 2 * 60_000;

export interface Visibility {
  hidden: () => boolean;
  listen: (onChange: () => void) => () => void;
  setTimeout: (callback: () => void, ms: number) => number;
  clearTimeout: (handle: number) => void;
}

const BROWSER: Visibility = {
  hidden: () => document.hidden,
  listen: onChange => {
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  },
  setTimeout: (callback, ms) => window.setTimeout(callback, ms),
  clearTimeout: handle => window.clearTimeout(handle),
};

/** Calls ``onChange(true)`` once the page has been hidden for ``graceMs`` without a break, and ``onChange(false)`` as soon as it is shown. */
export function watchHiddenTooLong(onChange: (tooLong: boolean) => void, graceMs = HIDDEN_GRACE_MS, page: Visibility = BROWSER): () => void {
  let timer: number | null = null;
  let tooLong = false;
  const set = (value: boolean) => {
    if (value !== tooLong) {
      tooLong = value;
      onChange(value);
    }
  };
  const check = () => {
    if (page.hidden()) {
      if (timer === null && !tooLong) timer = page.setTimeout(() => { timer = null; set(true); }, graceMs);
    } else {
      if (timer !== null) page.clearTimeout(timer);
      timer = null;
      set(false);
    }
  };
  const stop = page.listen(check);
  check();
  return () => {
    stop();
    if (timer !== null) page.clearTimeout(timer);
  };
}

/** Whether the page has been hidden for longer than the grace period; always false when ``enabled`` is false. */
export function useHiddenTooLong(enabled: boolean): boolean {
  const [tooLong, setTooLong] = useState(false);
  useEffect(() => {
    if (!enabled) {
      setTooLong(false);
      return undefined;
    }
    return watchHiddenTooLong(setTooLong);
  }, [enabled]);
  return enabled && tooLong;
}
