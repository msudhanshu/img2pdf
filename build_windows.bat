@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo  Building Img2PDF.exe (Windows, no console)
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.11+ from https://www.python.org/downloads/
  echo and check "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv
    pause
    exit /b 1
  )
)

echo Installing / updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 (
  echo Dependency install failed.
  pause
  exit /b 1
)

echo.
echo Running PyInstaller...
".venv\Scripts\pyinstaller.exe" --noconfirm Img2PDF.spec
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo ============================================
echo  Done.
echo  Double-click this to open the app:
echo    %cd%\dist\Img2PDF.exe
echo.
echo  Copy Img2PDF.exe to any Windows 10 PC and
echo  double-click it. No Python install needed.
echo ============================================
echo.
explorer "%cd%\dist"
pause
