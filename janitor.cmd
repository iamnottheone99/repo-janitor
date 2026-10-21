@echo off
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
python -m repo_janitor.cli %*
