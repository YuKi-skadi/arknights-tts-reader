$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$build = Join-Path $root "build\pyinstaller"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Create it with: py -3.12 -m venv .venv"
}

& $python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --windowed `
    --name "ArknightsTTSReader" `
    --icon (Join-Path $root "assets\app_icon.ico") `
    --distpath $root `
    --workpath $build `
    --specpath $build `
    --exclude-module torch `
    --exclude-module qwen_tts `
    --collect-all edge_tts `
    --collect-all aiohttp `
    --collect-all rapidocr_onnxruntime `
    --hidden-import http.cookies `
    --hidden-import PIL.Image `
    --hidden-import PIL.ImageDraw `
    --hidden-import PIL.ImageGrab `
    --hidden-import PIL.ImageOps `
    --hidden-import PIL.ImageTk `
    (Join-Path $root "main.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $root "ArknightsTTSReader.exe"
$size = (Get-Item -LiteralPath $exe).Length
Write-Output "Built: $exe"
Write-Output ("Size: {0:N2} MB" -f ($size / 1MB))
