/**
 * Plays one camera's live stream into a <video> element.
 *
 * Fragmented MP4 arrives over a WebSocket and is appended to Media Source
 * Extensions. Freshness is measured from frames the browser actually decoded,
 * never from the connection being open. Codecs the browser cannot decode switch
 * to the server's MJPEG fallback, labelled as such: the player reads that stream
 * of pictures itself (pictureStream.ts) and puts each into the tile's <img>, so a
 * picture arriving, and the stream ending, are things it knows about.
 *
 * Nothing here waits for ever. A connection that has given nothing for a while is
 * dropped and asked for again with the same growing wait in either mode (#534).
 */
import { withUnmasked } from "@/features/privacy/store";
import { mseHevc } from "@/lib/codecs";
import { pictureSplitter } from "./pictureStream";
import { reconnectDelay } from "./reconnect";

export type Phase = "connecting" | "live" | "stale" | "reconnecting" | "offline" | "error" | "paused";

export interface PlayerSnapshot {
  phase: Phase;
  transport: "mse" | "mjpeg" | null;
  /** The camera sends H.265 and the NVR converts it to H.264 for this browser. */
  converted: boolean;
  detail: string | null;
  lastFrameAt: number | null;
  audioAvailable: boolean;
  listening: boolean;
  profile: "main" | "sub" | null;
  /** How long the first picture took, from asking for the camera to a frame on screen (ms); null until it has appeared. */
  firstFrameMs: number | null;
}

interface Options {
  cameraId: string;
  quality: "low" | "high";
  video: HTMLVideoElement;
  onChange: (snapshot: PlayerSnapshot) => void;
  /** Compatibility mode began (the stream's address) or ended (null): show the <img> that `picture` returns in place of the video. */
  onFallback: (url: string | null) => void;
  /** The <img> compatibility-mode pictures are put into, once it is on the page. */
  picture?: () => HTMLImageElement | null;
  /** Measures for Auto quality: a video fragment arrived, and playback stopped to wait for data. */
  onFragment?: (at: number) => void;
  onStall?: (at: number) => void;
}

const STALE_AFTER_MS = 4_000;
const OFFLINE_AFTER_MS = 15_000;
const NO_RETRY_CODES = new Set([4401, 4403, 4404, 4410]);
const NO_RETRY_STATUSES = new Set([401, 403, 404, 410]);
/** A connection that has given nothing for this long is dropped and asked for again: before its first data (a converted stream takes a
 * while to start, and the server gives up on a silent camera after 15 s), and once data has flowed. */
const SILENT_BEFORE_DATA_MS = 20_000;
const SILENT_AFTER_DATA_MS = 10_000;
const COMPATIBILITY = "Compatibility mode: this camera's video is converted for the browser.";

export class LivePlayer {
  private socket: WebSocket | null = null;
  private source: MediaSource | null = null;
  private buffer: SourceBuffer | null = null;
  private queue: ArrayBuffer[] = [];
  private objectUrl: string | null = null;
  private attempt = 0;
  private retryTimer: number | null = null;
  private healthTimer: number | null = null;
  private frameCallback: number | null = null;
  private disconnectedAt: number | null = null;
  private stopped = false;
  /** The compatibility-mode stream being read, and the address of the picture now in the <img>. */
  private pictures: AbortController | null = null;
  private pictureUrl: string | null = null;
  /** When this connection was asked for, when it last gave data, whether it has, and whether a picture from it has been shown. */
  private askedAt = Date.now();
  private heardAt = Date.now();
  private heard = false;
  private shown = false;
  /** Whether this connection asked for H.265 as it is; if the browser then cannot play it, the next asks for H.264 instead. */
  private askedHevc = false;
  private hevcBroken = false;
  private snapshot: PlayerSnapshot = {
    phase: "connecting", transport: null, converted: false, detail: null, lastFrameAt: null, audioAvailable: false, listening: false, profile: null, firstFrameMs: null,
  };
  private asked = Date.now();

  constructor(private readonly options: Options) {}

  start(): void {
    this.stopped = false;
    this.connect();
    this.healthTimer = window.setInterval(() => this.checkHealth(), 1_000);
  }

  /** Release the camera connection, keeping the last frame visible as a poster. */
  stop(): void {
    this.stopped = true;
    this.options.video.onwaiting = null;
    this.keepLastFrame();
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    if (this.healthTimer !== null) window.clearInterval(this.healthTimer);
    this.cancelFrameCallback();
    this.closeSocket();
    this.closePictures();
    if (this.pictureUrl) URL.revokeObjectURL(this.pictureUrl); // The <img> keeps showing the picture it has already decoded
    this.pictureUrl = null;
    this.teardownMedia();
    this.update({ phase: "paused", detail: null });
  }

  retry(): void {
    this.attempt = 0;
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.closeSocket();
    this.closePictures();
    this.teardownMedia();
    this.connect();
  }

  setListening(on: boolean): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN || !this.snapshot.audioAvailable) return;
    this.socket.send(JSON.stringify({ type: "listen", on }));
    if (!on) {
      this.options.video.muted = true;
      this.update({ listening: false });
    }
  }

  private connect(): void {
    if (this.stopped) return;
    this.retryTimer = null;
    this.listen();
    const { cameraId, quality } = this.options;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    this.askedHevc = !this.hevcBroken && mseHevc();
    const socket = new WebSocket(withUnmasked(`${scheme}//${location.host}/api/cameras/${encodeURIComponent(cameraId)}/live/stream?quality=${quality}${this.askedHevc ? "&hevc=true" : ""}`, cameraId));
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    this.update({ phase: this.snapshot.lastFrameAt ? "reconnecting" : "connecting", detail: null });
    socket.onmessage = event => {
      if (typeof event.data === "string") {
        this.handleMessage(JSON.parse(event.data) as Record<string, unknown>);
        return;
      }
      // The growing wait starts over at the first picture shown (markFrame), not here: data that never becomes a picture is a failure too
      this.heardAt = Date.now();
      this.heard = true;
      this.options.onFragment?.(this.heardAt);
      this.enqueue(event.data as ArrayBuffer);
    };
    socket.onclose = event => {
      if (this.socket !== socket || this.stopped) return;
      this.socket = null;
      if (event.code === 4415) return; // handled by the "unsupported" message
      if (NO_RETRY_CODES.has(event.code)) {
        this.update({ phase: "error", detail: event.reason || this.snapshot.detail || "Live video is not available for this camera." });
        return;
      }
      this.scheduleReconnect();
    };
  }

  private handleMessage(message: Record<string, unknown>): void {
    switch (message.type) {
      case "init": {
        const mime = String(message.mime);
        if (!("MediaSource" in window) || !MediaSource.isTypeSupported(mime)) {
          this.closeSocket();
          if (message.codec === "hevc" && this.askedHevc) {
            // The browser said it could play H.265 and cannot: ask the NVR to convert it to H.264 instead of dropping to MJPEG.
            this.hevcBroken = true;
            this.connect();
            return;
          }
          this.useFallback(withUnmasked(`/api/cameras/${encodeURIComponent(this.options.cameraId)}/live?quality=${this.options.quality}`, this.options.cameraId));
          return;
        }
        if (this.snapshot.transport === "mjpeg") this.options.onFallback(null); // The camera's video changed to one this browser plays
        this.update({ transport: "mse", converted: Boolean(message.transcoded), profile: message.profile === "sub" ? "sub" : "main", audioAvailable: Boolean(message.audio) });
        this.setupMedia(mime);
        return;
      }
      case "listening":
        this.options.video.muted = !message.on;
        this.update({ listening: Boolean(message.on), detail: message.on ? this.snapshot.detail : (message.detail as string | null) ?? null });
        return;
      case "unsupported":
        if (this.askedHevc && !this.hevcBroken) {
          this.hevcBroken = true;
          this.closeSocket();
          this.connect();
          return;
        }
        this.useFallback(String(message.fallback));
        return;
      default:
        if (typeof message.detail === "string") this.update({ detail: message.detail });
    }
  }

  private useFallback(url: string): void {
    this.teardownMedia();
    this.update({ transport: "mjpeg", phase: this.snapshot.lastFrameAt ? "reconnecting" : "connecting", detail: COMPATIBILITY });
    this.options.onFallback(url);
    void this.readPictures(url);
  }

  /** A new connection starts being listened to: the silence that ends it is counted from now. */
  private listen(): void {
    this.askedAt = this.heardAt = Date.now();
    this.heard = this.shown = false;
  }

  /**
   * Read the compatibility-mode stream and show each picture as it arrives. When it ends, ask again: the server ends it on purpose when
   * masks or access change, and a stream that ended by itself must not leave its last picture on screen looking live.
   */
  private async readPictures(url: string): Promise<void> {
    this.closePictures();
    const controller = new AbortController();
    this.pictures = controller;
    this.listen();
    let refusal: string | null = null;
    try {
      const response = await fetch(url, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (this.pictures !== controller) return;
      if (!response.ok || !response.body) {
        const said = (await response.json().catch(() => null)) as { detail?: unknown } | null;
        if (this.pictures !== controller) return;
        refusal = typeof said?.detail === "string" ? said.detail : "Live video is not available for this camera.";
        if (NO_RETRY_STATUSES.has(response.status)) {
          this.pictures = null;
          this.update({ phase: "error", detail: refusal });
          return;
        }
      } else {
        const split = pictureSplitter();
        const reader = response.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (this.pictures !== controller) return;
          if (value) for (const jpeg of split(value)) this.showPicture(jpeg);
          if (done) break;
        }
      }
    } catch {
      if (this.pictures !== controller) return; // Stopped, retried or dropped by the watchdog: whoever did that carries on
    }
    this.pictures = null;
    this.scheduleReconnect();
    if (refusal) this.update({ detail: refusal }); // For example every live-view place being taken: said, and tried again
  }

  private showPicture(jpeg: Uint8Array): void {
    this.heardAt = Date.now();
    this.heard = true;
    const image = this.options.picture?.();
    if (image) {
      const previous = this.pictureUrl;
      this.pictureUrl = URL.createObjectURL(new Blob([jpeg as BlobPart], { type: "image/jpeg" }));
      image.src = this.pictureUrl;
      if (previous) URL.revokeObjectURL(previous);
    }
    this.markFrame();
  }

  private closePictures(): void {
    const reading = this.pictures;
    this.pictures = null;
    reading?.abort();
  }

  private setupMedia(mime: string): void {
    this.teardownMedia();
    const source = new MediaSource();
    this.source = source;
    this.objectUrl = URL.createObjectURL(source);
    const { video } = this.options;
    video.muted = !this.snapshot.listening;
    video.src = this.objectUrl;
    video.onwaiting = () => this.snapshot.phase === "live" && this.options.onStall?.(Date.now()); // buffering at connect is not a slow link
    source.addEventListener("sourceopen", () => {
      if (this.source !== source) return;
      const buffer = source.addSourceBuffer(mime);
      buffer.mode = "segments";
      buffer.addEventListener("updateend", () => this.drain());
      this.buffer = buffer;
      this.drain();
    }, { once: true });
    this.watchFrames();
  }

  private enqueue(data: ArrayBuffer): void {
    this.queue.push(data);
    if (this.queue.length > 120) this.queue.splice(0, this.queue.length - 120);
    this.drain();
  }

  private drain(): void {
    const { buffer, source } = this;
    if (!buffer || !source || source.readyState !== "open" || buffer.updating) return;
    const { video } = this.options;
    const ranges = buffer.buffered;
    if (ranges.length) {
      const start = ranges.start(0);
      const end = ranges.end(ranges.length - 1);
      // Stay at the live edge and keep the buffer small.
      if (end - video.currentTime > 2) video.currentTime = Math.max(start, end - 0.3);
      if (video.currentTime - start > 30) {
        buffer.remove(start, video.currentTime - 10);
        return;
      }
    }
    const next = this.queue.shift();
    if (!next) return;
    try {
      buffer.appendBuffer(next);
    } catch {
      this.scheduleReconnect();
      return;
    }
    if (video.paused) void video.play().catch(() => undefined);
  }

  private watchFrames(): void {
    const { video } = this.options;
    this.cancelFrameCallback();
    if ("requestVideoFrameCallback" in video) {
      const tick = () => {
        this.markFrame();
        this.frameCallback = video.requestVideoFrameCallback(tick);
      };
      this.frameCallback = video.requestVideoFrameCallback(tick);
    } else {
      (video as HTMLVideoElement).addEventListener("timeupdate", () => this.markFrame());
    }
  }

  private cancelFrameCallback(): void {
    const video = this.options.video;
    if (this.frameCallback !== null && "cancelVideoFrameCallback" in video) video.cancelVideoFrameCallback(this.frameCallback);
    this.frameCallback = null;
  }

  private markFrame(): void {
    this.disconnectedAt = null;
    if (!this.shown) this.attempt = 0;
    this.shown = true;
    const now = Date.now();
    if (this.snapshot.phase !== "live" || !this.snapshot.lastFrameAt || now - this.snapshot.lastFrameAt > 500) {
      this.update({ phase: "live", lastFrameAt: now, detail: this.snapshot.transport === "mjpeg" ? this.snapshot.detail : null,
                    firstFrameMs: this.snapshot.firstFrameMs ?? now - this.asked });
    } else {
      this.snapshot.lastFrameAt = now;
    }
  }

  private checkHealth(): void {
    const { phase, lastFrameAt } = this.snapshot;
    const now = Date.now();
    if (phase === "live" && lastFrameAt && now - lastFrameAt > STALE_AFTER_MS) this.update({ phase: "stale" });
    const dropped = this.dropIfSilent(now);
    const since = this.disconnectedAt ?? (phase === "stale" ? lastFrameAt : null);
    if ((phase === "stale" || phase === "reconnecting") && since && now - since > OFFLINE_AFTER_MS) {
      // "Retrying" is said only when it is true: a retry is waiting, or this connection has gone quiet and will be dropped for one.
      const retrying = dropped || this.retryTimer !== null || now - this.heardAt > STALE_AFTER_MS;
      this.update({ phase: "offline", detail: `No video has arrived from this camera.${retrying ? " Retrying automatically." : ""}` });
    }
  }

  /**
   * The watchdog. A connection is up, or being made, and no retry is waiting: drop it and ask again when it has given nothing for too
   * long, or (the video path) when data keeps arriving and no picture has ever come of it. The second is judged only while the video is
   * on a page someone can see, because a browser does not draw, and so does not report, frames for a tab in the background.
   */
  private dropIfSilent(now: number): boolean {
    if (this.stopped || this.retryTimer !== null || (!this.socket && !this.pictures)) return false;
    const silent = now - this.heardAt > (this.heard ? SILENT_AFTER_DATA_MS : SILENT_BEFORE_DATA_MS);
    if (document.visibilityState !== "visible") this.askedAt = Math.max(this.askedAt, now - 1_000);
    const neverShown = Boolean(this.socket) && this.heard && !this.shown && this.options.video.offsetParent !== null
      && now - this.askedAt > SILENT_BEFORE_DATA_MS;
    if (!silent && !neverShown) return false;
    this.scheduleReconnect();
    return true;
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    this.keepLastFrame();
    this.closeSocket();
    this.closePictures();
    this.teardownMedia();
    this.disconnectedAt ??= Date.now();
    const delay = reconnectDelay(this.attempt); // Spread out, so tiles do not all reconnect at the same instant after a restart (#539)
    this.attempt += 1;
    if (this.snapshot.phase !== "offline") this.update({ phase: "reconnecting" });
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = window.setTimeout(() => this.connect(), delay);
  }

  /** Freeze the most recent decoded frame into the poster so a failure never goes blank. */
  private keepLastFrame(): void {
    const { video } = this.options;
    if (!video.videoWidth || !video.videoHeight) return;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d")?.drawImage(video, 0, 0);
      video.poster = canvas.toDataURL("image/jpeg", 0.8);
    } catch {
      // A tainted or empty frame simply leaves the previous poster.
    }
  }

  private closeSocket(): void {
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close(1000);
  }

  private teardownMedia(): void {
    this.queue = [];
    this.buffer = null;
    if (this.source?.readyState === "open") {
      try {
        this.source.endOfStream();
      } catch {
        // Already closing.
      }
    }
    this.source = null;
    if (this.objectUrl) {
      this.options.video.removeAttribute("src");
      this.options.video.load();
      URL.revokeObjectURL(this.objectUrl);
      this.objectUrl = null;
    }
  }

  private update(patch: Partial<PlayerSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.options.onChange(this.snapshot);
  }
}
