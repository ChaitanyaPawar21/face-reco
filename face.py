"""Face detection, matching and the head-turn liveness challenge."""
import hashlib
import math
import os
import secrets
import threading
import time
from collections import namedtuple
from pathlib import Path

os.environ["OPENCV_IO_MAX_IMAGE_PIXELS"] = "4000000"  # before cv2: refuse decompression bombs
import cv2
import numpy as np

# Cosine similarity thresholds. Starting points: calibrate on real faces with this camera.
MATCH = {"user": 0.50, "admin": 0.55}
DUPLICATE = 0.45  # a new face this close to a registered one is refused
SAME_FACING, SAME_TURNED = 0.50, 0.30
# Yaw: nose offset from the eye midpoint, in eye-distances. Positive = the person's left.
YAW_CENTER, YAW_TURN = 0.12, 0.28
TURNS, MAX_LOST = 4, 5
MIN_FACE = 80  # smallest face width to register, in pixels (image scaled to 640)

Face = namedtuple("Face", "vec yaw size", defaults=(MIN_FACE,))
MODELS = Path(__file__).parent / "models"


def verified(path, sha256):
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha256:
        raise RuntimeError(f"{path} does not match its expected SHA-256; refusing to run")
    return str(path)


_detector = cv2.FaceDetectorYN.create(
    verified(MODELS / "face_detection_yunet_2023mar.onnx",
             "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "", (320, 320), 0.7)
_recognizer = cv2.FaceRecognizerSF.create(
    verified(MODELS / "face_recognition_sface_2021dec.onnx",
             "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"), "")
_lock = threading.Lock()  # cv2 networks are not thread-safe


def detect(img):
    """All faces in a BGR image."""
    h, w = img.shape[:2]
    if max(h, w) > 640:
        s = 640 / max(h, w)
        w, h = max(1, int(w * s)), max(1, int(h * s))
        img = cv2.resize(img, (w, h))
    out = []
    with _lock:
        _detector.setInputSize((w, h))
        rows = _detector.detect(img)[1]
        for row in rows if rows is not None else []:
            v = _recognizer.feature(_recognizer.alignCrop(img, row)).flatten()
            v = v / (np.linalg.norm(v) or 1)
            r_eye, l_eye, nose = row[4:10].reshape(3, 2)
            axis = l_eye - r_eye
            yaw = float((nose - (r_eye + l_eye) / 2) @ axis / ((axis @ axis) or 1))
            if not (np.isfinite(v).all() and math.isfinite(yaw)):
                v, yaw = np.zeros(128, np.float32), 0.0  # unusable, but still counts as a face
            out.append(Face(v, yaw, float(row[2])))
    return out


def analyze(data):
    """All faces in encoded image bytes; anything undecodable is no face."""
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        return []
    return detect(img) if img is not None else []


def is_match(vec, user):
    return bool(float(vec @ user.vec) >= MATCH[user.role])  # NaN is refused


def is_duplicate(vec, users):
    return any(not float(vec @ u.vec) < DUPLICATE for u in users)


class Challenge:
    """Face forward, then 4 random head turns, facing forward after each."""

    def __init__(self, ttl, min_face=0):
        self.expires, self.min_face = time.time() + ttl, min_face
        self.steps = ["center"]
        for _ in range(TURNS):
            self.steps += [secrets.choice(["left", "right"]), "center"]
        self.pos, self.lost, self.samples = 0, 0, []
        self.vec = self.reason = None
        self.done = False

    @property
    def prompt(self):
        return self.steps[min(self.pos, len(self.steps) - 1)]

    @property
    def anchored(self):
        return bool(self.samples)

    def _fail(self, reason):
        self.done, self.reason = True, reason
        return "fail"

    def feed(self, faces):
        if self.done:
            return "fail"
        if time.time() > self.expires:
            return self._fail("expired")
        if len(faces) > 1:
            return self._fail("several faces")
        if not faces:
            self.lost += self.anchored
            return self._fail("face lost") if self.lost > MAX_LOST else "continue"
        self.lost = 0
        vec, yaw, size = faces[0]
        facing = abs(yaw) < YAW_CENTER
        if self.anchored:
            need = SAME_FACING if facing else SAME_TURNED
            if not float(vec @ self.samples[0]) >= need:
                return self._fail("person changed")
        want = self.prompt
        if want == "center":
            if facing and size >= self.min_face:
                self.samples.append(vec)
                self.pos += 1
        elif abs(yaw) > YAW_TURN:
            if (yaw > 0) != (want == "left"):
                return self._fail("wrong turn")
            self.pos += 1
        if self.pos < len(self.steps):
            return "continue"
        mean = np.mean(self.samples, axis=0)
        self.vec = (mean / np.linalg.norm(mean)).astype(np.float32)
        self.done = True
        return "ok"
