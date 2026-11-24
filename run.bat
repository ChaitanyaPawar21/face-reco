@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv || goto :fail
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || goto :fail
.venv\Scripts\python app.py run || goto :fail
exit /b
:fail
echo Something went wrong. See the message above.
pause
