$ErrorActionPreference = "Stop"

$apiKey = if ($env:API_SERVER_KEY) { $env:API_SERVER_KEY } else { "change-me-local-dev" }
$port = if ($env:API_SERVER_PORT) { $env:API_SERVER_PORT } else { "8642" }
$hostName = if ($env:API_SERVER_HOST) { $env:API_SERVER_HOST } else { "127.0.0.1" }

hermes config set API_SERVER_ENABLED true
hermes config set API_SERVER_HOST $hostName
hermes config set API_SERVER_PORT $port
hermes config set API_SERVER_KEY $apiKey

$env:API_SERVER_ENABLED = "true"
$env:API_SERVER_HOST = $hostName
$env:API_SERVER_PORT = $port
$env:API_SERVER_KEY = $apiKey
$env:HERMES_ACCEPT_HOOKS = "1"

Write-Host "Starting Hermes gateway with API server enabled on http://$hostName`:$port"
hermes gateway run --accept-hooks
