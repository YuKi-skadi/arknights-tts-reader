param([string]$PythonPath = "", [string]$OutputDirectory = "")

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if ($PythonPath) { $python = $PythonPath }
if (-not $OutputDirectory) { $OutputDirectory = $root }
$build = Join-Path $root "build\pyinstaller"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Create it with: py -3.12 -m venv .venv"
}

# Do not resolve DLLs from unrelated image/PDF tools on the host PATH.
# Their ICU DLLs can have incompatible exports under the same filename.
$originalBuildPath = $env:PATH
$env:PATH = @((Join-Path $env:SystemRoot 'System32'), $env:SystemRoot, (Split-Path -Parent $python)) -join ';'
try {
# Set PATH inside Python as well: some managed launchers replace the shell PATH.
& $python -c "import os,runpy,sys; os.environ['PATH']=os.pathsep.join([os.path.join(os.environ['SystemRoot'],'System32'),os.environ['SystemRoot'],os.path.dirname(sys.executable)]); runpy.run_module('PyInstaller',run_name='__main__')" `
    --clean `
    --noconfirm `
    --onefile `
    --windowed `
    --name "ArknightsTTSReader" `
    --icon (Join-Path $root "assets\app_icon.ico") `
    --distpath $OutputDirectory `
    --workpath $build `
    --specpath $build `
    --exclude-module torch `
    --exclude-module qwen_tts `
    --exclude-module torchvision `
    --exclude-module torchaudio `
    --exclude-module transformers `
    --collect-all edge_tts `
    --collect-all aiohttp `
    --collect-all rapidocr_onnxruntime `
    --collect-all playsound3 `
    --hidden-import http.cookies `
    --hidden-import PIL.Image `
    --hidden-import PIL.ImageDraw `
    --hidden-import PIL.ImageGrab `
    --hidden-import PIL.ImageOps `
    --hidden-import PIL.ImageTk `
    (Join-Path $root "main.py")
} finally {
    $env:PATH = $originalBuildPath
}

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $OutputDirectory "ArknightsTTSReader.exe"
$size = (Get-Item -LiteralPath $exe).Length
Write-Output "Built: $exe"
Write-Output ("Size: {0:N2} MB" -f ($size / 1MB))
