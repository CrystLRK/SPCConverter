param(
    [switch] $Zip,
    [switch] $SourceOnly
)

$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
$project = $PSScriptRoot
$workspace = Split-Path -Parent $project
$src = Join-Path $project 'src'
$build = Join-Path $workspace 'build\SPCConverter'
$release = Join-Path $build 'release\SPCConverter'
$version = (python -c "import sys; sys.path.insert(0, r'$src'); from spc_converter import VERSION; print(VERSION)").Trim()

python -m unittest discover -s (Join-Path $project 'tests') -v
if ($LASTEXITCODE -ne 0) { throw 'SPCConverter tests failed.' }

if (Test-Path -LiteralPath $release) {
    Remove-Item -LiteralPath $release -Recurse -Force
}
New-Item -ItemType Directory -Force $release | Out-Null
Copy-Item -LiteralPath (Join-Path $project 'config_profiles.json') -Destination $release -Force
Copy-Item -LiteralPath (Join-Path $project 'LICENSE') -Destination $release -Force

if (-not $SourceOnly) {
    python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        python -m pip install --upgrade pyinstaller
    }
    $distGui = Join-Path $build 'dist-gui'
    $distCli = Join-Path $build 'dist-cli'
    New-Item -ItemType Directory -Force $distGui, $distCli, (Join-Path $build 'gui-work'), (Join-Path $build 'cli-work'), (Join-Path $build 'spec-gui'), (Join-Path $build 'spec-cli') | Out-Null

    python -m PyInstaller --noconfirm --clean --onefile --windowed --name SPCConverter --paths $src --distpath $distGui --workpath (Join-Path $build 'gui-work') --specpath (Join-Path $build 'spec-gui') --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox --hidden-import PIL --hidden-import PIL.Image --hidden-import mutagen (Join-Path $src 'gui_launcher.py')
    if ($LASTEXITCODE -ne 0) { throw 'GUI build failed.' }
    python -m PyInstaller --noconfirm --clean --onefile --console --name SPCConverterCLI --paths $src --distpath $distCli --workpath (Join-Path $build 'cli-work') --specpath (Join-Path $build 'spec-cli') (Join-Path $src 'spc_converter.py')
    if ($LASTEXITCODE -ne 0) { throw 'CLI build failed.' }
    Copy-Item -LiteralPath (Join-Path $distGui 'SPCConverter.exe') -Destination $release -Force
    Copy-Item -LiteralPath (Join-Path $distCli 'SPCConverterCLI.exe') -Destination $release -Force
}

$zipPath = Join-Path $build "release\SPCConverter-windows-$version.zip"
if ($Zip) {
    Compress-Archive -Path (Join-Path $release '*') -DestinationPath $zipPath -Force
    Write-Host "Windows package: $zipPath"
}

Get-ChildItem -LiteralPath $release -Recurse -File |
    Select-Object FullName, Length |
    Format-Table -AutoSize
