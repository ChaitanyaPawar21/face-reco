import zipfile

import make_zip


def test_zip_holds_only_the_listed_files(tmp_path):
    out = tmp_path / "x.zip"
    make_zip.build(out)
    names = set(zipfile.ZipFile(out).namelist())
    assert names == {f"face-login/{f}" for f in make_zip.FILES}
    assert not [n for n in names if n.endswith((".db", ".log", ".jpg", ".png")) or ".venv" in n]
    assert {"face-login/app.py", "face-login/README.md"} <= names
