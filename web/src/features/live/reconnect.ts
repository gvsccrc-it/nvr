/**
 * How long a live tile waits before reconnecting (#539).
 *
 * The waits were a fixed list, so after a server restart every tile of every person reconnected at the same instants (+1 s, +3 s, +7 s...)
 * while the server was still starting. Each wait is now somewhere between half the step and the whole step, chosen at random, so tiles
 * spread out; the longest is still 30 s.
 */
export const RECONNECT_STEPS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000] as const;

export function reconnectDelay(attempt: number, random: () => number = Math.random): number {
  const step = RECONNECT_STEPS_MS[Math.min(Math.max(0, Math.floor(attempt) || 0), RECONNECT_STEPS_MS.length - 1)] ?? 30_000;
  const share = Math.min(Math.max(random(), 0), 1);
  return Math.round(step * (0.5 + 0.5 * share));
}
