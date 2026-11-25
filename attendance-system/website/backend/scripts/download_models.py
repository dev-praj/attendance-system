"""Download the official OpenCV Zoo YuNet and SFace ONNX models."""

import hashlib
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models"
MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_detection_yunet/"
        "face_detection_yunet_2023mar.onnx"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_recognition_sface/"
        "face_recognition_sface_2021dec.onnx"
    ),
}


def main():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for filename, url in MODELS.items():
        destination = MODEL_DIR / filename
        temporary = destination.with_suffix(destination.suffix + ".download")
        try:
            urllib.request.urlretrieve(url, temporary)
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            temporary.replace(destination)
        except (OSError, urllib.error.URLError) as error:
            temporary.unlink(missing_ok=True)
            print(
                f"Download failed: {error}\nExact URL: {url}\n"
                f"Place the model manually at: {destination}"
            )
            return 1
        print(f"{destination.name}: {destination.stat().st_size} bytes, SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
