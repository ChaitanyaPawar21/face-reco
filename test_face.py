from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

import face
from face import Challenge, Face


def unit(i, j=None, cos=1.0):
    """128-d unit vector on axis i, or at a given cosine to it (towards axis j)."""
    v = np.zeros(128, np.float32)
    v[i] = cos
    if j is not None:
        v[j] = (1 - cos * cos) ** 0.5
    return v


A, B = unit(0), unit(1)
YAW = {"center": 0.0, "left": 0.4, "right": -0.4}


def user(name, vec, role="user"):
    return NS(name=name, vec=vec, role=role)


def play(ch, vec=A, flip_at=None):
    """Follow the prompts; at turn number flip_at, turn the wrong way."""
    result, n = "continue", 0
    while result == "continue":
        want = ch.prompt
        yaw = YAW[want]
        if want != "center":
            if n == flip_at:
                yaw = -yaw
            n += 1
        result = ch.feed([Face(vec, yaw)])
    return result


# --- match
def test_match_threshold_is_stricter_for_admins():
    assert not face.is_match(unit(0, 5, 0.49), user("a", A))
    assert face.is_match(unit(0, 5, 0.52), user("a", A))
    assert not face.is_match(unit(0, 5, 0.52), user("a", A, "admin"))
    assert face.is_match(unit(0, 5, 0.56), user("a", A, "admin"))


def test_nan_and_unreadable_templates_never_match():
    assert not face.is_match(np.full(128, np.nan), user("a", A))
    assert not face.is_match(A, user("a", np.zeros(128)))


def test_small_face_is_not_captured_when_a_size_is_required():
    ch = Challenge(45, min_face=80)
    assert ch.feed([Face(A, 0, 79)]) == "continue" and not ch.anchored
    assert ch.feed([Face(A, 0, 80)]) == "continue" and ch.anchored


def test_duplicate():
    assert face.is_duplicate(unit(0, 5, 0.46), [user("a", A)])
    assert not face.is_duplicate(unit(0, 5, 0.40), [user("a", A)])


# --- challenge
def test_following_the_prompts_passes_and_yields_vector():
    ch = Challenge(45)
    assert play(ch) == "ok"
    assert float(ch.vec @ A) == pytest.approx(1.0)


def test_challenge_has_four_turns_returning_to_center():
    ch = Challenge(45)
    assert len(ch.steps) == 9
    assert ch.steps[::2] == ["center"] * 5
    assert set(ch.steps[1::2]) <= {"left", "right"}


def test_sequences_vary():
    assert len({tuple(Challenge(45).steps) for _ in range(100)}) > 5


@pytest.mark.parametrize("n", range(4))
def test_wrong_turn_fails(n):
    ch = Challenge(45)
    assert play(ch, flip_at=n) == "fail" and ch.reason == "wrong turn"


def test_still_photo_never_passes():
    ch = Challenge(45)
    assert all(ch.feed([Face(A, 0.0)]) == "continue" for _ in range(200))


def test_two_faces_fails():
    ch = Challenge(45)
    assert ch.feed([Face(A, 0), Face(B, 0)]) == "fail" and ch.reason == "several faces"


def test_person_swap_while_facing_fails():
    ch = Challenge(45)
    ch.feed([Face(A, 0)])
    assert ch.feed([Face(B, 0)]) == "fail" and ch.reason == "person changed"


def test_person_swap_while_turned_fails():
    ch = Challenge(45)
    ch.feed([Face(A, 0)])
    assert ch.feed([Face(B, YAW[ch.prompt])]) == "fail"


def test_no_face_before_start_just_waits():
    ch = Challenge(45)
    assert all(ch.feed([]) == "continue" for _ in range(50))
    assert not ch.anchored


def test_face_lost_after_start_fails():
    ch = Challenge(45)
    ch.feed([Face(A, 0)])
    assert [ch.feed([]) for _ in range(6)] == ["continue"] * 5 + ["fail"]


def test_expired_fails(monkeypatch):
    ch = Challenge(45)
    now = face.time.time()
    monkeypatch.setattr(face.time, "time", lambda: now + 46)
    assert ch.feed([Face(A, 0)]) == "fail" and ch.reason == "expired"


def test_finished_challenge_stays_finished():
    ch = Challenge(45)
    play(ch)
    assert ch.feed([Face(A, 0)]) == "fail"


# --- real models
@pytest.mark.parametrize("data", [b"", b"not an image", b"\xff\xd8\xff" + b"0" * 500])
def test_garbage_image_is_no_face(data):
    assert face.analyze(data) == []


def test_huge_image_is_no_face():
    png = cv2.imencode(".png", np.zeros((5000, 5000), np.uint8))[1].tobytes()
    assert face.analyze(png) == []


def test_blank_image_is_no_face():
    assert face.analyze(cv2.imencode(".jpg", np.zeros((480, 640, 3), np.uint8))[1].tobytes()) == []


def test_tampered_model_is_refused(tmp_path):
    p = tmp_path / "m.onnx"
    p.write_bytes(b"evil")
    with pytest.raises(RuntimeError, match="does not match"):
        face.verified(p, "0" * 64)
