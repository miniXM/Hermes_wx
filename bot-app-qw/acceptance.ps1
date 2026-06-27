$ErrorActionPreference = "Stop"

$AdapterBase = if ($env:WECOM_PC_HOOK_BASE_URL) { $env:WECOM_PC_HOOK_BASE_URL.TrimEnd("/") } else { "http://127.0.0.1:8001" }
$HermesBase = if ($env:HERMES_API_BASE_URL) { $env:HERMES_API_BASE_URL.TrimEnd("/") } else { "http://127.0.0.1:8642/v1" }
$ApiKey = if ($env:HERMES_API_KEY) { $env:HERMES_API_KEY } elseif ($env:API_SERVER_KEY) { $env:API_SERVER_KEY } else { "change-me-local-dev" }
$Token = if ($env:WECOM_PC_HOOK_TOKEN) { $env:WECOM_PC_HOOK_TOKEN } elseif ($env:BOT_HOOK_TOKEN) { $env:BOT_HOOK_TOKEN } else { "testtoken" }

function Show-Step($Name) {
    Write-Host ""
    Write-Host "== $Name =="
}

Show-Step "WeCom PC Hook adapter health"
$adapterHealth = Invoke-RestMethod -Uri "$AdapterBase/health"
$adapterHealth | ConvertTo-Json -Compress

Show-Step "Adapter queue poll"
$poll = Invoke-WebRequest -UseBasicParsing -Uri "$AdapterBase/hook/$Token"
Write-Host "GET /hook status: $($poll.StatusCode)"
if ($poll.StatusCode -ne 204 -and $poll.StatusCode -ne 200) {
    throw "Unexpected adapter poll status: $($poll.StatusCode)"
}

Show-Step "Hermes health"
$hermesHealthUrl = $HermesBase -replace "/v1$", ""
$hermesHealth = Invoke-RestMethod -Uri "$hermesHealthUrl/health"
$hermesHealth | ConvertTo-Json -Compress

Show-Step "Hermes models"
$models = Invoke-RestMethod -Uri "$HermesBase/models" -Headers @{
    Authorization = "Bearer $ApiKey"
}
$models | ConvertTo-Json -Depth 6 -Compress

Write-Host ""
Write-Host "Acceptance passed: Hermes API and WeCom PC Hook adapter are reachable."
