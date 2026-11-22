@echo off
REM ============================================================================
REM QiBa Knowledge Fabric - Model Download and Setup Script (Windows)
REM ============================================================================
REM
REM This script checks for cached ML models and downloads if needed.
REM It checks BOTH Windows cache and WSL cache locations.
REM Run this from the kbfabric root directory.
REM
REM Usage:
REM     .\custom_ml\download_and_setup.bat
REM
REM ============================================================================

echo ======================================================================
echo QiBa Knowledge Fabric - Model Download and Setup
echo ======================================================================
echo.

REM Change to the kbfabric directory (parent of custom_ml)
cd /d "%~dp0.."
echo Working directory: %CD%
echo.

REM Set cache paths - Windows AND WSL caches
set HF_CACHE_PATH=%USERPROFILE%\.cache\huggingface
set WSL_HF_CACHE_PATH=\\wsl$\Ubuntu\home\ashwinp\.cache\huggingface
set WSL_TORCH_CACHE_PATH=\\wsl$\Ubuntu\home\ashwinp\.cache\torch

echo Cache locations:
echo   Windows HF cache: %HF_CACHE_PATH%
echo   WSL HF cache:     %WSL_HF_CACHE_PATH%
echo   WSL Torch cache:  %WSL_TORCH_CACHE_PATH%
echo.

REM ============================================================================
REM Step 1: Check existing caches and setup models
REM ============================================================================
echo ----------------------------------------------------------------------
echo [1/3] Checking existing caches for models...
echo ----------------------------------------------------------------------
echo.

python -m custom_ml.download_models

REM Check if all models are already set up
if %ERRORLEVEL% EQU 0 (
    echo.
    echo All models found in cache! No download needed.
    echo.
    goto :done
)

echo.
echo Some models not found in cache. Proceeding to download...
echo.

REM ============================================================================
REM Step 2: Download FinBERT (if not cached)
REM ============================================================================
echo ----------------------------------------------------------------------
echo [2/3] Downloading FinBERT (ProsusAI/finbert)...
echo ----------------------------------------------------------------------
echo.

python -c "from transformers import AutoModel, AutoTokenizer; print('Downloading FinBERT model...'); AutoModel.from_pretrained('ProsusAI/finbert'); print('Downloading FinBERT tokenizer...'); AutoTokenizer.from_pretrained('ProsusAI/finbert'); print('FinBERT download complete!')"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Failed to download FinBERT. Check your internet connection.
    pause
    exit /b 1
)
echo.

REM ============================================================================
REM Step 3: Download CrossEncoder (if not cached)
REM ============================================================================
echo ----------------------------------------------------------------------
echo [3/3] Downloading CrossEncoder (ms-marco-MiniLM-L-6-v2)...
echo ----------------------------------------------------------------------
echo.

python -c "from sentence_transformers import CrossEncoder; print('Downloading CrossEncoder model...'); CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2'); print('CrossEncoder download complete!')"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Failed to download CrossEncoder. Check your internet connection.
    pause
    exit /b 1
)
echo.

REM ============================================================================
REM Step 4: Final setup from newly downloaded models
REM ============================================================================
echo ----------------------------------------------------------------------
echo [4/4] Setting up bundled models from downloaded cache...
echo ----------------------------------------------------------------------
echo.

python -m custom_ml.download_models

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo WARNING: Model setup had issues. Check output above.
)

:done
echo.
echo ======================================================================
echo Done!
echo ======================================================================
echo.
pause
