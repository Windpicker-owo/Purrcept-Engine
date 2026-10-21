@echo off
setlocal
cd /d "%~dp0\..\.."
set LITELLM_LOCAL_MODEL_COST_MAP=True
".venv\Scripts\pythonw.exe" -m plugins.live_stage.credential_ui
