@echo off
setlocal

REM Build Windows EXE for Gateway.py (Intrinsic Alarm version)

python --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  exit /b 1
)

echo [1/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo [2/3] Building EXE with PyInstaller...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --name DanfossGatewayIR ^
  --hidden-import BAC0 ^
  --collect-submodules BAC0 ^
  --collect-all bacpypes3 ^
  --collect-all BAC0 ^
  --hidden-import bacpypes3.service.object ^
  --hidden-import bacpypes3.service.device ^
  --hidden-import bacpypes3.service.client ^
  --add-data "config.json;." ^
  Gateway.py

if errorlevel 1 (
  echo [ERROR] Build failed.
  exit /b 1
)

echo [3/3] Copying config.json next to EXE...
copy /Y config.json dist\config.json >nul

echo [DONE] EXE created:
echo   dist\DanfossGatewayIR.exe
echo.
echo Edit dist\config.json as needed, then run DanfossGatewayIR.exe.
endlocal
