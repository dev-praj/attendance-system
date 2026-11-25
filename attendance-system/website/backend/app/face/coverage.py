"""Backend-authoritative nine-pose capture state and enrollment quality checks."""

import math

import numpy as np


POSE_BOUNDS = {
    "CENTER": ((-8, 8), (-8, 8)),
    "LEFT": ((None, -18), (-12, 12)),
    "RIGHT": ((18, None), (-12, 12)),
    "UP": ((-12, 12), (None, -10)),
    "DOWN": ((-12, 12), (10, None)),
    "UP-LEFT": ((None, -12), (None, -8)),
    "UP-RIGHT": ((12, None), (None, -8)),
    "DOWN-LEFT": ((None, -12), (8, None)),
    "DOWN-RIGHT": ((12, None), (8, None)),
}
PER_POSE = 3
MIN_CAPTURE_INTERVAL_SECONDS = 0.2


def _within(value, bounds):
    low, high = bounds
    return (low is None or value >= low) and (high is None or value <= high)


def classify_pose(yaw, pitch):
    if not math.isfinite(yaw) or not math.isfinite(pitch):
        return None
    names = ("UP-LEFT", "UP-RIGHT", "DOWN-LEFT", "DOWN-RIGHT", "CENTER", "LEFT", "RIGHT", "UP", "DOWN")
    for name in names:
        yaw_bounds, pitch_bounds = POSE_BOUNDS[name]
        if _within(yaw, yaw_bounds) and _within(pitch, pitch_bounds):
            return name
    return None


def new_state():
    return {
        "counts": {name: 0 for name in POSE_BOUNDS},
        "stable_pose": None,
        "stable_count": 0,
        "last_capture_at": {},
        "neutral_samples": [],
        "neutral_offset": None,
        "pose_history": [],
        "sharpness_samples": [],
        "previous_center": None,
        "attempt": 1,
        "self_test_failures": 0,
    }


def observe(state, pose, good, now=None):
    """Capture spaced, distinct frames after the measured pose is stable."""
    last_capture_at = state.setdefault("last_capture_at", {})
    if not good or pose not in POSE_BOUNDS or state["counts"][pose] >= PER_POSE:
        state["stable_pose"] = pose if good else None
        state["stable_count"] = 3 if good and pose in POSE_BOUNDS else 0
        return False
    if state["stable_pose"] == pose:
        state["stable_count"] += 1
    else:
        state["stable_pose"] = pose
        state["stable_count"] = 1
    state["stable_count"] = min(state["stable_count"], 3)
    if state["stable_count"] < 3:
        return False
    if now is None:
        # Keep the helper convenient for logic-only callers and tests.
        now = 0.0 if last_capture_at.get(pose) is None else (
            last_capture_at[pose] + MIN_CAPTURE_INTERVAL_SECONDS
        )
    last_capture = last_capture_at.get(pose)
    if last_capture is not None and float(now) - last_capture < MIN_CAPTURE_INTERVAL_SECONDS - 1e-6:
        return False
    state["counts"][pose] += 1
    last_capture_at[pose] = float(now)
    return True


def is_complete(state):
    return all(count >= PER_POSE for count in state["counts"].values())


def verify_embedding_quality(vectors):
    """Return consistency/cohesion summary; vectors must be grouped by pose."""
    filtered = {}
    for pose, values in vectors.items():
        rows = [np.asarray(value, dtype=np.float32) for value in values]
        keep = []
        for index, row in enumerate(rows):
            others = rows[:index] + rows[index + 1 :]
            if not others:
                continue
            mean = np.mean(others, axis=0)
            denominator = float(np.linalg.norm(row) * np.linalg.norm(mean))
            score = float(np.dot(row, mean) / denominator) if denominator else -1.0
            if score >= 0.45:
                keep.append(row)
        filtered[pose] = keep
    if any(len(filtered.get(pose, [])) < 2 for pose in POSE_BOUNDS):
        return False, filtered, None, "per-pose consistency failed"
    all_rows = [row for rows in filtered.values() for row in rows]
    similarities = []
    for i, first in enumerate(all_rows):
        for second in all_rows[i + 1 :]:
            similarities.append(float(np.dot(first, second)))
    cohesion = float(np.mean(similarities)) if similarities else 0.0
    return cohesion >= 0.50, filtered, cohesion, None if cohesion >= 0.50 else "cohesion failed"
