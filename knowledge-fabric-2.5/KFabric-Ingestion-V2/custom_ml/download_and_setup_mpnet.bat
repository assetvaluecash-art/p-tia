@echo off
REM ============================================================================
REM QiBa Knowledge Fabric - all-mpnet-base-v2 Download/Bundle Script (Windows)
REM ============================================================================
REM
REM Purpose:
REM   Download the Apache-2.0 embedding model:
REM     sentence-transformers/all-mpnet-base-v2
REM   and bundle it into:
REM     kbfabric-ingest\custom_ml\embeddings\models\all-mpnet-base-v2
REM
REM Why:
REM   This avoids git-lfs (often blocked) and writes a local folder that
REM   SentenceTransformers can load offline via local_model_path.
REM
REM Usage (from kbfabric-ingest root OR anywhere):
REM   .\custom_ml\download_and_setup_mpnet.bat
REM
REM ============================================================================

echo ======================================================================
echo QiBa Knowledge Fabric - all-mpnet-base-v2 Download and Bundle
echo ======================================================================
echo.

REM Change to the kbfabric-ingest directory (parent of custom_ml)
cd /d "%~dp0.."
echo Working directory: %CD%
echo.

set "MPNET_ID=sentence-transformers/all-mpnet-base-v2"
set "MPNET_DEST=%CD%\custom_ml\embeddings\models\all-mpnet-base-v2"

echo Model:
echo   %MPNET_ID%
echo Destination:
echo   %MPNET_DEST%
echo.

REM If already bundled, skip
if exist "%MPNET_DEST%\modules.json" (
  echo Found existing bundled model. modules.json present. Skipping download.
  echo.
  goto :done
)

REM Disable HF telemetry noise
set "HF_HUB_DISABLE_TELEMETRY=1"

echo ----------------------------------------------------------------------
echo Downloading and bundling model (this can take a few minutes)...
echo ----------------------------------------------------------------------
echo.

python -c "import os; import sys; mid=os.environ.get('MPNET_ID'); dest=os.environ.get('MPNET_DEST'); print(f'[INFO] Loading: {mid}'); from sentence_transformers import SentenceTransformer; m=SentenceTransformer(mid); os.makedirs(dest, exist_ok=True); print(f'[INFO] Saving to: {dest}'); m.save(dest); print('[INFO] Done.');"

if errorlevel 1 (
  echo.
  echo ERROR: Failed to download/bundle all-mpnet-base-v2.
  echo - Verify Python env has sentence-transformers installed.
  echo - If your network blocks Hugging Face HTTPS, you will still get a 403 here.
  echo.
  pause
  exit /b 1
)

echo.
echo ----------------------------------------------------------------------
echo Optional: verify folder contains modules.json + model files
echo ----------------------------------------------------------------------
dir "%MPNET_DEST%" | findstr /i "modules.json config.json model.safetensors pytorch_model.bin" >nul
echo.

:done
echo ======================================================================
echo Done!
echo ======================================================================
echo.
pause

