"""Normalized SFace cosine matching and conservative multi-frame decisions."""

import numpy as np


DEFAULT_THRESHOLD = 0.50
MINIMUM_MARGIN = 0.05
REQUIRED_FRAMES = 8
REQUIRED_VOTES = 6
TOP_EMBEDDINGS = 3
EMBEDDING_SIZE = 128


def normalize_embedding(embedding):
    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if vector.size != EMBEDDING_SIZE or not np.isfinite(vector).all():
        raise ValueError(f"Expected {EMBEDDING_SIZE} finite embedding values.")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise ValueError("Embedding must have non-zero length.")
    return vector / norm


def score_embedding(probe, enrollments):
    """Score each identity by the mean of its best three cosine similarities."""
    vector = normalize_embedding(probe)
    if not enrollments:
        raise ValueError("No enrolled identities are available.")
    scores = {}
    for identity, embeddings in enrollments.items():
        rows = np.asarray(embeddings, dtype=np.float32)
        if (
            rows.ndim != 2
            or rows.shape[0] == 0
            or rows.shape[1] != EMBEDDING_SIZE
            or not np.isfinite(rows).all()
        ):
            raise ValueError(f"Invalid enrollment embeddings for {identity!r}.")
        norms = np.linalg.norm(rows, axis=1, keepdims=True)
        if np.any(norms <= 0):
            raise ValueError(f"Invalid enrollment embeddings for {identity!r}.")
        rows = rows / norms
        similarities = rows @ vector
        best = np.sort(similarities)[-min(TOP_EMBEDDINGS, len(similarities)):]
        scores[identity] = float(np.mean(best))
    return scores


def frame_vote(scores, threshold, margin=MINIMUM_MARGIN):
    """Return a frame vote only when its cosine score and identity margin pass."""
    if not scores:
        raise ValueError("Cannot vote without enrolled identities.")
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    identity, score = ranked[0]
    gap = float("inf") if len(ranked) == 1 else float(score - ranked[1][1])
    if score < threshold:
        return None, float(score), gap, "below_threshold"
    if len(ranked) > 1 and gap < margin:
        return None, float(score), gap, "low_margin"
    return identity, float(score), gap, "vote"


def decide_votes(frame_results, threshold):
    if len(frame_results) != REQUIRED_FRAMES:
        return {"accepted": False, "identity": None, "votes": {}, "reason": "need 8 good frames"}
    votes = {}
    for scores in frame_results:
        identity, _, _, _ = frame_vote(scores, threshold)
        if identity is not None:
            votes[identity] = votes.get(identity, 0) + 1
    winner = max(votes, key=votes.get) if votes else None
    if winner is not None and votes[winner] >= REQUIRED_VOTES:
        return {"accepted": True, "identity": winner, "votes": votes, "reason": "accepted"}
    return {"accepted": False, "identity": None, "votes": votes, "reason": "insufficient_votes"}
