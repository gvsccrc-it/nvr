# Third-party notices

The NVR's own code is covered by [LICENSE](LICENSE). The published container image (`ghcr.io/gvsccrc-it/nvr`) also redistributes the
third-party software and AI models below, each under its own licence. This file is copied into the image at `/app/THIRD_PARTY_NOTICES.md`.
A test (`tests/test_third_party_notices.py`) checks that every file the `Dockerfile` downloads and every package it installs is listed here.

Each model is downloaded when the image is built, from the source given, and checked against a pinned SHA-256 in the `Dockerfile`.

## Programs from Debian

The image is built on the official `python:3.14-slim` image (Debian). These packages are installed from Debian with `apt-get`:

| Package | What it is used for | Licence | Source |
|---|---|---|---|
| `ffmpeg` | Recording, live video, conversion, exports, snapshots | **GPL-2.0-or-later** as Debian builds it (FFmpeg is LGPL-2.1-or-later; Debian enables its GPL parts) | Debian's `ffmpeg` source package, [sources.debian.org/src/ffmpeg](https://sources.debian.org/src/ffmpeg/); upstream [ffmpeg.org](https://ffmpeg.org/) |
| `smartmontools` | Disk health (SMART) | GPL-2.0-or-later | [sources.debian.org/src/smartmontools](https://sources.debian.org/src/smartmontools/) |
| `curl` | Downloading the models during the build | curl licence (MIT-style) | [sources.debian.org/src/curl](https://sources.debian.org/src/curl/) |
| `ca-certificates` | Trusted certificate authorities | MPL-2.0 (the certificate data) and GPL-2.0-or-later (the scripts) | [sources.debian.org/src/ca-certificates](https://sources.debian.org/src/ca-certificates/) |

**The source of the exact versions in an image.** The exact version of each package in an image is shown by `dpkg -s <package>` inside it, and its source package
is at [snapshot.debian.org](https://snapshot.debian.org/) under that version. Debian keeps each package's copyright and licence text in the image at
`/usr/share/doc/<package>/copyright`, along with those of the rest of the Debian base system.

## AI models

| File(s) in the image | Model | Licence | Source |
|---|---|---|---|
| `person-vehicle-bike-detection-2004.xml`, `person-vehicle-bike-detection-2004.bin` | Intel Open Model Zoo person, vehicle and bike detector (AI detection) | Apache-2.0 | [Open Model Zoo](https://github.com/openvinotoolkit/open_model_zoo), files from `storage.openvinotoolkit.org` (2021.4) |
| `yolo-v9-t-640-license-plates-end2end.onnx` | Licence plate detector, YOLOv9-t (licence plates) | MIT, as its distributor states; **see the note below** | [open-image-models](https://github.com/ankandrew/open-image-models) release `assets` |
| `global_mobile_vit_v2_ocr.onnx`, `global_mobile_vit_v2_ocr_config.yaml` | Plate reader, Latin-alphabet plates worldwide | MIT, as its distributor states | [fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) (files from the `cnn-ocr-lp` release `arg-plates`) |
| `european_mobile_vit_v2_ocr.onnx`, `european_mobile_vit_v2_ocr_config.yaml` | Plate reader, European plates | MIT, as its distributor states | [fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) (files from the `cnn-ocr-lp` release `arg-plates`) |
| `pose_landmarker_lite.task` (only `pose_landmarks_detector.tflite` is kept) | MediaPipe Pose Landmarker lite (fall detection) | Apache-2.0, as the MediaPipe project states (not independently verified) | [MediaPipe](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker), `storage.googleapis.com/mediapipe-models` |
| `face_detection_yunet_2023mar.onnx` | YuNet face detector (face watch) | MIT | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) |
| `face_recognition_sface_2021dec.onnx` | SFace face recogniser (face watch) | Apache-2.0 | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) |

**The plate detector.** The model is a YOLOv9 network trained by the open-image-models project, which publishes the weights under MIT. The YOLOv9 training code
is GPL-3.0. Whether weights trained with GPL code may be offered under MIT has not been settled, and no legal advice has been taken. The owner is waiting for that
advice (#552); `docs/licence-plates.md` has the details.

A licence on a model file covers the file. It says nothing about the pictures the model was trained on.

Models deliberately **not** used, because their licences restrict commercial use: Ultralytics YOLO weights (AGPL-3.0 or a paid licence) and InsightFace's pretrained
face models (non-commercial research only).

## Python packages

The Python packages in `requirements.txt` (FastAPI, OpenVINO, NumPy, OpenCV and the rest) are installed with pip, pinned with hashes. Each package's licence is
in its metadata in the image (`/usr/local/lib/python3.14/site-packages/<package>-<version>.dist-info/`, the `LICENSE` file or the `License` field of `METADATA`).
Python itself is under the PSF License.

## The web interface

The interface is built with Node.js from the packages in `web/package-lock.json` (React and others); the build keeps only what the interface uses, in
`/app/app/web_dist`. Each package's licence is in its own repository and in `web/node_modules/<package>/` after `npm ci`. The image does not contain Node.js.
