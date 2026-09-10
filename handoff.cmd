@echo off
REM Runs tools\handoff.py from this folder against any repository.
REM   handoff doctor --repo D:\Vida\Profissional\htdocs\ementa
REM   set HANDOFF_REPO=D:\Vida\Profissional\htdocs\ementa  &&  handoff status
setlocal
REM Probe by running it: a "python3" on PATH may be the Microsoft Store alias.
set "PY="
for %%C in (python3 python py) do (
  if not defined PY (
    %%C -c "import sys" >nul 2>&1 && set "PY=%%C"
  )
)
if not defined PY (
  echo handoff: no working Python 3 interpreter found on PATH 1>&2
  exit /b 2
)
"%PY%" "%~dp0tools\handoff.py" %*
exit /b %ERRORLEVEL%
