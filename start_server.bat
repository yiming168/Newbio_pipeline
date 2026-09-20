@echo off
setlocal

REM --- Paths -------------------------------------------------------
REM Set these once:
REM   setx LLAMA_DIR        "D:\path\to\llama.cpp"
REM   setx LLAMA_MODELS_DIR "D:\path\to\llama.cpp\models"
REM   setx PIPELINE_DIR     "G:\path\to\pipeline"
REM (open a NEW terminal afterwards for setx to take effect)
if "%LLAMA_DIR%"==""        set LLAMA_DIR=%~dp0
if "%LLAMA_MODELS_DIR%"=="" set LLAMA_MODELS_DIR=%LLAMA_DIR%\models
if "%PIPELINE_DIR%"==""     set PIPELINE_DIR=%~dp0

set MODEL=%LLAMA_MODELS_DIR%\Qwen3.6-35B-A3B-UD-IQ4_NL.gguf

REM --- Tuning knobs ------------------------------------------------
REM NCPUMOE: how many MoE expert layers to push to CPU.
REM   VRAM not full  -> lower it  (faster)
REM   Out of memory  -> raise it  (slower but fits)
set NCPUMOE=24
set CTX=8192
set PORT=8080
REM ------------------------------------------------------------------

if not exist "%LLAMA_DIR%\llama-server.exe" (
  echo.
  echo llama-server.exe not found in: %LLAMA_DIR%
  echo Set LLAMA_DIR, or copy this .bat next to llama-server.exe
  echo.
  pause
  exit /b 1
)

if not exist "%MODEL%" (
  echo.
  echo Model not found:
  echo   %MODEL%
  echo.
  echo Run this first:  py "%PIPELINE_DIR%\download_model.py"
  echo.
  pause
  exit /b 1
)

echo Starting llama-server on port %PORT%
echo Model    : %MODEL%
echo n-cpu-moe: %NCPUMOE%   ctx: %CTX%
echo.
echo Keep this window open. Test from another window with:
echo   cd /d "%PIPELINE_DIR%" ^&^& py screen_papers.py --selftest
echo.

"%LLAMA_DIR%\llama-server.exe" ^
  -m "%MODEL%" ^
  --port %PORT% ^
  -c %CTX% ^
  -ngl 99 ^
  --n-cpu-moe %NCPUMOE% ^
  --jinja

pause
