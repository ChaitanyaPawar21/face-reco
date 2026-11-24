"""Build face-login.zip to hand over. Only the files listed here are included."""
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
FILES = [
    "README.md", "requirements.txt", "run.bat", "app.py", "face.py", "store.py",
    "test_app.py", "test_face.py",
    "models/face_detection_yunet_2023mar.onnx", "models/face_recognition_sface_2021dec.onnx",
    "public/camera.js", "public/style.css", "public/index.html", "public/login.html",
    "public/setup.html", "public/admin.html",
]


def build(out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            z.write(ROOT / f, f"face-login/{f}")


if __name__ == "__main__":
    build(ROOT / "face-login.zip")
    print("Wrote face-login.zip")
