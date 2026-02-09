$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$guiCore = Join-Path $repoRoot "library_manager\\src\\gui_core.py"
$version = $null
if (Test-Path $guiCore) {
    $text = Get-Content -Raw -Path $guiCore
    $match = [regex]::Match($text, 'APP_VERSION\s*=\s*\"v?([0-9\.]+)\"')
    if ($match.Success) {
        $version = $match.Groups[1].Value
    }
}
if (-not $version) {
    throw "APP_VERSION not found in $guiCore"
}
$packageName = "kicad-library-manager-$version"
$distDir = Join-Path $repoRoot "dist"
$stageDir = Join-Path $distDir $packageName
$zipPath = Join-Path $distDir "$packageName.zip"

if (Test-Path $stageDir) {
    Remove-Item -Recurse -Force $stageDir
}
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

Copy-Item -Recurse -Force (Join-Path $repoRoot "pcm_package\\plugins") (Join-Path $stageDir "plugins")
Copy-Item -Force (Join-Path $repoRoot "pcm_package\\metadata.json") (Join-Path $stageDir "metadata.json")

$resourcesPath = Join-Path $repoRoot "pcm_package\\resources"
if (Test-Path $resourcesPath) {
    Copy-Item -Recurse -Force $resourcesPath (Join-Path $stageDir "resources")
}

$pluginPkg = Join-Path $stageDir "plugins"
Copy-Item -Recurse -Force (Join-Path $repoRoot "library_manager\\src\\*") $pluginPkg
$druTemplates = Join-Path $repoRoot "library_manager\\dru_templates"
if (Test-Path $druTemplates) {
    Copy-Item -Recurse -Force $druTemplates (Join-Path $pluginPkg "dru_templates")
}

if (!(Test-Path $distDir)) {
    New-Item -ItemType Directory -Force -Path $distDir | Out-Null
}

if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}

Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath

Write-Host "Built: $zipPath"
