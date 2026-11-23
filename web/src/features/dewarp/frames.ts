/**
 * When a dewarped tile paints (#539).
 *
 * It painted on every animation frame while live, uploading the whole video frame again each time: a 15 to 25 frames-a-second camera was
 * uploaded and redrawn 60 times a second (120 on fast screens), per tile, even with no new picture. A video now paints when it has a new
 * frame (its ``requestVideoFrameCallback``). A source with no such callback (the compatibility mode's picture, a browser without it) still
 * paints on every animation frame, and so does a video that has produced nothing for ``WAKE_MS``, so a source that changes or stops is
 * never waited on for ever.
 */
export const WAKE_MS = 500;

interface VideoFrames {
  requestVideoFrameCallback: (callback: () => void) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
}

export interface Clock {
  animationFrame: (callback: () => void) => number;
  cancelAnimationFrame: (handle: number) => void;
  setTimeout: (callback: () => void, ms: number) => number;
  clearTimeout: (handle: number) => void;
}

const BROWSER: Clock = {
  animationFrame: callback => requestAnimationFrame(callback),
  cancelAnimationFrame: handle => cancelAnimationFrame(handle),
  setTimeout: (callback, ms) => window.setTimeout(callback, ms),
  clearTimeout: handle => window.clearTimeout(handle),
};

function hasVideoFrames(source: unknown): source is VideoFrames {
  return typeof source === "object" && source !== null && typeof (source as Partial<VideoFrames>).requestVideoFrameCallback === "function";
}

/** Paint now, then again each time the source has something new. Returns the function that stops it. */
export function paintFrames(source: () => unknown, paint: () => void, clock: Clock = BROWSER): () => void {
  let stopped = false;
  let cancel: () => void = () => undefined;
  const tick = () => {
    cancel();
    if (stopped) return;
    paint();
    const picture = source();
    if (hasVideoFrames(picture)) {
      const frame = picture.requestVideoFrameCallback(tick);
      const wake = clock.setTimeout(tick, WAKE_MS);
      cancel = () => {
        picture.cancelVideoFrameCallback?.(frame);
        clock.clearTimeout(wake);
      };
    } else {
      const frame = clock.animationFrame(tick);
      cancel = () => clock.cancelAnimationFrame(frame);
    }
  };
  tick();
  return () => {
    stopped = true;
    cancel();
  };
}
