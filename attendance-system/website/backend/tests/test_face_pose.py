import cv2
import numpy as np

from app.face.engine import FaceEngine, MODEL_3D


def _synthetic_face_row(yaw, pitch):
    width, height = 640, 480
    camera = np.asarray(
        [[width, 0, width / 2], [0, width, height / 2], [0, 0, 1]],
        dtype=np.float64,
    )
    rotation = np.radians([pitch, yaw, 0]).astype(np.float64)
    points, _ = cv2.projectPoints(
        MODEL_3D,
        rotation,
        np.asarray([[0], [0], [500]], dtype=np.float64),
        camera,
        np.zeros((4, 1), dtype=np.float64),
    )
    row = np.zeros(15, dtype=np.float32)
    row[4:14] = points.reshape(-1).astype(np.float32)
    row[14] = 0.99
    return row


def test_solve_pnp_head_pose_preserves_yaw_and_pitch_signs():
    face_engine = FaceEngine()
    right_yaw, level_pitch, _ = face_engine.estimate_pose(_synthetic_face_row(25, 0), (640, 480))
    left_yaw, _, _ = face_engine.estimate_pose(_synthetic_face_row(-25, 0), (640, 480))
    _, up_pitch, _ = face_engine.estimate_pose(_synthetic_face_row(0, -18), (640, 480))
    _, down_pitch, _ = face_engine.estimate_pose(_synthetic_face_row(0, 18), (640, 480))

    assert right_yaw > 0
    assert left_yaw < 0
    assert abs(level_pitch) < 3
    assert up_pitch < 0
    assert down_pitch > 0
