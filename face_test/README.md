# Local YuNet + SFace face scan

CPU-only local face enrollment and verification using OpenCV Zoo YuNet and SFace. The OpenCV UI uses a fixed portrait canvas. The project stores normalized embeddings and pose metadata only; it does not save face images or video, and it has no database, website, or liveness detection.

The project works on Windows, macOS, and Linux. A Python virtual environment is recommended on every operating system.

## Setup

Run the commands from this project folder.

### Windows (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python download_models.py
```

If PowerShell prevents activation, open a permitted shell or activate the environment using `.venv\Scripts\activate.bat` from Command Prompt.

### macOS and Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python download_models.py
```

`requirements.txt` uses the non-headless `opencv-python` package for `cv2.imshow`, plus NumPy. The model downloader prints the SHA256 and size for each model and gives the exact URL and manual destination if a download fails.

## Enroll

```text
python enroll.py --name "Alice"
```

Enrollment requires YuNet landmarks to produce a solvePnP head-pose estimate. It calibrates a neutral pose, then requires three distinct captures for each of nine measured poses: center, left, right, up, down, and the four diagonals. The direction ticks advance only when embeddings are kept; the center indicator and checklist show per-pose coverage. A 40-second attempt timeout never accepts partial coverage. Failed attempts can retry up to three times while retaining captures for completed poses and requesting missing poses. Before saving, the scan requires all nine poses, at least 40 degrees of measured yaw span and 20 degrees of pitch span, per-pose consistency, overall cohesion, and a five-frame live self-test. A passing enrollment writes normalized embeddings to `data/<name>.npy` and pose-count/range metadata to `data/<name>_meta.json`. If the name already exists, confirm overwrite in the terminal before the camera opens. Press `Q` or `ESC` to cancel.

Use `--debug` to print per-second frame and rejection counters and a final summary for every attempt:

```text
python enroll.py --name "Alice" --debug
```

`--image` is deliberately rejected for enrollment: a still image cannot prove coverage of all nine required poses.

## Verify

```text
python verify.py
python verify.py --threshold 0.50
python verify.py --attack-test
```

Verification starts after one good face is held for 0.7 seconds. It must collect eight quality-gated frames within five seconds. Each frame is aligned from the original unmirrored camera frame with the same `face_core.get_embedding` function used by enrollment, then L2-normalized. Each person's frame score is the mean of their top three cosine similarities. A frame votes only if its score meets the threshold and, when multiple identities are enrolled, beats the second-place score by at least `0.05`. Recognition requires at least six votes out of eight; a nearest-name fallback is never used. The default threshold is `0.50`, unless a completed `data/calibration.json` supplies a threshold; `--threshold` overrides either. The terminal prints each frame's scores, margin, vote, and the final decision. Fewer than eight good frames produces "Can't get a clear scan, try again".

`--attack-test` reports PASS only when the live subject is rejected and FAIL if anyone is recognized. With only one identity enrolled, false accepts are more likely; enroll at least two people and calibrate with impostors. A single `--image` is diagnostic only and never produces an identity decision because it cannot meet the eight-frame voting rule:

```text
python verify.py --image <image-file>
```

## Calibrate the threshold

Collect ten genuine webcam scans in varied lighting and at small angle changes:

```text
python calibrate.py --mode genuine --name "Alice"
```

For webcam impostor data, use at least two people who are not enrolled; each is prompted for ten scans:

```text
python calibrate.py --mode impostor --people 2
```

Alternatively, supply at least ten usable impostor face images:

```text
python calibrate.py --mode impostor --impostor-images <image-folder>
```

Calibration stores numeric scores only (never photos or video) in `data/calibration.json`, prints genuine/impostor summaries and histograms, and recommends the higher of `0.45` or the highest impostor score plus `0.05`. If the score ranges overlap, the tool warns that the model/camera cannot reliably separate the groups and recommends a fail-closed threshold that may reject genuine scans. Collect both genuine and impostor data before relying on a calibration threshold.

## Enrollment files, camera access, and tests

```text
python list_and_delete.py
python list_and_delete.py --delete "Alice"
python -m unittest discover -s tests -v
```

Files in `data/<name>.npy` have shape `(N, 128)` with up to 27 normalized embeddings; corresponding `_meta.json` files contain pose counts and angle ranges, never images. The camera helper tries indexes 0–3, selects the preferred OpenCV backend for the current operating system with the default backend as a fallback, requests 640×480, discards ten startup frames, and uses the actual frame size. If no camera opens, the message includes only the applicable access hint: Windows Settings > Privacy > Camera, macOS camera permission for the terminal or app, or Linux camera permissions/device availability.

The display mirrors a square center crop; detection and SFace alignment use the original unmirrored frame. For best results, keep your face well lit, sharp, and centered. This demo does not prevent spoofing and is not a production identity or access-control system.
