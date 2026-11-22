/**
 * Reads the NVR's compatibility-mode live stream: JPEG pictures one after another in one HTTP answer (multipart/x-mixed-replace).
 *
 * A browser can show that stream by itself in an <img>, but then tells the page nothing: not that a picture arrived, and not that
 * the stream ended. An image whose stream has ended keeps its last picture, which on a camera system is a frozen picture that looks
 * live (#534). Read here instead, every picture is an event and the end of the stream is seen.
 */

const HEADER_END = [13, 10, 13, 10]; // \r\n\r\n
const MOST_HEADER_BYTES = 4096;

function indexOf(bytes: Uint8Array, pattern: number[], from: number): number {
  outer: for (let at = from; at <= bytes.length - pattern.length; at += 1) {
    for (let offset = 0; offset < pattern.length; offset += 1) if (bytes[at + offset] !== pattern[offset]) continue outer;
    return at;
  }
  return -1;
}

/**
 * Give it the stream's bytes as they arrive; it gives back each whole picture. Every part the server writes carries its length
 * (`--frame`, `Content-Type`, `Content-Length`, a blank line, the JPEG), which is what is read. Throws on bytes that are not that.
 */
export function pictureSplitter(): (chunk: Uint8Array) => Uint8Array[] {
  let pending = new Uint8Array(0);
  return chunk => {
    const joined = new Uint8Array(pending.length + chunk.length);
    joined.set(pending);
    joined.set(chunk, pending.length);
    const pictures: Uint8Array[] = [];
    let at = 0;
    for (;;) {
      const headerEnd = indexOf(joined, HEADER_END, at);
      if (headerEnd < 0) {
        if (joined.length - at > MOST_HEADER_BYTES) throw new Error("The live picture stream is not in the expected form");
        break;
      }
      const header = new TextDecoder("latin1").decode(joined.subarray(at, headerEnd));
      const length = Number(/content-length:\s*(\d+)/i.exec(header)?.[1]);
      if (!Number.isSafeInteger(length) || length <= 0) throw new Error("A live picture arrived without its length");
      const start = headerEnd + HEADER_END.length;
      if (joined.length < start + length) break;
      pictures.push(joined.slice(start, start + length));
      at = start + length;
    }
    pending = joined.slice(at);
    return pictures;
  };
}
