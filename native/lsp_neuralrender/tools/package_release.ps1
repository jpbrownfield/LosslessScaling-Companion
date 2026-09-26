# Builds Release and assembles dist\LSP-NeuralRender-v<version>.zip.
# The DLSSNR snippet and the NVIDIA SDK are never packaged.
#
#   powershell -ExecutionPolicy Bypass -File tools\package_release.ps1 -Version 0.2.0 [-SkipBuild]
param(
    [string]$Version = "0.2.0",
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$build = Join-Path $root "build"

if (-not $SkipBuild) {
    cmake -S $root -B $build -G "Visual Studio 17 2022" -A x64
    if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }
    cmake --build $build --config Release -- /nologo /v:m
    if ($LASTEXITCODE -ne 0) { throw "build failed" }
}

$rel = Join-Path $build "Release"
foreach ($f in "LSP_NeuralRender.dll", "nvngx.dll_lspnr.dll", "lspnr_harness.exe") {
    if (-not (Test-Path (Join-Path $rel $f))) { throw "missing build output: $f" }
}

$name = "LSP-NeuralRender-v$Version"
$dist = Join-Path $root "dist"
$stage = Join-Path $dist $name
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
$addon = Join-Path $stage "addons\LSP-NeuralRender"
New-Item -ItemType Directory -Force $addon | Out-Null
New-Item -ItemType Directory -Force (Join-Path $stage "tools") | Out-Null

Copy-Item (Join-Path $rel "LSP_NeuralRender.dll") $addon
Copy-Item (Join-Path $rel "nvngx.dll_lspnr.dll") $addon
Copy-Item (Join-Path $root "addon.json") $addon
Copy-Item (Join-Path $rel "lspnr_harness.exe") (Join-Path $stage "tools")
Copy-Item (Join-Path $root "tools\release\INSTALL.txt") $stage
Copy-Item (Join-Path $root "docs\user-guide.md") (Join-Path $stage "GUIDE.md")
Copy-Item (Join-Path $root "LICENSE") (Join-Path $stage "LICENSE.txt")
Copy-Item (Join-Path $root "CHANGELOG.md") $stage

$zip = Join-Path $dist "$name.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal
Remove-Item -Recurse -Force $stage

Get-Item $zip | Select-Object Name, Length, LastWriteTime
