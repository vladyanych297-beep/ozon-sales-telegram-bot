param(
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [int]$Port = 8000,
    [string]$EnvironmentFile = ''
)

$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$pythonPath = Join-Path $rootPath '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Сначала создайте .venv и установите requirements-server.txt по инструкции Android.'
}
if (-not $EnvironmentFile) {
    $EnvironmentFile = Join-Path $rootPath '.env.forecast'
    if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
        $EnvironmentFile = Join-Path $rootPath '.env'
    }
}
if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
    throw 'Forecast environment file was not found.'
}
Set-Location -LiteralPath $rootPath
$env:PYTHONUTF8 = '1'
$env:FORECAST_ENV_FILE = (Resolve-Path -LiteralPath $EnvironmentFile).Path
# Bind to loopback only. Publish through an HTTPS reverse proxy after separate setup.
& $pythonPath -m uvicorn ozon_forecast.api:app --host 127.0.0.1 --port $Port --workers 1 --no-access-log
exit $LASTEXITCODE
