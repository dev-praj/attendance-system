import numpy as np
import pytest

from app.face.matching import decide_votes, frame_vote, score_embedding


def embedding(index):
    vector = np.zeros(128, dtype=np.float32)
    vector[index] = 1.0
    return vector


def test_normalized_cosine_scores_increase_with_similarity():
    enrollments = {"known": np.stack([embedding(0)] * 3)}
    assert score_embedding(embedding(0), enrollments)["known"] == pytest.approx(1.0)
    assert score_embedding(embedding(1), enrollments)["known"] == pytest.approx(0.0)


def test_eight_genuine_frames_are_accepted_by_six_vote_rule():
    frames = [{"known": 0.9} for _ in range(8)]
    decision = decide_votes(frames, threshold=0.5)
    assert decision == {
        "accepted": True, "identity": "known", "votes": {"known": 8}, "reason": "accepted"
    }


def test_impostor_below_threshold_does_not_vote():
    assert frame_vote({"known": 0.49}, threshold=0.5)[0] is None
    assert not decide_votes([{"known": 0.49} for _ in range(8)], 0.5)["accepted"]


def test_four_qualifying_frames_are_rejected():
    frames = [{"known": 0.9} for _ in range(4)] + [{"known": 0.1} for _ in range(4)]
    decision = decide_votes(frames, threshold=0.5)
    assert decision["accepted"] is False
    assert decision["votes"] == {"known": 4}


def test_close_identity_scores_fail_the_margin_gate():
    name, score, margin, reason = frame_vote(
        {"first": 0.80, "second": 0.77},
        threshold=0.5,
    )
    assert name is None
    assert score == pytest.approx(0.80)
    assert margin == pytest.approx(0.03)
    assert reason == "low_margin"
