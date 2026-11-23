# Live video kept ready (#216)

Opening a camera that nobody is watching makes the NVR connect to it and wait for it to start sending. On one site that took 2.5 to 6.4 seconds, nearly all of it the camera getting going (not the NVR waiting for a keyframe). Someone opening a camera that is already being watched joins the running stream in about 0.1 seconds. So the way to take the wait away is to have the stream open before anyone asks.

**Settings › General › Live video start-up** has three choices:

| Choice | What the NVR does |
|---|---|
| **Ready for every camera** (the default) | Holds each camera's live stream open all the time. The first person to open a camera joins a stream that is already running. |
| **Keep cameras that were just watched** | Holds nothing ahead, but a stream stays open for ten minutes after the last viewer leaves (instead of five seconds). |
| **Only while someone is watching** | As before: the stream closes five seconds after the last viewer leaves. |

## What is held

For each camera that is enabled and not archived, the stream a viewer opening a grid is given: the substream if the camera has one, else the main stream (the same stream, prepared the same way, with the same privacy mask drawn in; the keeper and the live endpoint share `app/live_variant.py`, so a held stream is exactly the one a viewer joins). A tile that is enlarged on a camera that has a substream plays the main stream, which is held only for cameras without a substream; it still starts on demand.

Not held: a camera whose H.265 would have to be converted to H.264, or whose privacy mask the NVR draws in (both are a full re-encode, and close 5 seconds after their last viewer, #488; browsers that play H.265 take the unconverted stream and are held), a camera whose codec a browser cannot play directly, an archived or disabled camera. At most 24 streams are held; the list in Settings says how many are, and each one's state.

## Cost

A held stream is one more connection to the camera (the recorder holds one, detection another when it reads the stream) and the same data again over the network from the camera to the NVR. Passing the video through costs very little CPU; a stream with a privacy mask drawn in is re-encoded, and one with audio converts the audio to AAC. If a camera limits how many connections it accepts, or the network between the cameras and the NVR is tight, choose **Keep cameras that were just watched** or **Only while someone is watching**.

A camera that does not answer is tried again after 15 seconds, then 30, 60, up to every five minutes, so an offline camera is not hammered; each wait has a random share of up to half again added, so cameras that went offline together are not all tried again in the same second (#495). Changing the choice takes effect within seconds and is recorded in the audit log.

## Reading Diagnostics

Settings › Diagnostics › Live video start-up counts viewers who **started the camera's stream** and those who **joined a running stream**. With streams held ready, almost every viewer joins a running one (about 0.1 s), and the "started" row stays empty except after a camera was offline or the mode was changed.

**Cameras with keyframes far apart (#496).** A viewer who joins a running stream is given what came since the latest keyframe, so the picture decodes at once. That is kept up to 60 fragments (about 18 seconds) and 32 MB. A camera whose keyframes are further apart (a "smart codec" mode can go a minute between them) has nothing kept past that point: a viewer who joins then is sent nothing until the next keyframe, and then plays from it, rather than being given more than a viewer's queue holds and closed as too slow. Camera health says which cameras these are.
