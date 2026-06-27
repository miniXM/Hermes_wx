$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistRoot = Join-Path $Root "dist"
$ElectronOut = Join-Path $DistRoot "electron"
$ElectronApp = Join-Path $DistRoot "electron-app"
$FinalDir = Join-Path $DistRoot "HermesPlugin-electron"
$ZipPath = Join-Path $DistRoot "HermesPlugin-electron.zip"

function Resolve-OutputDir {
    param([Parameter(Mandatory = $true)][string]$PreferredPath)

    if (-not (Test-Path -LiteralPath $PreferredPath)) {
        return $PreferredPath
    }

    $preferredFullPath = [System.IO.Path]::GetFullPath($PreferredPath).TrimEnd('\')
    Get-Process -Name "HermesPlugin" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Path -and [System.IO.Path]::GetFullPath($_.Path).StartsWith($preferredFullPath, [System.StringComparison]::OrdinalIgnoreCase)
        } |
        Stop-Process -Force

    Start-Sleep -Milliseconds 500

    try {
        Remove-Item -LiteralPath $PreferredPath -Recurse -Force -ErrorAction Stop
        return $PreferredPath
    } catch {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $alternate = "${PreferredPath}.clean-$stamp"
        if (Test-Path -LiteralPath $alternate) {
            Remove-Item -LiteralPath $alternate -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Release folder is in use; using alternate output: $alternate"
        return $alternate
    }
}

function Copy-IfExists {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $source = Join-Path $Root $Name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $Destination $Name) -Force
    }
}

if (Test-Path -LiteralPath $ElectronOut) {
    Remove-Item -LiteralPath $ElectronOut -Recurse -Force
}
if (Test-Path -LiteralPath $ElectronApp) {
    Remove-Item -LiteralPath $ElectronApp -Recurse -Force
}
$FinalDir = Resolve-OutputDir -PreferredPath $FinalDir
$ZipPath = if ($FinalDir -eq (Join-Path $DistRoot "HermesPlugin-electron")) {
    $ZipPath
} else {
    "$FinalDir.zip"
}

New-Item -ItemType Directory -Path (Join-Path $ElectronApp "electron") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Root "electron\main.js") -Destination (Join-Path $ElectronApp "electron\main.js") -Force
Copy-Item -LiteralPath (Join-Path $Root "electron\preload.js") -Destination (Join-Path $ElectronApp "electron\preload.js") -Force
Copy-Item -LiteralPath (Join-Path $Root "electron\renderer.html") -Destination (Join-Path $ElectronApp "electron\renderer.html") -Force
Copy-Item -LiteralPath (Join-Path $Root "electron\renderer.css") -Destination (Join-Path $ElectronApp "electron\renderer.css") -Force
Copy-Item -LiteralPath (Join-Path $Root "electron\renderer.js") -Destination (Join-Path $ElectronApp "electron\renderer.js") -Force
Copy-Item -LiteralPath (Join-Path $Root "electron\assets") -Destination (Join-Path $ElectronApp "electron\assets") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $Root "hermes_plugins") -Destination (Join-Path $ElectronApp "hermes_plugins") -Recurse -Force

$electronPackageJson = @"
{
  "name": "hermes-wecom-plugin",
  "version": "1.0.0",
  "main": "electron/main.js"
}
"@
Set-Content -LiteralPath (Join-Path $ElectronApp "package.json") -Value $electronPackageJson -Encoding UTF8

if (-not (Test-Path -LiteralPath $ElectronOut)) {
    New-Item -ItemType Directory -Path $ElectronOut -Force | Out-Null
}

Write-Host "Assembling Electron shell from local runtime..."
$ElectronDist = Join-Path $Root "node_modules\electron\dist"
if (-not (Test-Path -LiteralPath (Join-Path $ElectronDist "electron.exe"))) {
    throw "Local Electron runtime is missing: $ElectronDist"
}
$ManualDir = Join-Path $ElectronOut "HermesPlugin-win32-x64"
if (Test-Path -LiteralPath $ManualDir) {
    Remove-Item -LiteralPath $ManualDir -Recurse -Force
}
Copy-Item -LiteralPath $ElectronDist -Destination $ManualDir -Recurse -Force
Rename-Item -LiteralPath (Join-Path $ManualDir "electron.exe") -NewName "HermesPlugin.exe" -Force
$ResourcesApp = Join-Path $ManualDir "resources\app"
New-Item -ItemType Directory -Path $ResourcesApp -Force | Out-Null
Copy-Item -Path (Join-Path $ElectronApp "*") -Destination $ResourcesApp -Recurse -Force
$packagedDir = Get-Item -LiteralPath $ManualDir

Move-Item -LiteralPath $packagedDir.FullName -Destination $FinalDir

Write-Host "Copying plugin runtime next to Electron executable..."
$runtimeFiles = @(
    "cli.exe",
    "loader.dll",
    "helper.dll",
    "bot.ini",
    "README.md",
    "libbrotlicommon.dll",
    "libbrotlidec.dll",
    "libcares-2.dll",
    "libcrypto-3.dll",
    "libcurl-4.dll",
    "libgcc_s_dw2-1.dll",
    "libiconv-2.dll",
    "libidn2-0.dll",
    "libintl-8.dll",
    "libnghttp2-14.dll",
    "libnghttp3-9.dll",
    "libngtcp2_crypto_ossl-0.dll",
    "libngtcp2-16.dll",
    "libpsl-5.dll",
    "libssh2-1.dll",
    "libssl-3.dll",
    "libunistring-5.dll",
    "libwinpthread-1.dll",
    "libzstd.dll",
    "zlib1.dll"
)

foreach ($file in $runtimeFiles) {
    Copy-IfExists -Name $file -Destination $FinalDir
}

$notes = @"
Hermes WeCom Plugin Electron package

Start:
  Double-click HermesPlugin.exe

This package includes only the plugin:
  - Electron desktop control console.
  - Hermes WeCom PC Hook gateway adapter installer.
  - cli.exe plus DLL runtime files.

Not included:
  - Hermes.
  - Enterprise WeChat.
  - MSYS2, GCC, or source build tools.

Before using real replies:
  - Install Hermes and make sure the `hermes` command works in PowerShell.
  - Configure the Hermes model/provider in Hermes itself.
  - Open and log in to Enterprise WeChat, or set the WXWork.exe path in the app.

The app controls:
  - Hermes Gateway through `hermes gateway run --accept-hooks`.
  - Enterprise WeChat by configured WXWork.exe path.
  - WeCom PC Hook adapter and cli.exe.

The runtime path is:
  Enterprise WeChat -> cli.exe -> WeCom PC Hook adapter on 127.0.0.1:8001 -> Hermes Gateway.
"@
Set-Content -LiteralPath (Join-Path $FinalDir "RUN-ME.txt") -Value $notes -Encoding UTF8

if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}

Write-Host "Creating Electron zip package..."
$archiveItems = Get-ChildItem -LiteralPath $FinalDir -Force
Compress-Archive -LiteralPath $archiveItems.FullName -DestinationPath $ZipPath -Force

Write-Host ""
Write-Host "Electron release folder: $FinalDir"
Write-Host "Electron zip package:    $ZipPath"
