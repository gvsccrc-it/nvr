/**
 * The order cameras are listed in: by name, numbers in order ("Camera 2" before "Camera 10"), case and accents ignored, then by id (#539).
 *
 * One comparer for every comparison: ``localeCompare`` with options set one up for each of the roughly 10,000 comparisons sorting
 * 1,000 cameras takes. And the order is worked out again only when a name changes (``namesKey``), not on every 15-second refresh.
 */
interface Named { id: string; name: string }

const BY_NAME = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
const BY_ID = new Intl.Collator(); // What a plain localeCompare does, as before

export function compareCameras(a: Named, b: Named): number {
  return BY_NAME.compare(a.name, b.name) || BY_ID.compare(a.id, b.id);
}

/** A key that changes when, and only when, the cameras or their names do: what the order depends on. */
export function namesKey(cameras: readonly Named[]): string {
  let key = "";
  for (const camera of cameras) key += `${camera.id}\u0000${camera.name}\u0001`;
  return key;
}

/** The ids in order. */
export function orderOf(cameras: readonly Named[]): string[] {
  return [...cameras].sort(compareCameras).map(camera => camera.id);
}
