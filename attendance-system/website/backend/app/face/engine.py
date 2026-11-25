"""YuNet/SFace CPU engine ported from the sibling face_test project."""

import math
from pathlib import Path

import cv2
import numpy as np

from ..settings import BACKEND_DIR


MODEL_DIR = BACKEND_DIR / "models"
YUNET_PATH = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODEL_DIR / "face_recognition_sface_2021dec.onnx"
EMBEDDING_SIZE = 128
MAX_DETECTION_DIMENSION = 960
MODEL_3D = np.asarray(
    [(-32, 30, 0), (32, 30, 0), (0, 0, -30), (-25, -25, -5), (25, -25, -5)],
    dtype=np.float64,
)


class FaceEngine:
    def __init__(self):
        self.detector = None
        self.recognizer = None

    def load(self):
        missing = [path for path in (YUNET_PATH, SFACE_PATH) if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Missing YuNet/SFace model(s): "
                + ", ".join(str(path) for path in missing)
                + ". Run python scripts/download_models.py."
            )
        self.detector = cv2.FaceDetectorYN.create(
            str(YUNET_PATH), "", (320, 320), 0.5, 0.3, 5000,
            cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU,
        )
        self.recognizer = cv2.FaceRecognizerSF.create(
            str(SFACE_PATH), "",
            cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU,
        )
        return self

    def detect(self, frame):
        if self.detector is None:
            raise RuntimeError("Face models are not loaded.")
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("Expected a color frame.")
        height, width = frame.shape[:2]
        scale = min(1.0, MAX_DETECTION_DIMENSION / max(height, width))
        resized = (
            cv2.resize(
                frame,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
            if scale < 1
            else frame
        )
        source_h, source_w = resized.shape[:2]
        self.detector.setInputSize((source_w, source_h))
        _, rows = self.detector.detect(resized)
        if rows is None:
            return []
        mapped = np.asarray(rows, dtype=np.float32).copy()
        mapped[:, [0, 2, 4, 6, 8, 10, 12]] *= width / source_w
        mapped[:, [1, 3, 5, 7, 9, 11, 13]] *= height / source_h
        return list(mapped)

    def align_and_embed(self, frame, face_row):
        if self.recognizer is None:
            raise RuntimeError("Face models are not loaded.")
        height, width = frame.shape[:2]
        row = np.asarray(face_row, dtype=np.float32).reshape(-1).copy()
        if row.size < 15 or not np.isfinite(row[:15]).all():
            raise ValueError("Invalid YuNet face row.")
        x, y, box_w, box_h = row[:4]
        x0, y0 = max(0, math.floor(x)), max(0, math.floor(y))
        x1, y1 = min(width, math.ceil(x + box_w)), min(height, math.ceil(y + box_h))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("Face box does not intersect the frame.")
        points = row[4:14].reshape(5, 2)
        if (points[:, 0] < 0).any() or (points[:, 0] >= width).any():
            raise ValueError("Face landmarks extend outside the frame.")
        if (points[:, 1] < 0).any() or (points[:, 1] >= height).any():
            raise ValueError("Face landmarks extend outside the frame.")
        row[:4] = (x0, y0, x1 - x0, y1 - y0)
        aligned = self.recognizer.alignCrop(frame, row)
        vector = np.asarray(self.recognizer.feature(aligned), dtype=np.float32).reshape(-1)
        if vector.size != EMBEDDING_SIZE or not np.isfinite(vector).all():
            raise ValueError("SFace returned an invalid embedding.")
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            raise ValueError("SFace returned a zero-length embedding.")
        return vector / norm

    def estimate_pose(self, face_row, frame_size):
        width, height = frame_size
        row = np.asarray(face_row, dtype=np.float32).reshape(-1)
        points = row[4:14].reshape(5, 2).astype(np.float64)
        eye_distance = float(np.linalg.norm(points[0] - points[1]))
        if (
            not np.isfinite(points).all()
            or not math.isfinite(eye_distance)
            or eye_distance <= 1e-6
        ):
            raise ValueError("Invalid facial landmarks.")
        camera = np.asarray(
            [[width, 0, width / 2], [0, width, height / 2], [0, 0, 1]],
            dtype=np.float64,
        )
        solved, rotation, _ = cv2.solvePnP(
            MODEL_3D, points, camera, np.zeros((4, 1)), flags=cv2.SOLVEPNP_EPNP
        )
        if not solved or not np.isfinite(rotation).all():
            raise ValueError("Head pose could not be estimated.")
        matrix, _ = cv2.Rodrigues(rotation)
        sy = math.sqrt(matrix[0, 0] ** 2 + matrix[1, 0] ** 2)
        pitch, yaw, roll = (
            math.atan2(matrix[2, 1], matrix[2, 2]),
            math.atan2(-matrix[2, 0], sy),
            math.atan2(matrix[1, 0], matrix[0, 0]),
        )
        return tuple(math.degrees(angle) for angle in (yaw, pitch, roll))

    def quality(self, frame, face_row, threshold=0.7, sharpness_floor=15.0):
        row = np.asarray(face_row, dtype=np.float32)
        height, width = frame.shape[:2]
        if float(row[14]) < threshold:
            return False, "low_score", 0.0
        x, y, box_w, box_h = row[:4]
        center_x, center_y = x + box_w / 2, y + box_h / 2
        if box_w < width * 0.12:
            return False, "too_small", 0.0
        if box_w > width * 0.70:
            return False, "too_large", 0.0
        if not (width * 0.2 <= center_x <= width * 0.8 and height * 0.2 <= center_y <= height * 0.8):
            return False, "off_center", 0.0
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(width, int(x + box_w)), min(height, int(y + box_h))
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return False, "off_center", 0.0
        sharpness = cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        if sharpness < sharpness_floor:
            return False, "blurry", sharpness
        return True, None, sharpness


engine = FaceEngine()
