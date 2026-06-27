$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistRoot = Join-Path $Root "dist\nsis"

Push-Location $Root
try {
    if (-not (Test-Path -LiteralPath (Join-Path $Root "node_modules\electron-builder"))) {
        Write-Host "Installing npm dependencies..."
        npm install
    }

    Write-Host "Building standard NSIS installer..."
    npm run package:installer

    $Installer = Get-ChildItem -LiteralPath $DistRoot -Filter "HermesPluginSetup-*.exe" -ErrorAction Stop |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if (-not $Installer) {
        throw "Installer was not generated in $DistRoot"
    }

    $Hash = Get-FileHash -LiteralPath $Installer.FullName -Algorithm SHA256
    Write-Host ""
    Write-Host "Installer: $($Installer.FullName)"
    Write-Host "Size:      $($Installer.Length) bytes"
    Write-Host "SHA256:    $($Hash.Hash)"
}
finally {
    Pop-Location
}
