# Empacota laya_app.py em um LayaLocal.exe. Venv e pastas de build ficam fora do OneDrive.
# Saída: %LOCALAPPDATA%\Codex\Laya\build\dist\LayaLocal.exe (copie para prototype\dist com o app fechado).
$ErrorActionPreference = 'Stop'
$base = Join-Path $env:LOCALAPPDATA 'Codex\Laya'
$venv = Join-Path $base 'app-venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $python)) {
    uv venv --python 3.11 $venv
    uv pip install --python $python PySide6-Essentials pyinstaller
}
& $python -m PyInstaller (Join-Path $PSScriptRoot 'LayaLocal.spec') --noconfirm `
    --distpath (Join-Path $base 'build\dist') --workpath (Join-Path $base 'build\work')
exit $LASTEXITCODE
